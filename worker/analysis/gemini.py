"""Gemini 분석 파이프라인.

세 가지 일을 한다:
  1) 감성 분류      — 커뮤니티/뉴스 텍스트 → label + score  (대량, 저비용)
  2) 애널리스트 추출 — 뉴스 본문 → 증권사/투자의견/목표주가/논거 (구조화)
  3) 종목 브리핑     — 위 결과 + 수급 + 재무 → 자연어 3줄 요약

설계 원칙 (명세서 §7.3)
  LLM 은 '판단'이 아니라 '설명' 역할이다.
  숫자는 결정론적 코드가 계산하고, LLM 은 그걸 서술하거나
  비정형 텍스트에서 사실을 뽑아낼 때만 쓴다.

look-ahead bias 방지
  감성·추출 결과에는 원문의 발행 시각(as_of)을 그대로 박는다.
  나중에 재계산해서 덮어쓰지 않고 append 한다.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import psycopg

log = logging.getLogger(__name__)

API = "https://generativelanguage.googleapis.com/v1beta/models"


def _call(model: str, key: str, prompt: str, schema: dict | None = None,
          timeout: float = 120.0, retries: int = 4) -> Any:
    """429(무료 티어 쿼터)는 흔하다. 지수 백오프로 재시도한다."""
    body: dict[str, Any] = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }
    if schema:
        body["generationConfig"]["responseSchema"] = schema

    import time as _t
    last = ""
    for attempt in range(retries):
        r = httpx.post(f"{API}/{model}:generateContent",
                       headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                       json=body, timeout=timeout)
        if r.status_code == 200:
            break
        last = r.text[:300]
        if r.status_code == 429:
            # 응답이 retryDelay 를 주면 그걸 따르고, 없으면 지수 백오프
            delay = 2 ** attempt * 8
            try:
                for d_ in r.json().get("error", {}).get("details", []):
                    if "retryDelay" in str(d_):
                        delay = max(delay, int(str(d_["retryDelay"]).rstrip("s")) + 2)
            except Exception:
                pass
            log.warning("Gemini 429 — %d초 대기 후 재시도 (%d/%d)", delay, attempt + 1, retries)
            _t.sleep(delay)
            continue
        raise RuntimeError(f"Gemini {r.status_code}: {last}")
    else:
        raise RuntimeError(f"Gemini 429 재시도 소진: {last}")
    d = r.json()
    cand = (d.get("candidates") or [{}])[0]
    parts = (cand.get("content") or {}).get("parts") or []
    if not parts:
        raise RuntimeError(f"빈 응답 (finishReason={cand.get('finishReason')})")
    usage = d.get("usageMetadata", {})
    log.debug("gemini 토큰 in=%s out=%s",
              usage.get("promptTokenCount"), usage.get("candidatesTokenCount"))
    return json.loads(parts[0]["text"])


# ── 1. 감성 분류 ─────────────────────────────────────────────
SENTIMENT_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "i": {"type": "INTEGER"},
            "label": {"type": "STRING", "enum": ["positive", "negative", "neutral"]},
            "score": {"type": "NUMBER"},
            "confidence": {"type": "NUMBER"},
        },
        "required": ["i", "label", "score"],
    },
}

SENTIMENT_PROMPT = """다음은 한국/영어 주식 관련 게시글·뉴스다. 각 항목의 **투자 심리**를 분류하라.

기준
- label: positive(강세 기대) / negative(약세 우려) / neutral(정보전달·관망)
- score: -1.0(매우 부정) ~ 1.0(매우 긍정)
- confidence: 0.0~1.0
- 한국 커뮤니티 은어를 정확히 해석하라.
  떡상/가즈아/존버 = 강세, 물렸다/손절/흘러내린다 = 약세, 관망 = 중립
- 단순 사실 보도는 neutral 이다. 주가 방향에 대한 함의가 있을 때만 방향을 준다.

항목:
{items}"""


def score_sentiment(conn: psycopg.Connection, model: str, key: str,
                    batch: int = 20, limit: int = 200) -> int:
    """아직 채점되지 않은 글을 배치로 분류한다."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT p.id, p.posted_at, p.title, p.body
            FROM community_post p
            LEFT JOIN sentiment_score s
                   ON s.post_id = p.id AND s.posted_at = p.posted_at AND s.model = %s
            WHERE s.post_id IS NULL
            ORDER BY p.posted_at DESC
            LIMIT %s
        """, (model, limit))
        pending = cur.fetchall()

    if not pending:
        return 0

    done = 0
    for i in range(0, len(pending), batch):
        chunk = pending[i:i + batch]
        items = "\n".join(
            f"{n}. {(r[2] or '')} {(r[3] or '')[:300]}".strip()
            for n, r in enumerate(chunk)
        )
        try:
            out = _call(model, key, SENTIMENT_PROMPT.format(items=items),
                        SENTIMENT_SCHEMA)
        except Exception as e:
            log.warning("감성 배치 실패: %s", e)
            continue

        rows = []
        for o in out:
            n = int(o.get("i", -1))
            if not (0 <= n < len(chunk)):
                continue
            pid, posted = chunk[n][0], chunk[n][1]
            score = max(-1.0, min(1.0, float(o.get("score", 0))))
            rows.append((pid, posted, model, o.get("label", "neutral"),
                         score, o.get("confidence")))
        with conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO sentiment_score
                    (post_id, posted_at, model, label, score, confidence)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT DO NOTHING
            """, rows)
        conn.commit()
        done += len(rows)
    return done


# ── 2. 애널리스트 의견 구조화 추출 ───────────────────────────
ANALYST_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "i": {"type": "INTEGER"},
            "has_view": {"type": "BOOLEAN"},
            "broker": {"type": "STRING"},
            "analyst": {"type": "STRING"},
            "rating": {"type": "STRING"},
            "rating_norm": {"type": "STRING",
                            "enum": ["STRONG_BUY", "BUY", "HOLD", "SELL", "STRONG_SELL"]},
            "target_price": {"type": "NUMBER"},
            "currency": {"type": "STRING"},
            "thesis": {"type": "STRING"},
            "confidence": {"type": "NUMBER"},
        },
        "required": ["i", "has_view"],
    },
}

ANALYST_PROMPT = """다음 뉴스에서 **증권사 애널리스트의 투자의견**을 추출하라.

규칙 (중요)
- 기사에 실제로 적힌 것만 뽑아라. 추측하거나 지어내지 마라.
- 애널리스트 의견이 없는 단순 시황·사실 보도면 has_view=false 로 하고 나머지는 비워라.
- target_price 는 숫자만 (예: "12만원" → 120000, "$250" → 250).
- rating_norm 은 원문 의견을 5단계로 정규화하라.
  매수/Buy/비중확대 → BUY, 강력매수/Strong Buy → STRONG_BUY,
  중립/보유/Hold/시장수익률 → HOLD, 매도/비중축소 → SELL
- thesis 는 목표가 근거를 1~2문장으로. 기사에 근거가 없으면 비워라.
- confidence 는 추출 확신도 0.0~1.0.

기사:
{items}"""


def extract_analyst_views(conn: psycopg.Connection, model: str, key: str,
                          symbol: str, batch: int = 8, limit: int = 40) -> int:
    """뉴스 → 애널리스트 의견 테이블.

    증권사 리포트 원문은 저작권이 있어 저장하지 않는다.
    '목표주가/투자의견' 같은 사실 정보만 구조화해 남긴다.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT p.id, p.posted_at, p.title, p.body, p.url
            FROM community_post p
            WHERE p.symbol = %s AND p.source = 'naver_news'
              AND NOT EXISTS (
                    SELECT 1 FROM analyst_view a
                    WHERE a.symbol = p.symbol AND a.source_url = p.url)
            ORDER BY p.posted_at DESC LIMIT %s
        """, (symbol, limit))
        rows = cur.fetchall()

    if not rows:
        return 0

    found = 0
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        items = "\n\n".join(
            f"[{n}] 제목: {r[2]}\n본문: {(r[3] or '')[:700]}"
            for n, r in enumerate(chunk)
        )
        try:
            out = _call(model, key, ANALYST_PROMPT.format(items=items), ANALYST_SCHEMA)
        except Exception as e:
            log.warning("애널리스트 추출 실패: %s", e)
            continue

        ins = []
        for o in out:
            n = int(o.get("i", -1))
            if not (0 <= n < len(chunk)) or not o.get("has_view"):
                continue
            tp = o.get("target_price")
            # 투자의견도 목표가도 없으면 단순 언급이지 '의견'이 아니다 — 버린다
            if not o.get("rating_norm") and not tp:
                continue
            ins.append((
                symbol, chunk[n][1], o.get("broker"), o.get("analyst"),
                o.get("rating"), o.get("rating_norm"),
                float(tp) if tp else None, o.get("currency") or "KRW",
                o.get("thesis"), chunk[n][4], chunk[n][2],
                model, o.get("confidence"),
            ))
        if ins:
            with conn.cursor() as cur:
                cur.executemany("""
                    INSERT INTO analyst_view (symbol,as_of,broker,analyst,rating,
                        rating_norm,target_price,currency,thesis,source_url,
                        source_title,extracted_by,confidence)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT DO NOTHING
                """, ins)
            conn.commit()
            found += len(ins)
    return found


# ── 3. 종목 브리핑 ───────────────────────────────────────────
BRIEF_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "headline": {"type": "STRING"},
        "bullets": {"type": "ARRAY", "items": {"type": "STRING"}},
        "stance": {"type": "STRING",
                   "enum": ["positive", "negative", "mixed", "neutral"]},
    },
    "required": ["headline", "bullets", "stance"],
}

BRIEF_PROMPT = """아래는 특정 종목에 대해 **수집된 사실 데이터**다.
투자자가 30초 안에 상황을 파악할 수 있는 브리핑을 작성하라.

규칙
- 주어진 데이터에 있는 내용만 써라. 없는 사실을 지어내지 마라.
- headline: 지금 이 종목의 상황을 한 문장으로.
- bullets: 근거 3개. 각 항목은 한 문장. 반드시 구체적 숫자를 포함하라.
- stance: 수집된 정보 전반의 논조 (positive/negative/mixed/neutral).
  ⚠️ 이건 '투자 추천'이 아니라 '수집된 정보의 논조 요약'이다.
- 데이터가 빈약하면 그 사실을 솔직히 적어라.

종목: {name} ({symbol})

[가격]
{price}

[애널리스트 의견]
{analyst}

[뉴스·커뮤니티 감성]
{sentiment}

[최근 뉴스 제목]
{titles}

[재무지표]
{fin}"""


def build_briefing(conn: psycopg.Connection, model: str, key: str,
                   symbol: str) -> bool:
    """수집된 데이터를 종합해 브리핑 1건 생성."""
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM stock WHERE symbol=%s", (symbol,))
        row = cur.fetchone()
        name = row[0] if row else symbol

        cur.execute("""
            SELECT close, ts FROM candle
            WHERE symbol=%s AND interval='1d' ORDER BY ts DESC LIMIT 60
        """, (symbol,))
        candles = cur.fetchall()

        cur.execute("""
            SELECT broker, rating_norm, target_price, thesis, as_of
            FROM analyst_view WHERE symbol=%s
            ORDER BY as_of DESC LIMIT 8
        """, (symbol,))
        views = cur.fetchall()

        cur.execute("""
            SELECT s.label, count(*), avg(s.score)
            FROM sentiment_score s
            JOIN community_post p ON p.id=s.post_id AND p.posted_at=s.posted_at
            WHERE p.symbol=%s AND s.posted_at > now() - interval '14 days'
            GROUP BY s.label
        """, (symbol,))
        sent = cur.fetchall()

        cur.execute("""
            SELECT title FROM community_post
            WHERE symbol=%s ORDER BY posted_at DESC LIMIT 12
        """, (symbol,))
        titles = [r[0] for r in cur.fetchall()]

        cur.execute("""
            SELECT idx_name, idx_value FROM financial_indicator
            WHERE symbol=%s ORDER BY bsns_year DESC LIMIT 10
        """, (symbol,))
        fin = cur.fetchall()

    if not candles:
        log.info("[%s] 캔들 없음 — 브리핑 생략", symbol)
        return False

    last = float(candles[0][0])
    chg60 = (last / float(candles[-1][0]) - 1) * 100 if len(candles) > 1 else 0
    price_txt = (f"현재가 {last:,.0f} / 60일 등락 {chg60:+.1f}% / "
                 f"기간 고가 {max(float(c[0]) for c in candles):,.0f} "
                 f"저가 {min(float(c[0]) for c in candles):,.0f}")

    analyst_txt = "\n".join(
        f"- {v[0] or '증권사미상'}: {v[1] or '-'} 목표가 {v[2]:,.0f}" % {} if False else
        f"- {v[0] or '증권사미상'}: {v[1] or '-'}"
        + (f" 목표가 {float(v[2]):,.0f}" if v[2] else "")
        + (f" — {v[3]}" if v[3] else "")
        for v in views
    ) or "(수집된 애널리스트 의견 없음)"

    tot = sum(s[1] for s in sent) or 0
    sentiment_txt = (
        "\n".join(f"- {s[0]}: {s[1]}건 (평균 {float(s[2]):+.2f})" for s in sent)
        + f"\n- 합계 {tot}건 (최근 14일)"
    ) if sent else "(감성 분석된 글 없음)"

    fin_txt = "\n".join(f"- {f[0]}: {float(f[1])}" for f in fin) or "(재무지표 없음)"
    titles_txt = "\n".join(f"- {t}" for t in titles) or "(뉴스 없음)"

    out = _call(model, key, BRIEF_PROMPT.format(
        name=name, symbol=symbol, price=price_txt, analyst=analyst_txt,
        sentiment=sentiment_txt, titles=titles_txt, fin=fin_txt), BRIEF_SCHEMA)

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO briefing (symbol, as_of, headline, bullets, stance, inputs, model)
            VALUES (%s, date_trunc('hour', now()), %s, %s, %s, %s, %s)
            ON CONFLICT (symbol, as_of) DO UPDATE SET
                headline=EXCLUDED.headline, bullets=EXCLUDED.bullets,
                stance=EXCLUDED.stance, inputs=EXCLUDED.inputs
        """, (symbol, out["headline"], json.dumps(out["bullets"], ensure_ascii=False),
              out["stance"],
              json.dumps({"analyst_views": len(views), "sentiment_posts": tot,
                          "news_titles": len(titles), "fin_indicators": len(fin),
                          "price_change_60d_pct": round(chg60, 2)}, ensure_ascii=False),
              model))
    conn.commit()
    return True
