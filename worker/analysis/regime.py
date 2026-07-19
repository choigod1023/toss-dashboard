"""시장 국면 — 공포탐욕지수.

외부 지수
  • CNN Fear & Greed (미국 증시)  — 비공식 dataviz 엔드포인트
  • Alternative.me (크립토)       — 공식 무료 API

국내는 공인된 공포탐욕지수가 **존재하지 않는다.**
그래서 CNN 이 공개한 방법론(여러 축을 0~100 으로 정규화해 평균)을
우리가 가진 데이터로 재현한 **자체 산출값**을 만든다.

⚠️ 'kr_composite' 는 우리가 만든 값이지 공인 지표가 아니다.
   그래서 구성요소를 전부 components 에 남겨 재현·검증이 가능하게 한다.
   구성요소가 부족하면 그 사실을 그대로 노출한다 (억지로 채우지 않는다).
"""

from __future__ import annotations

import logging
import statistics as st
from datetime import date

import httpx
import psycopg

log = logging.getLogger(__name__)

UA = {"User-Agent": "Mozilla/5.0 (Macintosh) toss-dashboard/0.1"}


def rating_of(score: float) -> str:
    if score < 25:
        return "extreme fear"
    if score < 45:
        return "fear"
    if score <= 55:
        return "neutral"
    if score <= 75:
        return "greed"
    return "extreme greed"


def _save(conn: psycopg.Connection, source: str, as_of: date,
          score: float, rating: str, components: dict | None = None) -> None:
    import json
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO market_regime (source, as_of, score, rating, components)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (source, as_of) DO UPDATE SET
                score=EXCLUDED.score, rating=EXCLUDED.rating,
                components=EXCLUDED.components, fetched_at=now()
        """, (source, as_of, round(score, 2), rating,
              json.dumps(components, ensure_ascii=False) if components else None))
    conn.commit()


# ── 외부 지수 ────────────────────────────────────────────────
def fetch_cnn(conn: psycopg.Connection) -> float | None:
    try:
        r = httpx.get("https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
                      headers=UA, timeout=25.0, follow_redirects=True)
        if r.status_code != 200:
            log.warning("CNN F&G HTTP %s", r.status_code)
            return None
        fg = r.json()["fear_and_greed"]
        score = float(fg["score"])
        _save(conn, "cnn", date.today(), score, fg.get("rating") or rating_of(score))
        return score
    except Exception as e:
        log.warning("CNN F&G 실패: %s", str(e)[:150])
        return None


def fetch_crypto(conn: psycopg.Connection) -> float | None:
    try:
        r = httpx.get("https://api.alternative.me/fng/?limit=1", headers=UA, timeout=20.0)
        if r.status_code != 200:
            return None
        d = r.json()["data"][0]
        score = float(d["value"])
        _save(conn, "crypto", date.today(), score,
              d.get("value_classification", "").lower() or rating_of(score))
        return score
    except Exception as e:
        log.warning("크립토 F&G 실패: %s", str(e)[:150])
        return None


# ── 국내 자체 산출 ───────────────────────────────────────────
def _pctile(x: float, series: list[float]) -> float:
    """series 안에서 x 의 백분위(0~100). 표본이 적으면 중립 50."""
    s = [v for v in series if v is not None]
    if len(s) < 10:
        return 50.0
    below = sum(1 for v in s if v < x)
    return max(0.0, min(100.0, below / len(s) * 100))


def compute_kr(conn: psycopg.Connection) -> tuple[float, dict] | None:
    """국내 공포탐욕 자체 산출.

    축 (각각 0~100, 높을수록 '탐욕')
      1) 모멘텀   — KOSPI 종가가 125일 이동평균 대비 어디인가
      2) 변동성   — 최근 20일 실현변동성의 역백분위 (변동성↑ = 공포)
      3) 수급     — 외국인+기관 최근 5일 순매수 방향
      4) 안전선호 — 국채 10Y 금리 20일 변화 (금리↓ = 안전자산 쏠림 = 공포)
      5) 강도     — 최근 20일 상승일 비율
    """
    with conn.cursor() as cur:
        cur.execute("""SELECT ts::date, close FROM market_indicator_candle
                       WHERE symbol='KOSPI' AND interval='1d'
                       ORDER BY ts DESC LIMIT 200""")
        kospi = [(d, float(c)) for d, c in cur.fetchall()]
        cur.execute("""SELECT ts::date, close FROM market_indicator_candle
                       WHERE symbol='KR_BOND_10Y' AND interval='1d'
                       ORDER BY ts DESC LIMIT 60""")
        bond = [(d, float(c)) for d, c in cur.fetchall()]
        cur.execute("""SELECT trade_date, investor, buy_amount - sell_amount
                       FROM investor_trading
                       WHERE market='KOSPI' AND interval='1d'
                         AND investor IN ('foreigner','institution')
                       ORDER BY trade_date DESC LIMIT 20""")
        flows = cur.fetchall()

    if len(kospi) < 30:
        log.info("KOSPI 캔들 부족 — kr_composite 생략")
        return None

    closes = [c for _, c in kospi]
    comp: dict[str, float | None] = {}

    # 1) 모멘텀
    ma = st.mean(closes[:125]) if len(closes) >= 125 else st.mean(closes)
    dev = (closes[0] / ma - 1) * 100
    comp["momentum"] = max(0.0, min(100.0, 50 + dev * 5))   # ±10% → 0~100

    # 2) 변동성 (역방향)
    rets = [closes[i] / closes[i + 1] - 1 for i in range(len(closes) - 1)]
    if len(rets) >= 40:
        vol20 = st.pstdev(rets[:20])
        hist = [st.pstdev(rets[i:i + 20]) for i in range(0, len(rets) - 20, 5)]
        comp["volatility"] = 100 - _pctile(vol20, hist)
    else:
        comp["volatility"] = None

    # 3) 수급
    if flows:
        net = sum(float(n) for _, _, n in flows[:10])
        comp["flow"] = max(0.0, min(100.0, 50 + (net / 1e12) * 25))  # ±2조 → 0~100
    else:
        comp["flow"] = None

    # 4) 안전자산 선호 (금리 하락 = 공포)
    if len(bond) >= 20:
        chg = bond[0][1] - bond[19][1]     # %p 변화
        comp["safe_haven"] = max(0.0, min(100.0, 50 + chg * 100))
    else:
        comp["safe_haven"] = None

    # 5) 상승일 비율
    if len(rets) >= 20:
        comp["breadth"] = sum(1 for r in rets[:20] if r > 0) / 20 * 100
    else:
        comp["breadth"] = None

    have = {k: v for k, v in comp.items() if v is not None}
    if not have:
        return None
    score = sum(have.values()) / len(have)
    detail = {
        "components": {k: round(v, 1) for k, v in have.items()},
        "missing": [k for k, v in comp.items() if v is None],
        "kospi_close": closes[0],
        "kospi_ma125": round(ma, 2),
        "note": "자체 산출값. 공인 지표 아님. CNN 방법론을 국내 데이터로 재현.",
    }
    _save(conn, "kr_composite", kospi[0][0], score, rating_of(score), detail)
    return score, detail


def collect_all(conn: psycopg.Connection) -> dict:
    out = {"cnn": fetch_cnn(conn), "crypto": fetch_crypto(conn)}
    kr = compute_kr(conn)
    out["kr_composite"] = kr[0] if kr else None
    return out
