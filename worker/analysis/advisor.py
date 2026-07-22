"""대화형 상담.

구조
  질문 → **SQL 로 사실 수집** → LLM 이 그 사실만 가지고 답변

왜 이렇게 하나
  LLM 에 질문만 던지면 수익률·비중·변동성을 기억이나 상상에서 만들어낸다.
  숫자가 틀린 상담은 없느니만 못하다.
  → 답변에 쓸 수 있는 숫자를 코드가 먼저 뽑아 넘기고,
    그 밖의 수치는 쓰지 못하게 막는다. facts 를 저장해 사후 검증도 가능하다.

하지 않는 것
  가격 예측. "오를까요?"에 "오릅니다"라고 답하는 순간
  나머지 모든 답변의 신뢰가 같이 무너진다.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal

import psycopg

from .gemini import _call

log = logging.getLogger(__name__)
MAX_HISTORY = 8          # 직전 대화 몇 턴을 문맥으로 넣을지


def _j(o) -> str:
    return json.dumps(o, ensure_ascii=False, default=lambda v:
                      float(v) if isinstance(v, Decimal) else str(v))


def gather_facts(conn: psycopg.Connection, user_id: str) -> dict:
    """답변에 쓸 수 있는 사실 전부. 여기 없는 숫자는 LLM 이 쓸 수 없다."""
    f: dict = {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT symbol, name, market_country, quantity, avg_price, last_price,
                   market_value, pnl, pnl_rate, daily_pnl_rate, commission, tax
            FROM holding_snapshot
            WHERE user_id=%s AND snapshot_date=(
                SELECT max(snapshot_date) FROM holding_snapshot WHERE user_id=%s)
            ORDER BY market_value DESC NULLS LAST
        """, (user_id, user_id))
        cols = ("symbol", "name", "country", "quantity", "avg_price", "last_price",
                "market_value", "pnl", "pnl_rate", "daily_pnl_rate", "commission", "tax")
        f["holdings"] = [dict(zip(cols, r)) for r in cur.fetchall()]

        cur.execute("""
            SELECT market_value_total_krw, total_purchase_total_krw, pnl_total_krw,
                   pnl_rate_computed, daily_pnl_total_krw, daily_pnl_rate,
                   cash_buying_power_krw, market_value_krw, market_value_usd,
                   exchange_rate, snapshot_date
            FROM account_snapshot WHERE user_id=%s
            ORDER BY snapshot_date DESC LIMIT 1
        """, (user_id,))
        r = cur.fetchone()
        if r:
            f["account"] = dict(zip(
                ("total_krw", "purchase_krw", "pnl_krw", "pnl_rate",
                 "daily_pnl_krw", "daily_pnl_rate", "cash_krw",
                 "kr_value", "us_value_usd", "fx", "as_of"), r))

        cur.execute("""
            SELECT n_positions, hhi, top_weight, krw_weight, usd_weight,
                   cash_weight, port_vol_20d, kospi_vol_20d, beta_kospi,
                   max_drawdown_60d, win_rate
            FROM portfolio_metrics WHERE user_id=%s ORDER BY as_of DESC LIMIT 1
        """, (user_id,))
        r = cur.fetchone()
        if r:
            f["metrics"] = dict(zip(
                ("n_positions", "hhi", "top_weight", "krw_weight", "usd_weight",
                 "cash_weight", "vol_ann", "kospi_vol_ann", "beta",
                 "mdd_60d", "win_rate"), r))

        cur.execute("""
            SELECT source, score, rating FROM market_regime
            WHERE as_of > current_date - 5 ORDER BY source, as_of DESC
        """)
        f["fear_greed"] = [dict(zip(("source", "score", "rating"), r))
                           for r in cur.fetchall()]

        cur.execute("""
            SELECT p.symbol, count(*)::int AS n, round(avg(s.score)::numeric,3) AS avg
            FROM sentiment_score s
            JOIN community_post p ON p.id=s.post_id AND p.posted_at=s.posted_at
            WHERE p.symbol IS NOT NULL AND s.posted_at > now() - interval '30 days'
            GROUP BY p.symbol
        """)
        f["sentiment"] = [dict(zip(("symbol", "posts", "avg_score"), r))
                          for r in cur.fetchall()]

        cur.execute("""
            SELECT symbol, headline, stance FROM briefing b
            WHERE as_of = (SELECT max(as_of) FROM briefing WHERE symbol = b.symbol)
        """)
        f["briefings"] = [dict(zip(("symbol", "headline", "stance"), r))
                          for r in cur.fetchall()]

        cur.execute("""
            SELECT symbol, broker, rating_norm, target_price, thesis
            FROM analyst_view ORDER BY as_of DESC LIMIT 8
        """)
        f["analyst_views"] = [dict(zip(
            ("symbol", "broker", "rating", "target_price", "thesis"), r))
            for r in cur.fetchall()]

        # 보유 미국 종목을 담은 기관 (13F, 후행 지표)
        cur.execute("""
            SELECT ticker, count(DISTINCT institution)::int, sum(value_usd), max(period)
            FROM institution_holding
            WHERE ticker IN (SELECT symbol FROM holding_snapshot
                             WHERE user_id=%s AND market_country='US'
                               AND snapshot_date=(SELECT max(snapshot_date)
                                                  FROM holding_snapshot WHERE user_id=%s))
            GROUP BY ticker
        """, (user_id, user_id))
        f["institutions"] = [dict(zip(("ticker", "n_inst", "total_usd", "period"), r))
                             for r in cur.fetchall()]

        # 60일 가격 추이 (수준만, 시계열 전체는 넣지 않는다)
        cur.execute("""
            SELECT symbol,
                   max(close) FILTER (WHERE rn=1)  AS last,
                   max(close) FILTER (WHERE rn=60) AS d60,
                   max(close) AS high, min(close) AS low
            FROM (SELECT symbol, close,
                         row_number() OVER (PARTITION BY symbol ORDER BY ts DESC) rn
                  FROM candle WHERE interval='1d'
                    AND symbol IN (SELECT symbol FROM holding_snapshot
                                   WHERE user_id=%s AND snapshot_date=(
                                     SELECT max(snapshot_date) FROM holding_snapshot
                                     WHERE user_id=%s))) t
            WHERE rn <= 60 GROUP BY symbol
        """, (user_id, user_id))
        f["price_range_60d"] = [dict(zip(("symbol", "last", "d60_ago", "high", "low"), r))
                                for r in cur.fetchall()]

        cur.execute("""
            SELECT rationale, target FROM rebalance_plan
            WHERE user_id=%s ORDER BY as_of DESC LIMIT 1
        """, (user_id,))
        r = cur.fetchone()
        if r:
            f["latest_rebalance"] = {"summary": r[0], "plan": r[1]}
    return f


SYSTEM = """너는 개인 투자자의 포트폴리오를 함께 보는 어시스턴트다.

지켜야 할 것
1. **아래 [사실] 에 있는 숫자만 인용하라.** 없는 수치를 만들어내지 마라.
   모르면 "그 데이터는 아직 수집되지 않았습니다"라고 솔직히 말하라.
2. **가격을 예측하지 마라.** "오를 것", "목표가", "지금이 저점" 금지.
   대신 현재 상태를 설명하고, 무엇을 점검해야 하는지 말하라.
3. 매수·매도를 지시하지 마라. 판단은 사용자가 한다.
   "이렇게 하면 이런 위험이 줄고 대신 이런 걸 포기한다"처럼 트레이드오프로 말하라.
4. 일반론 말고 **이 사람의 숫자**로 답하라.
5. 13F 기관 보유는 분기말 기준이고 최대 45일 뒤 공시된다 — 후행 지표임을 밝혀라.
6. 감성 점수는 뉴스·커뮤니티 여론이지 주가 예측력이 검증된 지표가 아니다.
7. 한국어로, 군더더기 없이. 표가 나으면 표를 써라.

[사실]
{facts}
"""


def ask(conn: psycopg.Connection, model: str, key: str,
        user_id: str, question: str) -> dict:
    """질문 하나에 답한다. 대화 이력을 문맥으로 넣는다."""
    facts = gather_facts(conn, user_id)

    with conn.cursor() as cur:
        cur.execute("""
            SELECT role, content FROM advice_thread
            WHERE user_id=%s ORDER BY created_at DESC LIMIT %s
        """, (user_id, MAX_HISTORY))
        history = list(reversed(cur.fetchall()))

    convo = "\n".join(f"{'사용자' if r=='user' else '어시스턴트'}: {c}"
                      for r, c in history)
    prompt = (SYSTEM.format(facts=_j(facts))
              + (f"\n[이전 대화]\n{convo}\n" if convo else "")
              + f"\n[질문]\n{question}\n\n"
                "JSON 으로 답하라: "
                '{"answer": "마크다운 답변", '
                '"used_facts": ["인용한 사실의 키"], '
                '"followups": ["이어서 물어볼 만한 질문 2~3개"]}')

    out = _call(model, key, prompt, {
        "type": "OBJECT",
        "properties": {
            "answer": {"type": "STRING"},
            "used_facts": {"type": "ARRAY", "items": {"type": "STRING"}},
            "followups": {"type": "ARRAY", "items": {"type": "STRING"}},
        },
        "required": ["answer"],
    }, timeout=180.0)

    with conn.cursor() as cur:
        cur.execute("""INSERT INTO advice_thread (user_id, role, content)
                       VALUES (%s,'user',%s)""", (user_id, question))
        cur.execute("""INSERT INTO advice_thread (user_id, role, content, facts, model)
                       VALUES (%s,'assistant',%s,%s,%s)""",
                    (user_id, out["answer"],
                     _j({"used": out.get("used_facts"), "snapshot": facts}), model))
    conn.commit()
    return out
