"""토스증권 Open API 실측 — 명세서 v2 §8 미확인 항목 확정용.

읽기 전용. 주문(POST /api/v1/orders)은 절대 호출하지 않는다.

    python3 worker/probe_toss.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import get_settings  # noqa: E402

S = get_settings()
BASE = S.toss_base_url
KR = "005930"   # 삼성전자
US = "AAPL"

RL_KEYS = ("X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset", "Retry-After")


def rl(h) -> str:
    got = {k: h.get(k) for k in RL_KEYS if h.get(k) is not None}
    return " ".join(f"{k.split('-')[-1]}={v}" for k, v in got.items()) or "-"


def sec(t: str) -> None:
    print("\n" + "=" * 68)
    print(t)
    print("=" * 68)


def unwrap(body):
    """BFF 공통 envelope 이면 result 를 꺼낸다."""
    if isinstance(body, dict) and "result" in body:
        return body["result"]
    return body


def main() -> None:
    client = httpx.Client(base_url=BASE, timeout=20.0)

    # ── 1. 토큰 ──────────────────────────────────────────────
    sec("1. 인증 — POST /oauth2/token")
    r = client.post("/oauth2/token", data={
        "grant_type": "client_credentials",
        "client_id": S.toss_client_id,
        "client_secret": S.toss_client_secret,
    })
    print(f"HTTP {r.status_code}   rate-limit: {rl(r.headers)}")
    if r.status_code != 200:
        sys.exit(f"토큰 발급 실패: {r.text[:500]}")
    tok = r.json()
    exp = tok.get("expires_in") or 0
    print(f"token_type={tok.get('token_type')}  expires_in={exp}초 ({exp/3600:.1f}시간)")
    print(f"refresh_token: {'제공됨' if tok.get('refresh_token') else '없음 (스펙과 일치)'}")
    client.headers["Authorization"] = f"Bearer {tok['access_token']}"

    # ── 2. 호가 depth (§8-2) ────────────────────────────────
    sec("2. 호가 depth 확정 — GET /api/v1/orderbook")
    for sym in (KR, US):
        r = client.get("/api/v1/orderbook", params={"symbol": sym})
        if r.status_code != 200:
            print(f"  [{sym}] HTTP {r.status_code} {r.text[:200]}")
            continue
        d = unwrap(r.json())
        a, b = d.get("asks", []), d.get("bids", [])
        print(f"  [{sym}] 매도 {len(a)}단계 / 매수 {len(b)}단계   ts={d.get('timestamp')}")
        print(f"        → {'✅ 10호가' if len(a) == 10 else f'⚠️ {len(a)}호가'}   {rl(r.headers)}")
        if a:
            print(f"        최우선 매도 {a[0].get('price')} ({a[0].get('volume')}) / "
                  f"매수 {b[0].get('price') if b else '-'} ({b[0].get('volume') if b else '-'})")

    # ── 3. 시세 지연 (§8-3) ─────────────────────────────────
    sec("3. 시세 지연 여부 — timestamp vs 현재시각 (★핵심)")
    now = datetime.now(timezone.utc)
    r = client.get("/api/v1/prices", params={"symbols": f"{KR},{US}"})
    print(f"HTTP {r.status_code}  호출시각(UTC) {now.isoformat(timespec='seconds')}  {rl(r.headers)}")
    if r.status_code == 200:
        for it in unwrap(r.json()) or []:
            ts = it.get("timestamp")
            lag = "?"
            if ts:
                try:
                    lag = f"{(now - datetime.fromisoformat(ts)).total_seconds():,.0f}초"
                except Exception:
                    pass
            print(f"  {it.get('symbol'):8} last={it.get('lastPrice'):>12} "
                  f"{it.get('currency')}  ts={ts}  경과={lag}")
        print("\n  판정: 장중 호출 시 경과가 수 초 이내 → 실시간")
        print("        900~1200초(15~20분) → 지연시세")
        print("  ※ 오늘은 일요일(휴장)이라 마지막 체결 시각이 찍힌다. 장중 재확인 필요.")
    else:
        print(r.text[:300])

    # ── 4. 캔들 ─────────────────────────────────────────────
    sec("4. 캔들 — interval 별 실제 응답")
    for iv in ("1m", "1d"):
        r = client.get("/api/v1/candles", params={
            "symbol": KR, "interval": iv, "count": 200, "adjusted": "true"})
        if r.status_code != 200:
            print(f"  [{iv}] HTTP {r.status_code} {r.text[:200]}")
            continue
        d = unwrap(r.json())
        cs = d.get("candles", [])
        print(f"  [{iv}] {len(cs)}봉  nextBefore={d.get('nextBefore')}  {rl(r.headers)}")
        if cs:
            print(f"        최신 {cs[0].get('timestamp')} close={cs[0].get('closePrice')}")
            print(f"        최古 {cs[-1].get('timestamp')}")

    # ── 5. 계좌 ─────────────────────────────────────────────
    sec("5. 계좌 — GET /api/v1/accounts (ACCOUNT 1 TPS)")
    r = client.get("/api/v1/accounts")
    print(f"HTTP {r.status_code}  {rl(r.headers)}")
    acc_seq = None
    if r.status_code == 200:
        for a in unwrap(r.json()) or []:
            print(f"  accountSeq={a.get('accountSeq')}  type={a.get('accountType')}")
            acc_seq = acc_seq or a.get("accountSeq")
    else:
        print(r.text[:300])

    if acc_seq:
        client.headers["X-Tossinvest-Account"] = str(acc_seq)

        sec("6. 실수수료율 — GET /api/v1/commissions")
        r = client.get("/api/v1/commissions")
        print(f"HTTP {r.status_code}  {rl(r.headers)}")
        print("  " + str(unwrap(r.json()) if r.status_code == 200 else r.text[:300])[:600])

        sec("7. 보유자산 — GET /api/v1/holdings")
        r = client.get("/api/v1/holdings")
        print(f"HTTP {r.status_code}  {rl(r.headers)}")
        if r.status_code == 200:
            d = unwrap(r.json())
            items = d.get("items", []) if isinstance(d, dict) else d
            print(f"  보유 종목 {len(items)}개")
            keys = sorted(items[0].keys()) if items else []
            if keys:
                print(f"  필드: {', '.join(keys)}")
        else:
            print(r.text[:300])

        sec("8. 매수가능금액 — GET /api/v1/buying-power")
        r = client.get("/api/v1/buying-power")
        print(f"HTTP {r.status_code}  {rl(r.headers)}")
        print("  " + str(unwrap(r.json()) if r.status_code == 200 else r.text[:200])[:300])

    # ── 9. 시장지표 카탈로그 ────────────────────────────────
    sec("9. 시장지표 — 미국 지수 미지원 확인")
    r = client.get("/api/v1/market-indicators/prices", params={"symbols": "KOSPI,KOSDAQ"})
    print(f"  KOSPI,KOSDAQ → HTTP {r.status_code}  {rl(r.headers)}")
    if r.status_code == 200:
        for it in unwrap(r.json()) or []:
            print(f"    {it.get('symbol'):10} {it.get('lastPrice')}  ts={it.get('timestamp')}")
    r = client.get("/api/v1/market-indicators/prices", params={"symbols": "SPX"})
    print(f"  SPX(미국지수) → HTTP {r.status_code} "
          f"{'→ 예상대로 미지원, 외부 소스 필요' if r.status_code == 400 else ''}")
    print("    " + r.text[:200])

    # ── 10. 장 운영 ─────────────────────────────────────────
    sec("10. 장 운영 캘린더 — 폴링 게이팅용")
    for mk in ("KR", "US"):
        r = client.get(f"/api/v1/market-calendar/{mk}")
        print(f"  [{mk}] HTTP {r.status_code}  " + str(unwrap(r.json()))[:260] if r.status_code == 200
              else f"  [{mk}] HTTP {r.status_code} {r.text[:150]}")

    print("\n" + "=" * 68)
    print("완료 — 주문 API는 호출하지 않았습니다.")
    print("=" * 68)
    client.close()


if __name__ == "__main__":
    main()
