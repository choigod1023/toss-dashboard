"""수집 작업 모음.

각 함수는 (client, conn) 을 받아 DB 에 적재하고 적재 행수를 반환한다.
전부 job_run 테이블에 실행 이력이 남는다 (조용한 실패 방지).
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from typing import Any

import psycopg

log = logging.getLogger(__name__)


# ── 파싱 헬퍼 ────────────────────────────────────────────────
def num(v: Any) -> float | None:
    """토스는 정밀도 보존을 위해 숫자를 문자열로 준다."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def dig(d: Any, *path, default=None):
    """중첩 dict 안전 접근. dailyProfitLoss.amount.krw 같은 구조용."""
    cur = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def _ts(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        return None


# ── 1. 종목 마스터 ───────────────────────────────────────────
def collect_stocks(c, conn: psycopg.Connection, symbols: list[str]) -> int:
    """영업일 단위 갱신 데이터. 짧은 주기로 폴링하지 말 것."""
    rows = []
    for s in c.stocks(symbols):
        kmd = s.get("koreanMarketDetail") or {}
        rows.append((
            s["symbol"], s.get("name"), s.get("englishName"), s.get("isinCode"),
            s.get("market"), s.get("securityType"), s.get("status"), s.get("currency"),
            s.get("listDate"), s.get("delistDate"), num(s.get("sharesOutstanding")),
            kmd.get("liquidationTrading"), kmd.get("krxTradingSuspended"),
            kmd.get("nxtTradingSuspended"), kmd.get("nxtSupported"),
            json.dumps(s, ensure_ascii=False),
        ))
    with conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO stock (symbol,name,english_name,isin_code,market,security_type,
                status,currency,list_date,delist_date,shares_outstanding,
                liquidation_trading,krx_trading_suspended,nxt_trading_suspended,
                nxt_supported,raw,updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
            ON CONFLICT (symbol) DO UPDATE SET
                name=EXCLUDED.name, english_name=EXCLUDED.english_name,
                isin_code=EXCLUDED.isin_code, market=EXCLUDED.market,
                security_type=EXCLUDED.security_type, status=EXCLUDED.status,
                currency=EXCLUDED.currency, list_date=EXCLUDED.list_date,
                delist_date=EXCLUDED.delist_date,
                shares_outstanding=EXCLUDED.shares_outstanding,
                liquidation_trading=EXCLUDED.liquidation_trading,
                krx_trading_suspended=EXCLUDED.krx_trading_suspended,
                nxt_trading_suspended=EXCLUDED.nxt_trading_suspended,
                nxt_supported=EXCLUDED.nxt_supported,
                raw=EXCLUDED.raw, updated_at=now()
        """, rows)
    conn.commit()
    return len(rows)


def set_watchlist(conn: psycopg.Connection, symbols: list[str]) -> int:
    """1분봉 수집 대상 지정. 512MB 상한 때문에 소수만 켠다."""
    with conn.cursor() as cur:
        cur.execute("UPDATE stock SET is_watched = false WHERE is_watched")
        cur.execute("UPDATE stock SET is_watched = true WHERE symbol = ANY(%s)", (symbols,))
        cur.execute("SELECT count(*) FROM stock WHERE is_watched")
        n = cur.fetchone()[0]
    conn.commit()
    return n


def watched_symbols(conn: psycopg.Connection) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT symbol FROM stock WHERE is_watched ORDER BY symbol")
        return [r[0] for r in cur.fetchall()]


# ── 2. 캔들 ──────────────────────────────────────────────────
def collect_candles(c, conn: psycopg.Connection, symbol: str,
                    interval: str, pages: int = 1) -> int:
    """`before` 커서로 과거를 거슬러 올라가며 적재.

    1회 200봉 제한이 있으므로 pages 만큼 반복한다.
    adjusted=true (수정주가) — 백테스팅 정확도에 필수.
    """
    total, before = 0, None
    for _ in range(pages):
        d = c.candles(symbol, interval, count=200, before=before, adjusted=True)
        candles = d.get("candles") or []
        if not candles:
            break
        rows = [(
            symbol, interval, _ts(x.get("timestamp")),
            num(x.get("openPrice")), num(x.get("highPrice")),
            num(x.get("lowPrice")), num(x.get("closePrice")),
            num(x.get("volume")), x.get("currency"),
        ) for x in candles if x.get("timestamp")]
        with conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO candle (symbol,interval,ts,open,high,low,close,volume,currency)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (symbol,interval,ts) DO UPDATE SET
                    open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low,
                    close=EXCLUDED.close, volume=EXCLUDED.volume
            """, rows)
        conn.commit()
        total += len(rows)
        before = d.get("nextBefore")
        if not before:
            break
    return total


# ── 3. 시장지표 ──────────────────────────────────────────────
INDICATORS = ["KOSPI", "KOSDAQ", "KR_BOND_2Y", "KR_BOND_3Y",
              "KR_BOND_5Y", "KR_BOND_10Y", "KR_BOND_20Y", "KR_BOND_30Y"]


def collect_indicator_candles(c, conn: psycopg.Connection) -> int:
    total = 0
    for sym in INDICATORS:
        # 국채는 일봉만 지원 (분봉 요청 시 400)
        d = c.indicator_candles(sym, interval="1d", count=200)
        rows = [(
            sym, "1d", _ts(x.get("timestamp")),
            num(x.get("openPrice")), num(x.get("highPrice")),
            num(x.get("lowPrice")), num(x.get("closePrice")),
        ) for x in (d.get("candles") or []) if x.get("timestamp")]
        with conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO market_indicator_candle (symbol,interval,ts,open,high,low,close)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (symbol,interval,ts) DO UPDATE SET close=EXCLUDED.close
            """, rows)
        conn.commit()
        total += len(rows)
    return total


def collect_investor_trading(c, conn: psycopg.Connection, interval: str = "1d") -> int:
    """⚠️ KOSPI/KOSDAQ '시장 전체' 단위만 제공된다. 개별종목 수급 아님."""
    total = 0
    for market in ("KOSPI", "KOSDAQ"):
        d = c.investor_trading(market, interval=interval, count=100)
        rows = []
        for rec in d.get("records") or []:
            upd = _ts(rec.get("updatedAt"))
            for who in ("individual", "foreigner", "institution", "otherCorporation"):
                blk = rec.get(who) or {}
                rows.append((
                    market, interval, rec["date"], who,
                    num(blk.get("buyAmount")), num(blk.get("sellAmount")),
                    json.dumps(blk.get("breakdown"), ensure_ascii=False)
                    if blk.get("breakdown") else None,
                    upd,
                ))
        with conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO investor_trading
                    (market,interval,trade_date,investor,buy_amount,sell_amount,breakdown,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (market,interval,trade_date,investor) DO UPDATE SET
                    buy_amount=EXCLUDED.buy_amount, sell_amount=EXCLUDED.sell_amount,
                    breakdown=EXCLUDED.breakdown, updated_at=EXCLUDED.updated_at
            """, rows)
        conn.commit()
        total += len(rows)
    return total


# ── 4. 포트폴리오 스냅샷 ─────────────────────────────────────
def collect_portfolio(c, conn: psycopg.Connection, on: date | None = None,
                      user_id: str | None = None) -> int:
    """일별 스냅샷.

    ⚠️ 토스는 '일간' 손익만 준다 (dailyProfitLoss).
       주간/월간 수익률은 이 스냅샷 누적으로만 계산 가능하다.
       → 하루 빠지면 그날 수익률에 구멍이 생긴다.
    """
    on = on or datetime.now(timezone.utc).astimezone().date()
    h = c.holdings()
    items = h.get("items") or []

    rows = [(
        user_id, on, it["symbol"], it.get("name"), it.get("marketCountry"), it.get("currency"),
        num(it.get("quantity")), num(it.get("averagePurchasePrice")), num(it.get("lastPrice")),
        num(dig(it, "marketValue", "purchaseAmount")),
        num(dig(it, "marketValue", "amount")),
        num(dig(it, "marketValue", "amountAfterCost")),
        num(dig(it, "profitLoss", "amount")),
        num(dig(it, "profitLoss", "amountAfterCost")),
        num(dig(it, "profitLoss", "rate")),
        num(dig(it, "profitLoss", "rateAfterCost")),
        num(dig(it, "dailyProfitLoss", "amount")),
        num(dig(it, "dailyProfitLoss", "rate")),
        num(dig(it, "cost", "commission")),
        num(dig(it, "cost", "tax")),
    ) for it in items]

    with conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO holding_snapshot (user_id,snapshot_date,symbol,name,market_country,currency,
                quantity,avg_price,last_price,purchase_amount,market_value,
                market_value_after_cost,pnl,pnl_after_cost,pnl_rate,pnl_rate_after_cost,
                daily_pnl,daily_pnl_rate,commission,tax)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (user_id,snapshot_date,symbol) DO UPDATE SET
                quantity=EXCLUDED.quantity, last_price=EXCLUDED.last_price,
                market_value=EXCLUDED.market_value, pnl=EXCLUDED.pnl,
                pnl_rate=EXCLUDED.pnl_rate, daily_pnl=EXCLUDED.daily_pnl,
                daily_pnl_rate=EXCLUDED.daily_pnl_rate,
                commission=EXCLUDED.commission, tax=EXCLUDED.tax
        """, rows)

        bp_krw = num(c.buying_power("KRW").get("cashBuyingPower"))
        try:
            bp_usd = num(c.buying_power("USD").get("cashBuyingPower"))
        except Exception:
            bp_usd = None
        fx = num(c.exchange_rate("USD", "KRW").get("rate"))

        vk = num(dig(h, "marketValue", "amount", "krw")) or 0
        vu = num(dig(h, "marketValue", "amount", "usd")) or 0
        pk = num(dig(h, "totalPurchaseAmount", "krw")) or 0
        pu = num(dig(h, "totalPurchaseAmount", "usd")) or 0
        lk = num(dig(h, "profitLoss", "amount", "krw")) or 0
        lu = num(dig(h, "profitLoss", "amount", "usd")) or 0
        dk = num(dig(h, "dailyProfitLoss", "amount", "krw")) or 0
        du = num(dig(h, "dailyProfitLoss", "amount", "usd")) or 0
        rate = fx or 0
        v_tot, p_tot = vk + vu * rate, pk + pu * rate
        computed = (v_tot / p_tot - 1) if p_tot else None

        cur.execute("""
            INSERT INTO account_snapshot (user_id,snapshot_date,
                total_purchase_krw,total_purchase_usd,market_value_krw,market_value_usd,
                market_value_total_krw,total_purchase_total_krw,
                pnl_krw,pnl_usd,pnl_total_krw,pnl_rate_api,pnl_rate_computed,
                daily_pnl_krw,daily_pnl_usd,daily_pnl_total_krw,daily_pnl_rate,
                cash_buying_power_krw,cash_buying_power_usd,exchange_rate,raw)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (user_id,snapshot_date) DO UPDATE SET
                market_value_krw=EXCLUDED.market_value_krw,
                market_value_usd=EXCLUDED.market_value_usd,
                market_value_total_krw=EXCLUDED.market_value_total_krw,
                total_purchase_total_krw=EXCLUDED.total_purchase_total_krw,
                pnl_total_krw=EXCLUDED.pnl_total_krw,
                pnl_rate_api=EXCLUDED.pnl_rate_api,
                pnl_rate_computed=EXCLUDED.pnl_rate_computed,
                daily_pnl_total_krw=EXCLUDED.daily_pnl_total_krw,
                daily_pnl_rate=EXCLUDED.daily_pnl_rate,
                cash_buying_power_krw=EXCLUDED.cash_buying_power_krw,
                exchange_rate=EXCLUDED.exchange_rate, raw=EXCLUDED.raw
        """, (
            user_id, on, pk, pu, vk, vu, v_tot, p_tot,
            lk, lu, lk + lu * rate,
            num(dig(h, "profitLoss", "rate")), computed,
            dk, du, dk + du * rate, num(dig(h, "dailyProfitLoss", "rate")),
            bp_krw, bp_usd, fx,
            json.dumps(h, ensure_ascii=False),
        ))
    conn.commit()
    return len(rows)


# ── 5. 참조 데이터 ───────────────────────────────────────────
def collect_commissions(c, conn: psycopg.Connection) -> int:
    rows = [(x.get("marketCountry"), json.dumps(x, ensure_ascii=False))
            for x in c.commissions()]
    with conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO commission (market,detail,fetched_at) VALUES (%s,%s,now())
            ON CONFLICT (market) DO UPDATE SET detail=EXCLUDED.detail, fetched_at=now()
        """, rows)
    conn.commit()
    return len(rows)


def collect_warnings(c, conn: psycopg.Connection, symbols: list[str]) -> int:
    total = 0
    for sym in symbols:
        try:
            ws = c.warnings(sym)
        except Exception as e:      # 종목 없음(404) 등은 건너뛴다
            log.debug("warnings(%s) 실패: %s", sym, e)
            continue
        rows = [(sym, w.get("type") or w.get("kind") or "UNKNOWN",
                 w.get("startDate"), w.get("endDate"),
                 json.dumps(w, ensure_ascii=False)) for w in ws if w.get("startDate")]
        if rows:
            with conn.cursor() as cur:
                cur.executemany("""
                    INSERT INTO stock_warning (symbol,kind,start_date,end_date,detail,fetched_at)
                    VALUES (%s,%s,%s,%s,%s,now())
                    ON CONFLICT (symbol,kind,start_date) DO UPDATE SET
                        end_date=EXCLUDED.end_date, detail=EXCLUDED.detail, fetched_at=now()
                """, rows)
            conn.commit()
            total += len(rows)
    return total


# ── 6. 장 운영 게이팅 ────────────────────────────────────────
def market_open(c, market: str = "KR") -> bool:
    """휴장일에 폴링을 돌리면 rate limit 만 낭비한다."""
    cal = c.market_calendar(market)
    today = cal.get("today") or {}
    if market == "KR":
        return today.get("integrated") is not None
    return any(today.get(k) for k in ("dayMarket", "preMarket", "regularMarket", "afterMarket"))


# ── 실행 이력 ────────────────────────────────────────────────
class job_run:
    """with job_run(conn, "name") as j: j.rows = 123"""

    def __init__(self, conn: psycopg.Connection, name: str):
        self.conn, self.name, self.rows, self.id = conn, name, 0, None

    def __enter__(self):
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO job_run (job_name) VALUES (%s) RETURNING id", (self.name,))
            self.id = cur.fetchone()[0]
        self.conn.commit()
        return self

    def __exit__(self, exc_type, exc, tb):
        # 예외가 났으면 트랜잭션이 깨져 있다. 롤백하지 않고 INSERT 를 시도하면
        # InFailedSqlTransaction 이 나서 **진짜 원인이 가려진다**(실제로 물렸던 버그).
        if exc is not None:
            try:
                self.conn.rollback()
            except Exception:
                pass
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE job_run SET ended_at=now(), ok=%s, rows=%s, error=%s WHERE id=%s",
                (exc is None, self.rows, str(exc)[:500] if exc else None, self.id),
            )
        self.conn.commit()
        if exc:
            log.error("[%s] 실패: %s", self.name, exc)
        else:
            log.info("[%s] 완료 — %s행", self.name, self.rows)
        return False


# ── 워커 outbound IP 보고 ────────────────────────────────────
def report_ip(conn: psycopg.Connection, source: str = "unknown") -> str | None:
    """이 서버의 공인 IP 를 DB 에 기록한다.

    사용자는 이 IP 를 자기 토스 계정의 허용 IP 에 등록해야 한다.
    호스팅이 IP 를 바꿔도 다음 실행에서 자동으로 갱신되므로,
    프론트는 항상 **현재 유효한 주소**를 안내할 수 있다.
    """
    import httpx as _httpx
    ip = None
    for svc in ("https://api.ipify.org", "https://ifconfig.me/ip",
                "https://icanhazip.com"):
        try:
            ip = _httpx.get(svc, timeout=10).text.strip()
            if ip:
                break
        except Exception:
            continue
    if not ip:
        log.warning("공인 IP 조회 실패 — 프론트 안내가 갱신되지 않습니다")
        return None

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO worker_ip (ip, source) VALUES (%s, %s)
            ON CONFLICT (ip) DO UPDATE SET
                last_seen = now(),
                run_count = worker_ip.run_count + 1,
                source    = COALESCE(EXCLUDED.source, worker_ip.source)
        """, (ip, source))
    conn.commit()
    log.info("outbound IP %s (%s) 보고됨 — 토스 허용 IP 에 등록 필요", ip, source)
    return ip
