"""워커 진입점.

    python3 worker/main.py backfill --symbols 005930,000660   # 초기 적재
    python3 worker/main.py once                                # 전 작업 1회
    python3 worker/main.py run                                 # 스케줄러 상주

설계 메모
  • 이 프로세스가 '토큰 단일 발급 지점'이다. 여러 개 띄우지 말 것.
    (client 당 유효 토큰 1개 — 재발급하면 서로를 401 로 만든다)
  • 휴장일에는 시세 폴링을 돌리지 않는다 (rate limit 낭비).
  • 대시보드(Next.js)는 이 DB 를 읽기만 한다. 나중에 Vercel 로 떼어낸다.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))

import accounts as ACC                          # noqa: E402
from analysis import gemini, regime, strategy   # noqa: E402
from collectors import jobs as J                 # noqa: E402
from collectors import rss, sec13f, sources      # noqa: E402
from config import get_settings                  # noqa: E402
from toss import TossClient                      # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("worker")

# 1분봉을 수집할 관심종목 기본값.
# ⚠️ 512MB 상한 — 1분봉은 종목당 하루 약 60KB. 20종목이면 하루 1.2MB.
DEFAULT_WATCH = ["005930", "000660"]


def connect(s):
    return psycopg.connect(s.database_url, autocommit=False)


# ── 작업 정의 ────────────────────────────────────────────────
def job_reference(c, conn, symbols: list[str]) -> None:
    """영업일 1회면 충분한 참조 데이터."""
    with J.job_run(conn, "reference") as j:
        j.rows = J.collect_stocks(c, conn, symbols)
        J.collect_commissions(c, conn)
        j.rows += J.collect_warnings(c, conn, symbols)


def job_daily_candles(c, conn) -> None:
    with J.job_run(conn, "daily_candles") as j:
        for sym in J.watched_symbols(conn):
            j.rows += J.collect_candles(c, conn, sym, "1d", pages=1)


def job_minute_candles(c, conn) -> None:
    """장중에만. 1회 200봉 = 약 3.3시간치라 시간당 1회면 충분히 겹친다."""
    if not J.market_open(c, "KR"):
        log.info("[minute_candles] 국내 휴장 — 건너뜀")
        return
    with J.job_run(conn, "minute_candles") as j:
        for sym in J.watched_symbols(conn):
            j.rows += J.collect_candles(c, conn, sym, "1m", pages=1)


def job_indicators(c, conn) -> None:
    with J.job_run(conn, "indicators") as j:
        j.rows = J.collect_indicator_candles(c, conn)
        j.rows += J.collect_investor_trading(c, conn, "1d")


def job_portfolio_all(conn, s) -> None:
    """사용자별 포트폴리오 스냅샷 + 전략.

    시세·뉴스·13F 는 공용이라 1회만 수집한다.
    계좌 데이터만 사용자마다 각자의 자격증명으로 받는다.
    한 사용자가 실패해도 나머지는 계속 진행한다.
    """
    users = ACC.active_users(conn)
    if not users:
        log.info("[portfolio] 등록된 사용자 없음")
        return
    with J.job_run(conn, "portfolio_all") as j:
        for uid, acc_seq in users:
            try:
                uc = TossClient(s, conn, user_id=uid, account_seq=acc_seq)
                try:
                    j.rows += J.collect_portfolio(uc, conn, user_id=uid)
                    strategy.build_strategy(conn, s.sentiment_model,
                                            s.gemini_api_key, user_id=uid)
                finally:
                    uc.close()
            except Exception as e:
                log.warning("[portfolio] %s 실패: %s", uid[:8], str(e)[:140])


# ── 분석 파이프라인 ─────────────────────────────────────────
def job_rss(conn, symbols: list[str]) -> None:
    """RSS + 네이버 뉴스. Reddit 은 API 승인이 필요해져 RSS 로 받는다."""
    with J.job_run(conn, "rss") as j:
        j.rows = rss.collect_all(conn)

    with J.job_run(conn, "naver_news") as j:
        with conn.cursor() as cur:
            cur.execute("SELECT symbol, name FROM stock WHERE symbol = ANY(%s)", (symbols,))
            pairs = cur.fetchall()
        for sym, name in pairs:
            if name:
                j.rows += sources.collect_naver_news(conn, sym, name, display=30)


def job_sentiment(conn, s) -> None:
    with J.job_run(conn, "sentiment") as j:
        j.rows = gemini.score_sentiment(conn, s.sentiment_model, s.gemini_api_key,
                                        batch=s.sentiment_batch_size, limit=200)


def job_analyst(conn, s, symbols: list[str]) -> None:
    with J.job_run(conn, "analyst_views") as j:
        for sym in symbols:
            j.rows += gemini.extract_analyst_views(
                conn, s.sentiment_model, s.gemini_api_key, sym, batch=8, limit=40)


def job_briefing(conn, s, symbols: list[str]) -> None:
    with J.job_run(conn, "briefing") as j:
        for sym in symbols:
            try:
                if gemini.build_briefing(conn, s.sentiment_model, s.gemini_api_key, sym):
                    j.rows += 1
            except Exception as e:
                log.warning("[briefing] %s 실패: %s", sym, str(e)[:120])


def job_13f(conn) -> None:
    """SEC 13F. 분기 공시라 하루 1회로 충분하다.
    이미 적재된 분기는 건너뛰므로 반복 실행이 싸다."""
    with J.job_run(conn, "sec_13f") as j:
        j.rows = sec13f.collect(conn)
        if j.rows:
            sec13f.link_tickers(conn)


def job_regime(conn) -> None:
    """공포탐욕지수. 국내는 자체 산출(공인 지표 아님)."""
    with J.job_run(conn, "regime") as j:
        got = regime.collect_all(conn)
        j.rows = sum(1 for v in got.values() if v is not None)


def job_strategy(conn, s) -> None:
    """맞춤 전략. 숫자는 코드가 계산하고 LLM 은 서술만 한다."""
    with J.job_run(conn, "strategy") as j:
        j.rows = 1 if strategy.build_strategy(
            conn, s.sentiment_model, s.gemini_api_key) else 0


def job_maintenance(c, conn) -> None:
    with J.job_run(conn, "maintenance") as j:
        j.rows = c.flush_observations()


# ── 모드 ─────────────────────────────────────────────────────
def cmd_backfill(c, conn, symbols: list[str], pages: int) -> None:
    log.info("초기 적재 시작 — 종목 %d개, 일봉 %d페이지", len(symbols), pages)
    job_reference(c, conn, symbols)
    n = J.set_watchlist(conn, symbols)
    log.info("관심종목 %d개 지정 (1분봉 수집 대상)", n)

    with J.job_run(conn, "backfill_daily") as j:
        for sym in symbols:
            got = J.collect_candles(c, conn, sym, "1d", pages=pages)
            log.info("  %s 일봉 %d봉", sym, got)
            j.rows += got
    job_indicators(c, conn)
    job_portfolio_all(conn, s)
    job_maintenance(c, conn)


def cmd_once(c, conn, symbols: list[str], s) -> None:
    job_reference(c, conn, symbols)
    job_daily_candles(c, conn)
    job_minute_candles(c, conn)
    job_indicators(c, conn)
    cmd_analyze(conn, symbols, s)
    job_portfolio_all(conn, s)
    job_maintenance(c, conn)


def cmd_analyze(conn, symbols: list[str], s) -> None:
    """수집 → 감성 → 추출 → 브리핑 → 국면 → 전략."""
    job_rss(conn, symbols)
    job_sentiment(conn, s)
    job_analyst(conn, s, symbols)
    job_briefing(conn, s, symbols)
    job_regime(conn)
    job_13f(conn)


# ── GitHub Actions 용 모드 ───────────────────────────────────
#  상주 스케줄러(run) 대신 배치로 쪼갠다. 이 작업들은 전부 주기 배치라
#  상주 프로세스가 필요 없다 — Actions 가 시간에 맞춰 깨우면 된다.
def cmd_news(conn, symbols: list[str], s) -> None:
    """RSS · 네이버뉴스 → 감성분류. 하루 4회."""
    job_rss(conn, symbols)
    job_sentiment(conn, s)


def cmd_market(c, conn) -> None:
    """장중 1분봉. 휴장이면 즉시 종료한다."""
    job_minute_candles(c, conn)
    job_maintenance(c, conn)


def cmd_daily(c, conn, symbols: list[str], s) -> None:
    """일봉·지표·13F·애널리스트·브리핑·국면 → 사용자별 포트폴리오·전략."""
    job_reference(c, conn, symbols)
    job_daily_candles(c, conn)
    job_indicators(c, conn)
    job_analyst(conn, s, symbols)
    job_briefing(conn, s, symbols)
    job_regime(conn)
    job_13f(conn)
    job_portfolio_all(conn, s)
    job_maintenance(c, conn)


def cmd_run(c, conn, symbols: list[str], s) -> None:
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    sch = BlockingScheduler(timezone="Asia/Seoul")

    # 장 시작 전 참조 데이터 갱신
    sch.add_job(lambda: job_reference(c, conn, symbols),
                CronTrigger(day_of_week="mon-fri", hour=8, minute=30), id="reference")
    # 장중 1분봉 (09~16시 매시 정각)
    sch.add_job(lambda: job_minute_candles(c, conn),
                CronTrigger(day_of_week="mon-fri", hour="9-16", minute=0), id="minute")
    # 장 마감 후 일봉·지표·포트폴리오
    sch.add_job(lambda: job_daily_candles(c, conn),
                CronTrigger(day_of_week="mon-fri", hour=20, minute=30), id="daily")
    sch.add_job(lambda: job_indicators(c, conn),
                CronTrigger(day_of_week="mon-fri", hour=20, minute=40), id="indicators")
    sch.add_job(lambda: job_portfolio_all(conn, s),
                CronTrigger(day_of_week="mon-fri", hour=20, minute=50), id="portfolio")
    # 정리 — 압축·보존정책이 없으므로 직접 돌린다
    sch.add_job(lambda: _retention(conn),
                CronTrigger(hour=3, minute=0), id="retention")
    sch.add_job(lambda: job_maintenance(c, conn),
                CronTrigger(minute="*/10"), id="maintenance")

    # ── 분석 파이프라인 ──
    # RSS 는 장중·장외 무관하게 돌린다 (해외 뉴스가 밤에 들어온다)
    sch.add_job(lambda: job_rss(conn, symbols),
                CronTrigger(hour="7,12,18,22", minute=10), id="rss")
    sch.add_job(lambda: job_sentiment(conn, s),
                CronTrigger(hour="7,12,18,22", minute=25), id="sentiment")
    sch.add_job(lambda: job_analyst(conn, s, symbols),
                CronTrigger(hour="8,21", minute=5), id="analyst")
    sch.add_job(lambda: job_regime(conn),
                CronTrigger(hour="8,21", minute=15), id="regime")
    # 13F 는 분기 공시 — 하루 1회면 충분 (새 분기 없으면 즉시 종료)
    sch.add_job(lambda: job_13f(conn),
                CronTrigger(hour=6, minute=30), id="sec13f")
    sch.add_job(lambda: job_briefing(conn, s, symbols),
                CronTrigger(hour="8,21", minute=30), id="briefing")
    # 전략은 브리핑·국면이 끝난 뒤에 (장 시작 전, 마감 후)
    sch.add_job(lambda: job_portfolio_all(conn, s),
                CronTrigger(hour="8,21", minute=45), id="strategy")

    log.info("스케줄러 시작 — 등록된 작업 %d개 (Ctrl+C 로 종료)", len(sch.get_jobs()))
    for j in sch.get_jobs():
        log.info("  %-12s next=%s", j.id, j.next_run_time)
    sch.start()


def _retention(conn) -> None:
    from db import retention
    with J.job_run(conn, "retention"):
        retention.run(conn, dry=False)


# ── main ─────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["backfill", "once", "run", "analyze",
                                     "news", "market", "daily"])
    ap.add_argument("--symbols", default=",".join(DEFAULT_WATCH))
    ap.add_argument("--pages", type=int, default=3,
                    help="backfill 시 일봉 페이지 수 (1페이지=200봉≈10개월)")
    a = ap.parse_args()

    s = get_settings()
    symbols = [x.strip() for x in a.symbols.split(",") if x.strip()]
    missing = s.missing()
    if missing:
        sys.exit(f"설정 누락: {', '.join(missing)} → open -e ~/toss-dashboard/.env")

    conn = connect(s)
    c = TossClient(s, conn)
    log.info("주문 실행 모드: %s",
             "⚠️ 실주문 ON" if s.execute_orders else "드라이런 (EXECUTE_ORDERS=false)")
    try:
        {"backfill": lambda: cmd_backfill(c, conn, symbols, a.pages),
         "once": lambda: cmd_once(c, conn, symbols, s),
         "analyze": lambda: cmd_analyze(conn, symbols, s),
         "news": lambda: cmd_news(conn, symbols, s),
         "market": lambda: cmd_market(c, conn),
         "daily": lambda: cmd_daily(c, conn, symbols, s),
         "run": lambda: cmd_run(c, conn, symbols, s)}[a.mode]()
    except KeyboardInterrupt:
        log.info("중단")
    finally:
        c.close()
        conn.close()


if __name__ == "__main__":
    main()
