"""토스증권 Open API 클라이언트.

  • 토큰 자동 주입 + 401 시 1회 강제 재발급
  • 그룹별 rate limit 준수 (헤더 기반 적응)
  • 계좌 컨텍스트(X-Tossinvest-Account) 캐시 — ACCOUNT 는 1 TPS 라
    매 요청마다 조회하면 안 된다
  • 주문은 드라이런 게이트를 통과해야만 실제 POST 된다
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

import httpx
import psycopg

from .ratelimit import RateLimiter, group_for
from .token import TokenManager

log = logging.getLogger(__name__)


class TossError(RuntimeError):
    def __init__(self, status: int, body: Any):
        self.status = status
        self.body = body
        code = ""
        if isinstance(body, dict):
            code = (body.get("error") or {}).get("code", "")
        super().__init__(f"HTTP {status} {code} {str(body)[:200]}")


class TossClient:
    def __init__(self, settings, conn: psycopg.Connection,
                 user_id: str | None = None, account_seq: str | None = None) -> None:
        """user_id 를 주면 그 사용자의 자격증명으로 동작한다 (멀티유저).

        생략하면 .env 의 단일 자격증명을 쓴다 (공용 수집 · 개인 실행용).
        rate limit 은 클라이언트(=client_id) 단위이므로 **사용자마다
        리미터를 따로 둔다** — 공유하면 남의 호출 때문에 내가 대기한다.
        """
        self._s = settings
        self._conn = conn
        self._uid = user_id
        self._http = httpx.Client(base_url=settings.toss_base_url, timeout=30.0)
        if user_id:
            from accounts import UserTokenManager
            self._tokens = UserTokenManager(conn, user_id)
        else:
            self._tokens = TokenManager(
                settings.toss_base_url, settings.toss_client_id,
                settings.toss_client_secret, conn,
            )
        self._limiter = RateLimiter()
        self._account_seq: str | None = account_seq or settings.toss_account_seq or None

    # ── 저수준 ──────────────────────────────────────────────
    def request(self, method: str, path: str, *, params=None, json=None,
                account: bool = False, retries: int = 3) -> Any:
        group = group_for(method, path)
        last_exc: Exception | None = None

        for attempt in range(retries):
            headers = {"Authorization": f"Bearer {self._tokens.get()}"}
            if account:
                headers["X-Tossinvest-Account"] = str(self.account_seq())

            self._limiter.acquire(group)
            r = self._http.request(method, path, params=params, json=json, headers=headers)
            self._limiter.observe(group, r.headers, r.status_code)

            if r.status_code == 200:
                body = r.json()
                # BFF 공통 envelope 이면 result 를 꺼낸다
                return body.get("result", body) if isinstance(body, dict) else body

            if r.status_code == 401 and attempt == 0:
                # 만료 전 401 = 같은 client_id 로 제3자가 토큰을 발급했다는 신호.
                # (토큰 1개 제약의 부작용 — 탈취 탐지에 역이용한다)
                if self._uid and hasattr(self._tokens, "note_401"):
                    if self._tokens.note_401():
                        raise TossError(401, {"error": {
                            "code": "account-locked",
                            "message": "자격증명 탈취가 의심되어 계정을 잠갔습니다. "
                                       "토스 WTS 에서 client_secret 을 재발급하세요."}})
                log.warning("401 — 토큰 재발급 후 재시도")
                self._tokens.get(force=True)
                continue

            if r.status_code == 429:
                # observe() 가 blocked_until 을 세팅했으므로 다음 acquire 가 잔다
                log.warning("429 %s — Retry-After 만큼 대기 후 재시도", group)
                last_exc = TossError(r.status_code, r.json() if r.text else {})
                continue

            if 500 <= r.status_code < 600:
                time.sleep(2 ** attempt)
                last_exc = TossError(r.status_code, r.text[:300])
                continue

            # 4xx 는 재시도해도 소용없다 (파라미터 오류 등)
            try:
                raise TossError(r.status_code, r.json())
            except ValueError:
                raise TossError(r.status_code, r.text[:300])

        raise last_exc or RuntimeError("요청 실패")

    def get(self, path: str, **kw) -> Any:
        return self.request("GET", path, **kw)

    # ── 계좌 컨텍스트 ───────────────────────────────────────
    def account_seq(self) -> str:
        """ACCOUNT 그룹은 1 TPS. 최초 1회만 조회하고 캐시한다."""
        if self._account_seq is None:
            accounts = self.request("GET", "/api/v1/accounts")
            if not accounts:
                raise RuntimeError("조회 가능한 계좌가 없습니다")
            self._account_seq = str(accounts[0]["accountSeq"])
            log.info("accountSeq=%s 캐시됨", self._account_seq)
        return self._account_seq

    # ── 시세 ────────────────────────────────────────────────
    def prices(self, symbols: list[str]) -> list[dict]:
        """최대 200종목 일괄. 관심종목 전체가 1 TPS 로 끝난다."""
        out: list[dict] = []
        for i in range(0, len(symbols), 200):
            chunk = symbols[i:i + 200]
            out += self.get("/api/v1/prices", params={"symbols": ",".join(chunk)}) or []
        return out

    def orderbook(self, symbol: str) -> dict:
        """심볼 1개씩만 가능 — 여기가 MARKET_DATA 예산의 병목이다."""
        return self.get("/api/v1/orderbook", params={"symbol": symbol})

    def trades(self, symbol: str) -> Any:
        return self.get("/api/v1/trades", params={"symbol": symbol})

    def price_limits(self, symbol: str) -> Any:
        return self.get("/api/v1/price-limits", params={"symbol": symbol})

    def candles(self, symbol: str, interval: str, count: int = 200,
                before: str | None = None, adjusted: bool = True) -> dict:
        p: dict[str, Any] = {"symbol": symbol, "interval": interval,
                             "count": count, "adjusted": str(adjusted).lower()}
        if before:
            p["before"] = before
        return self.get("/api/v1/candles", params=p)

    # ── 종목 / 시장 ─────────────────────────────────────────
    def stocks(self, symbols: list[str]) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(symbols), 200):
            out += self.get("/api/v1/stocks",
                            params={"symbols": ",".join(symbols[i:i + 200])}) or []
        return out

    def warnings(self, symbol: str) -> list[dict]:
        return self.get(f"/api/v1/stocks/{symbol}/warnings") or []

    def exchange_rate(self, base: str = "USD", quote: str = "KRW") -> dict:
        return self.get("/api/v1/exchange-rate",
                        params={"baseCurrency": base, "quoteCurrency": quote})

    def market_calendar(self, market: str) -> dict:
        return self.get(f"/api/v1/market-calendar/{market}")

    def rankings(self, type_: str = "MARKET_TRADING_AMOUNT", market: str = "KR",
                 duration: str = "realtime", count: int = 20) -> dict:
        # duration enum 은 소문자: realtime|1d|1w|1mo|3mo|6mo|1y
        return self.get("/api/v1/rankings", params={
            "type": type_, "marketCountry": market,
            "duration": duration, "count": count})

    def indicator_prices(self, symbols: list[str]) -> list[dict]:
        # 카탈로그 8종만: KOSPI, KOSDAQ, KR_BOND_2Y/3Y/5Y/10Y/20Y/30Y
        return self.get("/api/v1/market-indicators/prices",
                        params={"symbols": ",".join(symbols)}) or []

    def indicator_candles(self, symbol: str, interval: str = "1d", count: int = 200) -> dict:
        return self.get(f"/api/v1/market-indicators/{symbol}/candles",
                        params={"interval": interval, "count": count})

    def investor_trading(self, symbol: str, interval: str = "1d",
                         count: int = 100, until: str | None = None) -> dict:
        # symbol 은 KOSPI|KOSDAQ 만, interval 은 필수 (1d|1w|1mo|1y)
        p: dict[str, Any] = {"interval": interval, "count": count}
        if until:
            p["until"] = until
        return self.get(f"/api/v1/market-indicators/{symbol}/investor-trading", params=p)

    # ── 계좌 / 자산 ─────────────────────────────────────────
    def holdings(self) -> Any:
        return self.get("/api/v1/holdings", account=True)

    def buying_power(self, currency: str = "KRW") -> dict:
        # currency 는 필수 파라미터 (누락 시 400 invalid-request)
        return self.get("/api/v1/buying-power",
                        params={"currency": currency}, account=True)

    def sellable_quantity(self, symbol: str) -> dict:
        return self.get("/api/v1/sellable-quantity",
                        params={"symbol": symbol}, account=True)

    def commissions(self) -> list[dict]:
        return self.get("/api/v1/commissions", account=True) or []

    def open_orders(self) -> Any:
        return self.get("/api/v1/orders", params={"status": "OPEN"}, account=True)

    # ── 주문 (드라이런 게이트) ──────────────────────────────
    def place_order(self, *, symbol: str, side: str, order_type: str,
                    quantity: str | None = None, order_amount: str | None = None,
                    price: str | None = None, time_in_force: str = "DAY",
                    client_order_id: str | None = None,
                    reason: dict | None = None) -> dict:
        """주문 생성.

        ⚠️ clientOrderId 는 토스가 제공하는 멱등성 키(10분 유효)다.
           미전달 시 네트워크 재시도가 '별개 주문'이 되어 중복 체결된다.
           → 항상 생성해서 보내고, DB 에 먼저 기록한 뒤 호출한다.

        EXECUTE_ORDERS=false 면 페이로드 조립·검증까지만 하고
        실제 POST 는 하지 않는다. 코드 경로는 끝까지 살아있으므로
        주석 처리와 달리 실제로 테스트된다.
        """
        if (quantity is None) == (order_amount is None):
            raise ValueError("quantity 와 orderAmount 중 정확히 하나만 지정해야 합니다")
        if order_type == "LIMIT" and price is None:
            raise ValueError("LIMIT 주문은 price 가 필수입니다")
        if order_type == "MARKET" and price is not None:
            raise ValueError("MARKET 주문에는 price 를 전달할 수 없습니다")

        coid = client_order_id or f"td-{uuid.uuid4().hex[:20]}"
        body: dict[str, Any] = {
            "clientOrderId": coid, "symbol": symbol, "side": side,
            "orderType": order_type, "timeInForce": time_in_force,
        }
        if quantity is not None:
            body["quantity"] = str(quantity)
        if order_amount is not None:
            body["orderAmount"] = str(order_amount)
        if price is not None:
            body["price"] = str(price)

        # ── 주문 경로 하드 게이트 ──────────────────────────
        #  ⚠️ 토스 자격증명에는 읽기 전용 scope 가 없다.
        #     즉 '조회용'으로 받은 남의 키에도 주문 권한이 붙어 있다.
        #     멀티유저 배포에서는 주문 경로가 존재하는 것 자체가 위험이므로,
        #     ALLOW_ORDERS=true 를 명시하지 않으면 아예 차단한다.
        #     (공개 배포판에서는 이 값을 절대 켜지 말 것)
        import os as _os
        if _os.environ.get("ALLOW_ORDERS", "").lower() not in ("1", "true", "yes"):
            raise RuntimeError(
                "주문 경로가 차단되어 있습니다 (ALLOW_ORDERS 미설정). "
                "멀티유저 배포에서는 켜지 마세요.")
        if self._uid:
            raise RuntimeError(
                "타인 계정(user_id 지정)으로는 주문할 수 없습니다. "
                "본인 계정 단일 실행에서만 허용됩니다.")

        dry = not self._s.execute_orders
        self._log_order(coid, body, dry, reason)

        if dry:
            log.warning("[DRY-RUN] 주문 미전송: %s", body)
            return {"dryRun": True, "clientOrderId": coid, "request": body}

        self._guard_live_order(body)
        resp = self.request("POST", "/api/v1/orders", json=body, account=True)
        self._update_order(coid, resp)
        return resp

    # ── 주문 감사 기록 ──────────────────────────────────────
    def _log_order(self, coid: str, body: dict, dry: bool, reason: dict | None) -> None:
        import json as _json
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO order_log (client_order_id, symbol, side, order_type,
                    time_in_force, quantity, order_amount, price, is_dry_run,
                    status, reason, request_body)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING',%s,%s)
                ON CONFLICT (client_order_id) DO NOTHING
                """,
                (coid, body["symbol"], body["side"], body["orderType"],
                 body["timeInForce"], body.get("quantity"), body.get("orderAmount"),
                 body.get("price"), dry,
                 _json.dumps(reason) if reason else None, _json.dumps(body)),
            )
        self._conn.commit()

    def _update_order(self, coid: str, resp: dict) -> None:
        import json as _json
        with self._conn.cursor() as cur:
            cur.execute(
                """UPDATE order_log SET toss_order_id=%s, status='SUBMITTED',
                          response_body=%s, updated_at=now()
                   WHERE client_order_id=%s""",
                (str(resp.get("orderId") or ""), _json.dumps(resp), coid),
            )
        self._conn.commit()

    def _guard_live_order(self, body: dict) -> None:
        """실주문 킬스위치. 드라이런에서는 호출되지 않는다."""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM order_log "
                "WHERE NOT is_dry_run AND created_at >= date_trunc('day', now())"
            )
            today = cur.fetchone()[0]
        if today >= self._s.max_orders_per_day:
            raise RuntimeError(
                f"일일 주문 한도 초과 ({today}/{self._s.max_orders_per_day}) — 중단")

        est = None
        if body.get("price") and body.get("quantity"):
            est = float(body["price"]) * float(body["quantity"])
        elif body.get("orderAmount"):
            est = float(body["orderAmount"])
        if est and est > self._s.max_order_amount_krw:
            raise RuntimeError(
                f"주문 금액 한도 초과 ({est:,.0f} > {self._s.max_order_amount_krw:,}) — 중단")

    # ── 관측 기록 ───────────────────────────────────────────
    def flush_observations(self) -> int:
        rows = self._limiter.drain_observations()
        if not rows:
            return 0
        with self._conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO rate_limit_observation
                     (group_name, limit_value, remaining, reset_sec, was_429)
                   VALUES (%s,%s,%s,%s,%s)
                   ON CONFLICT DO NOTHING""",
                rows,
            )
        self._conn.commit()
        return len(rows)

    def close(self) -> None:
        self._http.close()
