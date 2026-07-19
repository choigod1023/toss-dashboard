"""Rate limit 관리 — 응답 헤더 기반 적응형.

토스 문서 원문: "구체적인 한도 수치는 운영 상황에 따라 사전 공지 없이
조정될 수 있으며, 현재 허용 한도는 응답 헤더로 확인할 수 있습니다."

→ 아래 표는 '초기값'일 뿐이다. 실제 운영은 X-RateLimit-Limit 을 읽어
  런타임에 갱신하고, 429 는 Retry-After 를 그대로 따른다.

한도는 **클라이언트 × API 그룹** 단위이므로 그룹별로 따로 관리한다.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field

# 2026-07-19 실측으로 문서 표와 일치 확인 (헤더 X-RateLimit-Limit)
DEFAULT_TPS: dict[str, int] = {
    "AUTH": 5,
    "ACCOUNT": 1,          # 가장 빡빡 — 앱 시작 시 1회 호출 후 캐시할 것
    "ASSET": 5,
    "STOCK": 5,
    "MARKET_INFO": 3,
    "MARKET_DATA": 10,     # 호가·현재가·체결·상하한가가 이 한도를 '공유'
    "MARKET_DATA_CHART": 5,
    "RANKING": 5,
    "MARKET_INDICATOR": 10,
    "MARKET_INDICATOR_CHART": 5,
    "ORDER": 6,            # 09:00~09:10 KST 에는 3
    "ORDER_HISTORY": 5,
    "ORDER_INFO": 6,       # 09:00~09:10 KST 에는 3
    "CONDITIONAL_ORDER": 5,
    "CONDITIONAL_ORDER_HISTORY": 10,
}

# 장 초반 한도가 절반으로 떨어지는 그룹 (문서 명시)
PEAK_GROUPS = {"ORDER", "ORDER_INFO"}
PEAK_TPS = 3


def _is_peak_window() -> bool:
    """09:00~09:10 KST 인가. 하필 장 초반 변동성 구간과 겹친다."""
    kst = time.gmtime(time.time() + 9 * 3600)
    return kst.tm_hour == 9 and kst.tm_min < 10


@dataclass
class _Bucket:
    tps: int
    calls: deque[float] = field(default_factory=deque)
    lock: threading.Lock = field(default_factory=threading.Lock)
    blocked_until: float = 0.0


class RateLimiter:
    """그룹별 초당 호출 수를 강제하는 슬라이딩 윈도우 리미터."""

    def __init__(self, safety: float = 0.9) -> None:
        # safety: 한도의 90%만 쓴다. 헤더 갱신 지연·시계 오차 대비 여유.
        self._safety = safety
        self._buckets: dict[str, _Bucket] = {
            g: _Bucket(tps=t) for g, t in DEFAULT_TPS.items()
        }
        self._observations: list[tuple] = []

    def _bucket(self, group: str) -> _Bucket:
        if group not in self._buckets:
            self._buckets[group] = _Bucket(tps=DEFAULT_TPS.get(group, 3))
        return self._buckets[group]

    def effective_tps(self, group: str) -> float:
        tps = self._bucket(group).tps
        if group in PEAK_GROUPS and _is_peak_window():
            tps = min(tps, PEAK_TPS)
        return max(1.0, tps * self._safety)

    def acquire(self, group: str) -> None:
        """호출 직전에 부른다. 필요하면 잔다."""
        b = self._bucket(group)
        while True:
            with b.lock:
                now = time.monotonic()
                if now < b.blocked_until:          # 429 로 막힌 상태
                    wait = b.blocked_until - now
                else:
                    limit = self.effective_tps(group)
                    while b.calls and now - b.calls[0] >= 1.0:
                        b.calls.popleft()
                    if len(b.calls) < limit:
                        b.calls.append(now)
                        return
                    wait = 1.0 - (now - b.calls[0])
            time.sleep(max(wait, 0.01))

    def observe(self, group: str, headers, status: int) -> None:
        """응답 헤더로 한도를 갱신하고 429 를 처리한다."""
        b = self._bucket(group)
        limit = headers.get("X-RateLimit-Limit")
        remaining = headers.get("X-RateLimit-Remaining")
        reset = headers.get("X-RateLimit-Reset")

        if limit is not None:
            try:
                new = int(limit)
                if new > 0 and new != b.tps:
                    b.tps = new           # 서버가 조정했으면 따라간다
            except ValueError:
                pass

        if status == 429:
            # 문서 권장: Retry-After 값만큼 대기 (맹목적 백오프보다 우선)
            retry = headers.get("Retry-After") or reset or "1"
            try:
                delay = float(retry)
            except ValueError:
                delay = 1.0
            with b.lock:
                b.blocked_until = time.monotonic() + delay

        def _i(v):
            try:
                return int(float(v))
            except (TypeError, ValueError):
                return None

        self._observations.append(
            (group, _i(limit), _i(remaining), _i(reset), status == 429)
        )

    def drain_observations(self) -> list[tuple]:
        obs, self._observations = self._observations, []
        return obs


# 엔드포인트 → Rate Limits Group 매핑 (OpenAPI 스펙의 description 기준)
def group_for(method: str, path: str) -> str:
    p = path.split("?")[0]
    if p.startswith("/oauth2/token"):
        return "AUTH"
    if p.startswith("/api/v1/candles"):
        return "MARKET_DATA_CHART"
    if p.startswith(("/api/v1/orderbook", "/api/v1/prices",
                     "/api/v1/trades", "/api/v1/price-limits")):
        return "MARKET_DATA"
    if p.startswith("/api/v1/stocks"):
        return "STOCK"
    if p.startswith(("/api/v1/exchange-rate", "/api/v1/market-calendar")):
        return "MARKET_INFO"
    if p.startswith("/api/v1/rankings"):
        return "RANKING"
    if p.startswith("/api/v1/market-indicators"):
        return "MARKET_INDICATOR_CHART" if p.endswith("/candles") else "MARKET_INDICATOR"
    if p.startswith("/api/v1/accounts"):
        return "ACCOUNT"
    if p.startswith("/api/v1/holdings"):
        return "ASSET"
    if p.startswith("/api/v1/conditional-orders"):
        return "CONDITIONAL_ORDER" if method != "GET" else "CONDITIONAL_ORDER_HISTORY"
    if p.startswith(("/api/v1/buying-power", "/api/v1/sellable-quantity",
                     "/api/v1/commissions")):
        return "ORDER_INFO"
    if p.startswith("/api/v1/orders"):
        return "ORDER_HISTORY" if method == "GET" else "ORDER"
    return "MARKET_INFO"
