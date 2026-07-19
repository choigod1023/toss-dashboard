"""외부 API 실측 — Gemini / 네이버 검색 / DART.

    python3 worker/probe_external.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import get_settings  # noqa: E402

S = get_settings()


def sec(t: str) -> None:
    print("\n" + "=" * 68)
    print(t)
    print("=" * 68)


# ── 1. Gemini ────────────────────────────────────────────────
sec("1. Gemini — 한국어 종목토론방 말투 감성분류")
POSTS = [
    "여기 지금 들어가면 물리는거 아님? 계속 흘러내리는데",
    "실적 발표 보고 확신함. 존버 간다 ㅋㅋ",
    "오늘 거래량 터지는거 보소 떡상각",
    "그냥 관망중입니다. 방향 정해지면 들어가려구요",
    "손절했습니다... 더 떨어질듯",
]
prompt = (
    "다음은 한국 주식 커뮤니티 게시글이다. 각 글의 투자 심리를 분류하라.\n"
    "label 은 positive/negative/neutral 중 하나, score 는 -1.0(매우 부정) ~ "
    "1.0(매우 긍정) 사이 실수.\n"
    "JSON 배열만 출력하라. 형식: "
    '[{"i":0,"label":"...","score":0.0}]\n\n'
    + "\n".join(f"{i}. {p}" for i, p in enumerate(POSTS))
)

url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
       f"{S.sentiment_model}:generateContent")
try:
    r = httpx.post(
        url,
        headers={"x-goog-api-key": S.gemini_api_key, "Content-Type": "application/json"},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0},
        },
        timeout=60.0,
    )
    print(f"HTTP {r.status_code}  model={S.sentiment_model}")
    if r.status_code == 200:
        d = r.json()
        txt = d["candidates"][0]["content"]["parts"][0]["text"]
        usage = d.get("usageMetadata", {})
        try:
            for item in json.loads(txt):
                i = item.get("i", 0)
                print(f"  [{item.get('label'):8} {item.get('score'):+.2f}] {POSTS[i][:34]}")
        except Exception:
            print("  파싱 실패, 원문:", txt[:300])
        pt, ct = usage.get("promptTokenCount"), usage.get("candidatesTokenCount")
        print(f"\n  토큰: 입력 {pt} / 출력 {ct}")
        if pt and ct:
            # Flash-Lite 유료 기준 $0.25 / $1.50 per 1M
            cost = pt / 1e6 * 0.25 + ct / 1e6 * 1.50
            print(f"  이 요청 비용(유료 환산): ${cost:.6f} → 게시글당 ${cost/len(POSTS):.6f}")
            print(f"  하루 1,000건 환산: ${cost/len(POSTS)*1000:.3f}/일 "
                  f"= ${cost/len(POSTS)*1000*30:.2f}/월  (무료 티어면 0원)")
    else:
        print("  " + r.text[:400])
except Exception as e:
    print("  ❌", str(e)[:200])


# ── 2. 네이버 검색 API ───────────────────────────────────────
sec("2. 네이버 검색 API — 헤더명·쿼터 실측")
nid = os.environ.get("NAVER_CLIENT_ID", "")
nsec = os.environ.get("NAVER_CLIENT_SECRET", "")
if not nid or not nsec:
    print("  ⏭️  NAVER_CLIENT_ID/SECRET 미설정 — 건너뜀")
else:
    try:
        r = httpx.get(
            "https://openapi.naver.com/v1/search/news.json",
            headers={"X-Naver-Client-Id": nid, "X-Naver-Client-Secret": nsec},
            params={"query": "삼성전자", "display": 3, "sort": "date"},
            timeout=30.0,
        )
        print(f"HTTP {r.status_code}  헤더 X-Naver-Client-Id/Secret "
              f"{'✅ 유효' if r.status_code == 200 else '❌'}")
        quota = {k: v for k, v in r.headers.items() if "rate" in k.lower() or "quota" in k.lower()}
        print(f"  쿼터 관련 응답 헤더: {quota or '(없음)'}")
        if r.status_code == 200:
            d = r.json()
            print(f"  전체 {d.get('total'):,}건 중 {len(d.get('items', []))}건 수신")
            for it in d.get("items", [])[:3]:
                title = it["title"].replace("<b>", "").replace("</b>", "")
                print(f"    · {title[:46]}  ({it.get('pubDate', '')[:16]})")
            print(f"  응답 필드: {', '.join(sorted(d.get('items', [{}])[0].keys()))}")
        else:
            print("  " + r.text[:300])
    except Exception as e:
        print("  ❌", str(e)[:200])


# ── 3. DART ─────────────────────────────────────────────────
sec("3. DART OpenAPI — 펀더멘털 소스")
if not S.dart_api_key:
    print("  ⏭️  DART_API_KEY 미설정 — 건너뜀")
else:
    try:
        # 삼성전자 고유번호 00126380 / 2025 사업보고서 주요 재무지표
        r = httpx.get(
            "https://opendart.fss.or.kr/api/fnlttSinglIndx.json",
            params={"crtfc_key": S.dart_api_key, "corp_code": "00126380",
                    "bsns_year": "2025", "reprt_code": "11011", "idx_cl_code": "M210000"},
            timeout=30.0,
        )
        d = r.json()
        print(f"HTTP {r.status_code}  status={d.get('status')} ({d.get('message')})")
        if d.get("status") == "000":
            for it in (d.get("list") or [])[:8]:
                print(f"    {it.get('idx_nm'):24} {it.get('idx_val')}")
        else:
            print("  ⚠️ 조회 실패 — 키/연도/보고서코드 확인 필요")
    except Exception as e:
        print("  ❌", str(e)[:200])

print("\n" + "=" * 68)
