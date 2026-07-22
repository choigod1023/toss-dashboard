"""리밸런싱 제안.

역할 분리 (이 프로젝트 전체의 원칙)
  • **목표 비중은 코드가 계산한다.** 집중도 상한, 현금 버퍼, 통화 노출은
    규칙으로 정하고 그 규칙을 명시적으로 남긴다(guardrails).
  • **LLM 은 왜 그런지 설명만 한다.** 비중 숫자를 LLM 이 만들면
    검증이 불가능하고 매번 달라진다.

왜 규칙 기반인가
  "최적 비중"을 최적화로 구하는 건 표본이 부족할 때 추정오차를 과적합한다.
  (DeMiguel et al. 2009 — 균등가중이 표본 외에서 최적화를 이긴다)
  보유 3종목·60일 데이터로 공분산 최적화를 돌리는 건 근거가 없다.
  → 방어 가능한 단순 규칙을 쓰고, 규칙 자체를 화면에 드러낸다.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import psycopg

from decimal import Decimal

from .gemini import _call


def _json(o) -> str:
    """psycopg 가 numeric 을 Decimal 로 주는데 json 이 못 다룬다."""
    return json.dumps(o, ensure_ascii=False,
                      default=lambda v: float(v) if isinstance(v, Decimal) else str(v))
from .strategy import compute_metrics

log = logging.getLogger(__name__)

# ── 가드레일 (전부 설정값. 근거를 함께 적는다) ────────────────
GUARDRAILS = {
    "max_single_weight": 0.30,
    "max_single_weight_why": "한 종목이 30%를 넘으면 그 종목의 악재가 계좌 전체를 좌우한다",
    "min_cash_weight": 0.05,
    "min_cash_weight_why": "하락장에서 대응할 여력. 0이면 선택지가 없다",
    "min_positions": 5,
    "min_positions_why": "분산 효과는 5종목 부근까지 급격히 커지고 이후 완만해진다",
    "max_turnover_per_round": 0.20,
    "max_turnover_per_round_why": "한 번에 크게 갈아타면 타이밍 리스크와 비용이 커진다",
    "note": "이 값들은 방어 가능한 관행이지 최적값이 아니다. 취향에 맞게 조정할 것.",
}


def compute_targets(conn: psycopg.Connection, user_id: str) -> dict | None:
    """현재 비중 → 목표 비중. 결정론적이다.

    절차
      1) 상한(30%) 초과분을 깎는다
      2) 현금이 5% 미만이면 채운다
      3) 남는 몫을 '보유 종목 수가 적을수록 더' 신규 후보에 배분한다
    """
    m = compute_metrics(conn, user_id=user_id)
    if not m:
        return None

    positions = m["positions"]          # symbol, name, weight, pnl_rate, vol_ann
    cash_w = m.get("cash_weight") or 0.0
    invest_w = 1.0 - cash_w             # 주식에 들어가 있는 비중

    g = GUARDRAILS
    targets, freed = [], 0.0

    # 1) 집중도 상한
    for p in positions:
        w = p["weight"] * invest_w      # 현금 포함 전체 대비
        cap = g["max_single_weight"]
        if w > cap:
            freed += w - cap
            targets.append({**p, "current": round(w, 4), "target": cap,
                            "action": "trim",
                            "reason": f"단일 종목 상한 {cap:.0%} 초과 (현재 {w:.1%})"})
        else:
            targets.append({**p, "current": round(w, 4), "target": round(w, 4),
                            "action": "hold", "reason": ""})

    # 2) 현금 버퍼
    cash_target = cash_w
    if cash_w < g["min_cash_weight"]:
        need = g["min_cash_weight"] - cash_w
        take = min(need, freed)
        freed -= take
        cash_target = cash_w + take
        if take < need:
            # 상한 초과분만으로 부족하면 비중 큰 것부터 비례 축소
            short = need - take
            pool = [t for t in targets if t["target"] > 0.05]
            tot = sum(t["target"] for t in pool) or 1
            for t in pool:
                cut = short * (t["target"] / tot)
                t["target"] = round(t["target"] - cut, 4)
                if t["action"] == "hold":
                    t["action"] = "trim"
                    t["reason"] = f"현금 버퍼 {g['min_cash_weight']:.0%} 확보를 위한 비례 축소"
            cash_target = g["min_cash_weight"]

    n_missing = max(0, g["min_positions"] - len(positions))
    return {
        "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "metrics": {k: m[k] for k in
                    ("n_positions", "hhi", "top_weight", "krw_weight",
                     "usd_weight", "cash_weight", "port_vol_20d",
                     "kospi_vol_20d", "beta_kospi", "max_drawdown_60d")},
        "current_cash_weight": round(cash_w, 4),
        "target_cash_weight": round(cash_target, 4),
        "positions": targets,
        "free_to_allocate": round(freed, 4),
        "slots_to_fill": n_missing,
        "guardrails": g,
    }


# ── LLM 서술 ─────────────────────────────────────────────────
SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "summary": {"type": "STRING"},
        "moves": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "symbol": {"type": "STRING"},
                    "name": {"type": "STRING"},
                    "action": {"type": "STRING",
                               "enum": ["trim", "hold", "add", "new"]},
                    "target_weight": {"type": "NUMBER"},
                    "why": {"type": "STRING"},
                },
                "required": ["name", "action", "why"],
            },
        },
        "theme_gaps": {"type": "ARRAY", "items": {"type": "STRING"}},
        "cautions": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["summary", "moves", "cautions"],
}

PROMPT = """개인 투자자의 리밸런싱 계획을 설명하라.

절대 규칙
- **목표 비중은 이미 계산되어 있다.** 네가 새로 정하지 마라. 주어진 값을 쓰고
  그것이 왜 그런지 가드레일을 근거로 설명하라.
- 신규 편입 후보는 **아래 후보 목록에 실제로 있는 것만** 골라라.
  목록에 없는 종목·ETF 를 지어내면 안 된다. 티커도 목록의 것을 그대로 써라.
- 미래 가격을 예측하지 마라. "오를 것", "목표가" 금지.
- 레버리지·인버스는 후보에서 이미 제외되어 있다. 다시 끌어들이지 마라.
- 이 사람의 실제 숫자를 인용하라. 일반론만 쓰지 마라.

작성할 것
- summary: 지금 포트폴리오의 구조적 문제와 조정 방향 2~3문장. 숫자 포함.
- moves: 보유 종목별 조치(trim/hold) + 빈 슬롯이 있으면 후보에서 골라 new.
  target_weight 는 계산된 값을 그대로. new 는 남는 몫을 나눠 배분하되
  단일 종목 상한을 넘지 마라. why 에는 가드레일 근거를 적어라.
- theme_gaps: 현재 포트폴리오에 없는 자산군·지역·테마 2~3개.
  후보 목록의 asset_class/region/theme 를 근거로.
- cautions: 실행 시 주의점 2~3개 (거래비용, 세금, 타이밍 분산 등).

[계산된 리밸런싱 계획 — 이 숫자를 쓸 것]
{plan}

[신규 편입 후보 — 여기 있는 것만 고를 수 있다]
ETF:
{etfs}

개별 종목(최근 랭킹 등장):
{stocks}
"""


def build_plan(conn: psycopg.Connection, model: str, key: str,
               user_id: str) -> dict | None:
    plan = compute_targets(conn, user_id)
    if not plan:
        log.info("포트폴리오 데이터 부족 — 리밸런싱 생략")
        return None

    from collectors.universe import candidates
    cand = candidates(conn, user_id, limit=40)

    etf_txt = "\n".join(
        f"- {e['name']} ({e['symbol']}): {e['asset_class']}/{e['region']}"
        + (f"/{e['theme']}" if e["theme"] else "")
        + (" [환헤지]" if e["is_hedged"] else "")
        for e in cand["etfs"]) or "(없음)"
    stk_txt = "\n".join(
        f"- {s['name']} ({s['symbol']}, {s['market'] or '?'}): 최근 7일 랭킹 {s['appearances']}회"
        for s in cand["stocks"][:25]) or "(없음)"

    out = _call(model, key, PROMPT.format(
        plan=_json(plan),
        etfs=etf_txt, stocks=stk_txt), SCHEMA, timeout=180.0)

    # ── 검증: 지어낸 종목을 걸러낸다 ──
    valid = {e["symbol"] for e in cand["etfs"]} | {s["symbol"] for s in cand["stocks"]}
    valid |= {p["symbol"] for p in plan["positions"]}
    kept, dropped = [], []
    for mv in out.get("moves", []):
        sym = (mv.get("symbol") or "").strip()
        if mv.get("action") in ("new", "add") and sym not in valid:
            dropped.append(mv.get("name") or sym)
            continue
        kept.append(mv)
    if dropped:
        log.warning("후보 목록에 없는 종목 %d건 제거: %s", len(dropped), dropped)
    out["moves"] = kept
    out["dropped_hallucinations"] = dropped

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO rebalance_plan
                (user_id, as_of, current, target, candidates, rationale, guardrails, model)
            VALUES (%s, date_trunc('hour', now()), %s,%s,%s,%s,%s,%s)
            ON CONFLICT (user_id, as_of) DO UPDATE SET
                current=EXCLUDED.current, target=EXCLUDED.target,
                candidates=EXCLUDED.candidates, rationale=EXCLUDED.rationale,
                guardrails=EXCLUDED.guardrails
        """, (user_id,
              _json(plan["positions"]),
              _json(out),
              _json(cand),
              out.get("summary"),
              _json(plan["guardrails"]),
              model))
    conn.commit()
    return {**out, "plan": plan}
