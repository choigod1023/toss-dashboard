"""맞춤 전략 — 포트폴리오 진단 + LLM 서술.

역할 분리 (중요)
  • 숫자는 전부 **결정론적 코드**가 계산한다 (compute_metrics).
    집중도·변동성·베타·낙폭·승률은 LLM 이 만지지 않는다.
  • LLM 은 그 숫자를 **해석·서술**하고, 사용자 성향에 맞춰
    점검 항목을 제안한다. 미래 가격을 예측하게 두지 않는다.

왜 이렇게 하나
  LLM 에 "이 주식 오를까?" 를 시키면 그럴듯한 헛소리가 나온다.
  대신 "네 포트폴리오는 반도체에 78% 쏠려 있고 변동성이 시장의 1.6배다"
  같은 **검증 가능한 사실**을 주고 그것을 설명하게 하면 쓸모가 있다.
"""

from __future__ import annotations

import json
import logging
import statistics as st
from datetime import date

import psycopg

from .gemini import _call

log = logging.getLogger(__name__)


# ── 1. 결정론적 포트폴리오 진단 ──────────────────────────────
def compute_metrics(conn: psycopg.Connection, on: date | None = None,
                    user_id: str | None = None) -> dict | None:
    with conn.cursor() as cur:
        cur.execute("SELECT max(snapshot_date) FROM holding_snapshot WHERE user_id=%s", (user_id,))
        row = cur.fetchone()
        on = on or (row[0] if row else None)
        if not on:
            return None

        cur.execute("""
            SELECT symbol, name, market_country, currency, quantity,
                   market_value, pnl, pnl_rate, daily_pnl, avg_price, last_price
            FROM holding_snapshot WHERE snapshot_date=%s AND user_id=%s
        """, (on, user_id))
        holds = cur.fetchall()
        cur.execute("""
            SELECT market_value_total_krw, cash_buying_power_krw,
                   market_value_krw, market_value_usd, exchange_rate
            FROM account_snapshot WHERE snapshot_date=%s AND user_id=%s
        """, (on, user_id))
        acc = cur.fetchone()

    if not holds or not acc:
        return None

    total, cash, v_krw, v_usd, fx = (float(x or 0) for x in acc)
    fx = fx or 1

    # 종목별 원화 환산 평가액 (통화별 소계이므로 직접 환산해야 한다)
    vals = []
    for h in holds:
        mv = float(h[5] or 0)
        vals.append(mv * (fx if h[3] == "USD" else 1))
    gross = sum(vals) or 1

    weights = [v / gross for v in vals]
    hhi = sum(w * w for w in weights)
    top_i = weights.index(max(weights))

    # 변동성·베타 — 일봉에서 계산
    def rets_of(symbol: str, n: int = 60) -> list[float]:
        with conn.cursor() as cur:
            cur.execute("""SELECT close FROM candle WHERE symbol=%s AND interval='1d'
                           ORDER BY ts DESC LIMIT %s""", (symbol, n + 1))
            cs = [float(r[0]) for r in cur.fetchall()]
        return [cs[i] / cs[i + 1] - 1 for i in range(len(cs) - 1)]

    with conn.cursor() as cur:
        cur.execute("""SELECT close FROM market_indicator_candle
                       WHERE symbol='KOSPI' AND interval='1d'
                       ORDER BY ts DESC LIMIT 61""")
        kc = [float(r[0]) for r in cur.fetchall()]
    kospi_rets = [kc[i] / kc[i + 1] - 1 for i in range(len(kc) - 1)]

    per_symbol = {}
    for h, w in zip(holds, weights):
        r = rets_of(h[0])
        per_symbol[h[0]] = {
            "name": h[1], "weight": round(w, 4), "rets": r,
            "vol_ann": round(st.pstdev(r[:20]) * (252 ** 0.5), 4) if len(r) >= 20 else None,
            "pnl_rate": float(h[7] or 0),
        }

    # 포트폴리오 일간 수익률 (가중합)
    n = min([len(v["rets"]) for v in per_symbol.values()] + [len(kospi_rets)] or [0])
    port_rets, beta, mdd = [], None, None
    if n >= 20:
        for i in range(n):
            port_rets.append(sum(per_symbol[h[0]]["rets"][i] * w
                                 for h, w in zip(holds, weights)))
        kr = kospi_rets[:n]
        var_k = st.pvariance(kr)
        if var_k > 0:
            mk, mp = st.mean(kr), st.mean(port_rets)
            cov = sum((a - mp) * (b - mk) for a, b in zip(port_rets, kr)) / n
            beta = cov / var_k
        # 최대 낙폭 (누적 수익 곡선, 최신→과거 순이므로 뒤집는다)
        curve, peak, worst = 1.0, 1.0, 0.0
        for r in reversed(port_rets):
            curve *= (1 + r)
            peak = max(peak, curve)
            worst = min(worst, curve / peak - 1)
        mdd = worst

    wins = sum(1 for h in holds if float(h[7] or 0) > 0)
    best = max(holds, key=lambda h: float(h[7] or 0))
    worst_h = min(holds, key=lambda h: float(h[7] or 0))

    m = {
        "as_of": str(on),
        "n_positions": len(holds),
        "hhi": round(hhi, 4),
        "top_weight": round(max(weights), 4),
        "top_symbol": holds[top_i][0],
        "top_name": holds[top_i][1],
        "krw_weight": round(v_krw / gross, 4) if gross else None,
        "usd_weight": round(v_usd * fx / gross, 4) if gross else None,
        "cash_weight": round(cash / (gross + cash), 4) if (gross + cash) else None,
        "port_vol_20d": round(st.pstdev(port_rets[:20]) * (252 ** 0.5), 4) if len(port_rets) >= 20 else None,
        "kospi_vol_20d": round(st.pstdev(kospi_rets[:20]) * (252 ** 0.5), 4) if len(kospi_rets) >= 20 else None,
        "beta_kospi": round(beta, 4) if beta is not None else None,
        "max_drawdown_60d": round(mdd, 4) if mdd is not None else None,
        "win_rate": round(wins / len(holds), 4),
        "best_symbol": best[0], "best_name": best[1], "best_rate": float(best[7] or 0),
        "worst_symbol": worst_h[0], "worst_name": worst_h[1], "worst_rate": float(worst_h[7] or 0),
        "total_krw": round(gross),
        "cash_krw": round(cash),
        "positions": [
            {"symbol": h[0], "name": h[1], "country": h[2],
             "weight": round(w, 4), "pnl_rate": float(h[7] or 0),
             "vol_ann": per_symbol[h[0]]["vol_ann"]}
            for h, w in zip(holds, weights)
        ],
    }

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO portfolio_metrics (user_id,as_of,n_positions,hhi,top_weight,krw_weight,
                usd_weight,cash_weight,port_vol_20d,kospi_vol_20d,beta_kospi,
                max_drawdown_60d,win_rate,best_symbol,worst_symbol,detail)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (user_id,as_of) DO UPDATE SET
                hhi=EXCLUDED.hhi, top_weight=EXCLUDED.top_weight,
                port_vol_20d=EXCLUDED.port_vol_20d, beta_kospi=EXCLUDED.beta_kospi,
                max_drawdown_60d=EXCLUDED.max_drawdown_60d, win_rate=EXCLUDED.win_rate,
                detail=EXCLUDED.detail
        """, (user_id, on, m["n_positions"], m["hhi"], m["top_weight"], m["krw_weight"],
              m["usd_weight"], m["cash_weight"], m["port_vol_20d"], m["kospi_vol_20d"],
              m["beta_kospi"], m["max_drawdown_60d"], m["win_rate"],
              m["best_symbol"], m["worst_symbol"],
              json.dumps(m, ensure_ascii=False)))
    conn.commit()
    return m


# ── 2. LLM 서술 ──────────────────────────────────────────────
STRATEGY_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "investor_type": {"type": "STRING"},
        "regime": {"type": "STRING"},
        "diagnosis": {"type": "STRING"},
        "actions": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "title": {"type": "STRING"},
                    "why": {"type": "STRING"},
                    "caution": {"type": "STRING"},
                },
                "required": ["title", "why"],
            },
        },
        "themes": {"type": "ARRAY", "items": {"type": "STRING"}},
        "watchlist": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "symbol": {"type": "STRING"},
                    "name": {"type": "STRING"},
                    "reason": {"type": "STRING"},
                },
                "required": ["name", "reason"],
            },
        },
        "risks": {"type": "ARRAY", "items": {"type": "STRING"}},
        "expert_views": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "role": {"type": "STRING",
                             "enum": ["리스크 매니저", "포트폴리오 매니저", "퀀트 애널리스트"]},
                    "reads": {"type": "STRING"},      # 이 데이터를 어떻게 읽는가
                    "would_do": {"type": "STRING"},   # 그래서 뭘 하는가
                    "blind_spot": {"type": "STRING"}, # 개인투자자가 놓치는 지점
                },
                "required": ["role", "reads", "would_do", "blind_spot"],
            },
        },
    },
    "required": ["investor_type", "regime", "diagnosis", "actions", "themes",
                 "risks", "expert_views"],
}

PROMPT = """너는 개인 투자자의 포트폴리오를 진단하는 어시스턴트다.
아래는 **실제로 계산된 수치**다. 여기 없는 사실을 지어내지 마라.

절대 규칙
- 미래 가격을 예측하지 마라. "오를 것" "목표가" 같은 표현 금지.
- 수치를 인용할 땐 반드시 아래 데이터의 실제 값을 써라.
- 이건 본인이 쓰는 개인 도구다. 일반론 대신 **이 사람의 숫자**를 말해라.
- 데이터가 없는 항목은 "데이터 부족"이라고 솔직히 써라.

작성할 것
- investor_type: 이 사람의 투자 성향을 한 문장으로. 보유 구성·집중도·변동성 근거로.
  (예: "소수 대형 반도체주에 집중하고 환노출을 감수하는 고변동성 성향")
- regime: 지금 시장 국면 한 문장. 공포탐욕지수와 수급을 근거로.
- diagnosis: 이 포트폴리오의 상태 2~3문장. 반드시 구체 숫자 포함.
- actions: 지금 점검할 것 2~4개. title(무엇을) / why(왜, 숫자 근거) / caution(주의점).
  매수·매도 지시가 아니라 **점검 항목**으로 써라.
- themes: 보유 종목에서 읽히는 테마 2~4개 (예: "메모리 반도체", "미국 소형 성장주").
- watchlist: themes 와 같은 결이면서 **아래 후보 목록에 실제로 있는** 종목만 골라라.
  **국내에 한정하지 마라.** 해외 종목도 같은 비중으로 검토하고,
  보유 구성이 한쪽에 쏠려 있으면 반대쪽도 후보에 포함하라.
  없으면 빈 배열로 둬라. reason 에는 왜 같은 결인지 + 최근 언급/등락 근거를 써라.
  ⚠️ 후보 목록에 없는 종목을 지어내면 안 된다.
- risks: 이 사람이 놓치기 쉬운 위험 2~3개.
- expert_views: **세 직군이 각자의 훈련된 관점으로** 이 포트폴리오를 볼 때.
  반드시 세 개 다 작성하고, 서로 다른 것을 보게 하라 (같은 말 반복 금지).

  · 리스크 매니저 — 손실 통제가 직업이다. 집중도·변동성·유동성·꼬리위험을 본다.
    "얼마나 벌까"가 아니라 "최악에 얼마나 잃나"를 묻는다.
  · 포트폴리오 매니저 — 자산배분이 직업이다. 상관관계·리밸런싱 규칙·
    현금 포지션의 옵션가치·비중 상한을 본다.
  · 퀀트 애널리스트 — 통계가 직업이다. 표본 수가 결론을 지지하는지,
    생존편향·과최적화·우연을 의심한다. 데이터가 부족하면 부족하다고 말한다.

  각 항목:
    reads      — 위 숫자를 자기 관점에서 어떻게 읽는가 (구체 수치 인용)
    would_do   — 그래서 실무에서 무엇을 하는가 (기관의 표준 관행 기준)
    blind_spot — 개인투자자가 이 지점에서 흔히 놓치는 것

  ⚠️ 여기서도 가격 예측은 금지다. '프로세스'를 말하게 하라.

[내 포트폴리오 — 계산값]
{metrics}

[시장 국면]
{regime}

[내 보유 종목 여론]
{sentiment}

[관찰 후보 — 국내·해외 통합]
  · 앞부분: 뉴스·커뮤니티 언급 종목 (국내 + 해외)
  · 뒷부분: SEC 13F 기준 복수 기관이 보유 중인 미국 종목
    (분기 공시라 후행 지표다. '지금 산다'는 뜻이 아니라 '기관이 담고 있다'는 사실이다)
{candidates}
"""


def build_strategy(conn: psycopg.Connection, model: str, key: str,
                   user_id: str | None = None) -> dict | None:
    m = compute_metrics(conn, user_id=user_id)
    if not m:
        log.info("포트폴리오 데이터 부족 — 전략 생성 생략")
        return None

    with conn.cursor() as cur:
        cur.execute("""SELECT source, score, rating, components FROM market_regime
                       WHERE as_of > current_date - 7 ORDER BY source, as_of DESC""")
        regs = cur.fetchall()
        cur.execute("""
            SELECT p.symbol, s2.name, count(*), round(avg(sc.score)::numeric,3)
            FROM sentiment_score sc
            JOIN community_post p ON p.id=sc.post_id AND p.posted_at=sc.posted_at
            LEFT JOIN stock s2 ON s2.symbol=p.symbol
            WHERE p.symbol IS NOT NULL AND sc.posted_at > now() - interval '30 days'
            GROUP BY 1,2 ORDER BY 3 DESC""")
        sent = cur.fetchall()
        # ── 관찰 후보 (국내·해외 통합) ──
        #  세 갈래로 모은다. 국내 뉴스만 보면 후보가 국내 대형주뿐이 된다.
        #   1) 뉴스·커뮤니티에 언급된 종목 (국내 + 해외)
        #   2) SEC 13F — 기관이 실제로 담고 있는 미국 종목
        #      (분기 공시라 후행이지만, '누가 얼마나' 담았는지는 사실이다)
        cur.execute("""
            WITH held AS (
                SELECT symbol FROM holding_snapshot
                WHERE user_id=%s AND snapshot_date=(
                    SELECT max(snapshot_date) FROM holding_snapshot WHERE user_id=%s)
            )
            SELECT p.symbol, COALESCE(s.name, p.symbol) AS name,
                   count(*)::int AS mentions,
                   round(avg(sc.score)::numeric, 2) AS sent
            FROM community_post p
            LEFT JOIN stock s ON s.symbol = p.symbol
            LEFT JOIN sentiment_score sc
                   ON sc.post_id = p.id AND sc.posted_at = p.posted_at
            WHERE p.symbol IS NOT NULL
              AND p.posted_at > now() - interval '14 days'
              AND p.symbol NOT IN (SELECT symbol FROM held)
            GROUP BY 1,2 ORDER BY 3 DESC LIMIT 12
        """, (user_id, user_id))
        cands = cur.fetchall()

        cur.execute("""
            WITH held AS (
                SELECT symbol FROM holding_snapshot
                WHERE user_id=%s AND snapshot_date=(
                    SELECT max(snapshot_date) FROM holding_snapshot WHERE user_id=%s)
            )
            SELECT ticker,
                   max(issuer) AS issuer,
                   count(DISTINCT institution)::int AS n_inst,
                   sum(value_usd) AS total_usd,
                   string_agg(DISTINCT institution, ', ') AS who
            FROM institution_holding
            WHERE ticker IS NOT NULL
              AND period > current_date - interval '400 days'
              AND ticker NOT IN (SELECT symbol FROM held)
            GROUP BY ticker
            HAVING count(DISTINCT institution) >= 2
            ORDER BY count(DISTINCT institution) DESC, sum(value_usd) DESC
            LIMIT 15
        """, (user_id, user_id))
        inst_cands = cur.fetchall()

    regime_txt = "\n".join(
        f"- {r[0]}: {float(r[1]):.1f}/100 ({r[2]})"
        + (f"  구성={json.dumps(r[3].get('components'), ensure_ascii=False)}"
           if r[3] and r[3].get("components") else "")
        for r in regs
    ) or "(공포탐욕 데이터 없음)"

    sent_txt = "\n".join(
        f"- {s[1] or s[0]}: {s[2]}건, 평균 감성 {float(s[3]):+.2f}" for s in sent
    ) or "(감성 데이터 없음)"

    cand_lines = [
        f"- {c[1]} ({c[0]}): 최근 14일 언급 {c[2]}건"
        + (f", 평균 감성 {float(c[3]):+.2f}" if c[3] is not None else "")
        for c in cands
    ] + [
        f"- {c[1][:30]} ({c[0]}): 기관 {c[2]}곳 보유, 합계 ${float(c[3])/1e9:.1f}B — {c[4][:60]}"
        for c in inst_cands
    ]
    cand_txt = "\n".join(cand_lines) or "(후보 없음 — watchlist 는 빈 배열로 두어라)"

    out = _call(model, key, PROMPT.format(
        metrics=json.dumps(m, ensure_ascii=False, indent=1),
        regime=regime_txt, sentiment=sent_txt, candidates=cand_txt,
    ), STRATEGY_SCHEMA, timeout=180.0)

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO strategy_note (user_id, as_of, regime, diagnosis, actions, risks, inputs, model)
            VALUES (%s, date_trunc('hour', now()), %s,%s,%s,%s,%s,%s)
            ON CONFLICT (user_id, as_of) DO UPDATE SET
                regime=EXCLUDED.regime, diagnosis=EXCLUDED.diagnosis,
                actions=EXCLUDED.actions, risks=EXCLUDED.risks, inputs=EXCLUDED.inputs
        """, (user_id, out.get("regime"), out.get("diagnosis"),
              json.dumps(out.get("actions"), ensure_ascii=False),
              json.dumps(out.get("risks"), ensure_ascii=False),
              json.dumps({"metrics": m, "investor_type": out.get("investor_type"),
                          "themes": out.get("themes"), "watchlist": out.get("watchlist"),
                          "expert_views": out.get("expert_views"),
                          "regime_sources": [r[0] for r in regs]}, ensure_ascii=False),
              model))
    conn.commit()
    return out
