"""종목 유니버스 수집 — 추천 후보의 근거.

왜 필요한가
  LLM 에 "ETF 추천해줘"를 시키면 기억에서 꺼내 지어낸다.
  상장폐지된 종목, 없는 티커, 틀린 이름이 나온다.
  → 토스 랭킹 API 로 **실재하는 종목만** 매일 적재하고,
    추천은 이 표 안에서만 고르게 강제한다.

ETF 식별
  토스 종목정보의 securityType 이 'ETF' 로 온다 (실측 확인).
  다만 자산군·지역·테마는 API 에 없어서 **이름에서 규칙으로** 유추한다.
  규칙 기반이라 완벽하지 않으므로 classified_by 에 출처를 남긴다.
"""

from __future__ import annotations

import logging
import re
from datetime import date

import psycopg

log = logging.getLogger(__name__)

RANK_TYPES = ["MARKET_TRADING_AMOUNT", "MARKET_TRADING_VOLUME",
              "TOP_GAINERS", "TOP_LOSERS"]


def collect_rankings(c, conn: psycopg.Connection, count: int = 30) -> int:
    """국내·미국 랭킹을 적재. 여기가 추천 후보 풀이 된다."""
    today = date.today()
    total = 0
    for market in ("KR", "US"):
        for rtype in RANK_TYPES:
            try:
                d = c.rankings(type_=rtype, market=market,
                               duration="1d", count=count)
            except Exception as e:
                log.warning("랭킹 %s/%s 실패: %s", market, rtype, str(e)[:100])
                continue
            rows = []
            for i, x in enumerate(d.get("rankings") or [], 1):
                price = x.get("price") or {}
                def num(v):
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        return None
                rows.append((today, market, rtype, "1d", i, x.get("symbol"),
                             num(price.get("last")), num(price.get("changeRate")),
                             num(x.get("tradingValue") or x.get("tradingAmount"))))
            if not rows:
                continue
            with conn.cursor() as cur:
                cur.executemany("""
                    INSERT INTO ranking_snapshot
                        (as_of,market,rank_type,duration,rank,symbol,
                         last_price,change_rate,trading_value)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (as_of,market,rank_type,duration,rank)
                    DO UPDATE SET symbol=EXCLUDED.symbol,
                                  last_price=EXCLUDED.last_price,
                                  change_rate=EXCLUDED.change_rate
                """, rows)
            conn.commit()
            total += len(rows)
    return total


def enrich_universe(c, conn: psycopg.Connection, limit: int = 200) -> int:
    """랭킹에 등장한 종목의 마스터 정보를 채운다 (ETF 여부 포함)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT r.symbol FROM ranking_snapshot r
            LEFT JOIN stock s ON s.symbol = r.symbol
            WHERE r.as_of > current_date - 7 AND s.symbol IS NULL
            LIMIT %s
        """, (limit,))
        missing = [r[0] for r in cur.fetchall()]
    if not missing:
        return 0
    from . import jobs as J
    return J.collect_stocks(c, conn, missing)


# ── ETF 분류 (규칙 기반) ─────────────────────────────────────
#  ⚠️ 이름 규칙이라 완벽하지 않다. 틀리면 etf_profile 을 직접 고치고
#     classified_by='manual' 로 표시하면 이후 갱신에서 보존된다.
_REGION = [
    (r"미국|US|S&P|나스닥|NASDAQ|다우", "US"),
    (r"중국|차이나|CSI|항셍", "CN"),
    (r"일본|니케이|JPX", "JP"),
    (r"인도|NIFTY", "IN"),
    (r"유로|유럽|EURO", "EU"),
    (r"선진국|신흥국|글로벌|월드|World|ACWI", "GLOBAL"),
]
#  ⚠️ 순서가 중요하다. 위에서부터 먼저 맞는 것이 이긴다.
#     또 단어 경계를 신경써야 한다 — "은"(silver)이 "은행"에 걸려
#     KODEX 은행이 commodity 로 분류됐던 적이 있다.
_THEME = [
    # 금리·현금성 먼저 (CD금리, 머니마켓은 채권 성격)
    (r"CD금리|머니마켓|MMF|초단기|금리액티브", "cash"),
    (r"국고채|국채|회사채|크레딧|채권|Bond|만기매칭", "bond"),
    # 섹터
    (r"반도체|SOX|시스템반도체", "semiconductor"),
    (r"2차전지|배터리|전기차|\bEV\b", "battery_ev"),
    (r"바이오|헬스케어|제약|의료", "healthcare"),
    (r"은행|금융|증권|보험", "financial"),
    (r"방산|우주항공|조선|기계", "industrial"),
    (r"인터넷|게임|엔터|미디어", "consumer_tech"),
    (r"AI|인공지능|빅테크|테크|Tech|소프트웨어", "tech"),
    (r"배당|고배당|Dividend|커버드콜", "dividend"),
    (r"리츠|REIT|부동산", "reit"),
    # 원자재 — '은'은 단독으로 쓰일 때만 (은행/은퇴 오탐 방지)
    (r"골드|Gold|(?<![가-힣])금(?![가-힣])|원유|WTI|구리|천연가스|(?<![가-힣])은(?![가-힣])", "commodity"),
    # 지수는 마지막 (숫자만 보고 잡히면 오탐이 많다)
    (r"코스피|KOSPI|코스닥|KOSDAQ|S&P|나스닥|NASDAQ|다우|지수|Index|\b200\b|\b500\b|\b100\b", "index"),
]


def _classify(name: str) -> dict:
    n = name or ""
    lev = 1.0
    if re.search(r"레버리지|2X|3X|Leverage", n, re.I):
        lev = 2.0
    if re.search(r"인버스|Inverse|숏|-1X", n, re.I):
        lev = -1.0
    if re.search(r"2X.*인버스|인버스.*2X", n, re.I):
        lev = -2.0

    region = next((v for p, v in _REGION if re.search(p, n, re.I)), "KR")
    theme = next((v for p, v in _THEME if re.search(p, n, re.I)), None)
    asset = ("bond" if theme in ("bond", "cash")
             else "commodity" if theme == "commodity" else "equity")
    return {
        "asset_class": asset, "region": region, "theme": theme,
        "leverage": lev,
        "is_hedged": bool(re.search(r"\(H\)|환헤지|헤지", n)),
    }


def classify_etfs(conn: psycopg.Connection) -> int:
    """security_type='ETF' 인 종목을 분류한다. manual 로 고친 건 건드리지 않는다."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT s.symbol, s.name FROM stock s
            LEFT JOIN etf_profile e ON e.symbol = s.symbol
            WHERE s.security_type = 'ETF'
              AND (e.symbol IS NULL OR e.classified_by = 'rule')
        """)
        targets = cur.fetchall()
        rows = []
        for sym, name in targets:
            c = _classify(name)
            rows.append((sym, name, c["asset_class"], c["region"], c["theme"],
                         c["leverage"], c["is_hedged"]))
        if rows:
            cur.executemany("""
                INSERT INTO etf_profile
                    (symbol,name,asset_class,region,theme,leverage,is_hedged,classified_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,'rule')
                ON CONFLICT (symbol) DO UPDATE SET
                    name=EXCLUDED.name, asset_class=EXCLUDED.asset_class,
                    region=EXCLUDED.region, theme=EXCLUDED.theme,
                    leverage=EXCLUDED.leverage, is_hedged=EXCLUDED.is_hedged,
                    updated_at=now()
                WHERE etf_profile.classified_by = 'rule'
            """, rows)
    conn.commit()
    return len(rows)


# ── 후보 조회 (LLM 에 넘길 '실재하는' 목록) ──────────────────
def candidates(conn: psycopg.Connection, user_id: str, limit: int = 40) -> dict:
    """추천 후보. 전부 DB 에 실재하는 종목이다.

    LLM 은 여기 있는 것만 고를 수 있다 — 지어내면 검증에서 걸러진다.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT symbol FROM holding_snapshot
            WHERE user_id=%s AND snapshot_date=(
                SELECT max(snapshot_date) FROM holding_snapshot WHERE user_id=%s)
        """, (user_id, user_id))
        held = {r[0] for r in cur.fetchall()}

        cur.execute("""
            SELECT e.symbol, e.name, e.asset_class, e.region, e.theme,
                   e.leverage, e.is_hedged
            FROM etf_profile e
            JOIN stock s ON s.symbol = e.symbol
            WHERE COALESCE(s.status,'ACTIVE') = 'ACTIVE'
              AND e.leverage BETWEEN 0.9 AND 1.1     -- 레버리지·인버스 제외
            ORDER BY e.region, e.theme NULLS LAST
            LIMIT %s
        """, (limit,))
        etfs = [dict(zip(("symbol", "name", "asset_class", "region",
                          "theme", "leverage", "is_hedged"), r))
                for r in cur.fetchall() if r[0] not in held]

        cur.execute("""
            SELECT r.symbol, COALESCE(s.name, r.symbol) AS name,
                   s.market, s.security_type,
                   count(*)::int AS appearances,
                   max(r.change_rate) AS best_change
            FROM ranking_snapshot r
            LEFT JOIN stock s ON s.symbol = r.symbol
            WHERE r.as_of > current_date - 7
              AND COALESCE(s.security_type,'STOCK') = 'STOCK'
            GROUP BY 1,2,3,4 ORDER BY 5 DESC LIMIT %s
        """, (limit,))
        stocks = [dict(zip(("symbol", "name", "market", "security_type",
                            "appearances", "best_change"), r))
                  for r in cur.fetchall() if r[0] not in held]

    return {"etfs": etfs, "stocks": stocks}
