"""토큰 매니저 — 단일 발급 지점.

스펙 원문:
  "client 당 유효한 access token 은 1 개입니다.
   재발급 시 이전에 발급된 token 은 즉시 무효화됩니다."
  "refresh token 은 제공되지 않습니다."

→ 두 프로세스가 각자 발급하면 서로를 401 로 만든다.
  그래서 발급을 Postgres advisory lock 으로 직렬화하고,
  결과를 toss_token 테이블(단일 행)에 공유한다.

  락을 잡은 뒤 **반드시 DB 를 다시 읽는다** — 내가 락을 기다리는 동안
  다른 프로세스가 이미 새 토큰을 발급했을 수 있기 때문이다.
  (이 재확인이 없으면 락이 있어도 이중 발급이 난다)

실측: expires_in = 86399초(24시간).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx
import psycopg

log = logging.getLogger(__name__)

# pg_advisory_lock 키 (schema.sql 주석과 일치시킬 것)
TOKEN_LOCK_KEY = 8_020_119

# 만료 이 시간 전이면 미리 갱신한다
REFRESH_MARGIN = timedelta(minutes=30)


class TokenManager:
    def __init__(self, base_url: str, client_id: str, client_secret: str,
                 conn: psycopg.Connection) -> None:
        self._base_url = base_url
        self._id = client_id
        self._secret = client_secret
        self._conn = conn

    # ── 내부 ────────────────────────────────────────────────
    def _read(self) -> tuple[str, datetime] | None:
        with self._conn.cursor() as cur:
            cur.execute("SELECT access_token, expires_at FROM toss_token WHERE id = 1")
            row = cur.fetchone()
        return (row[0], row[1]) if row else None

    @staticmethod
    def _fresh(exp: datetime) -> bool:
        return exp - REFRESH_MARGIN > datetime.now(timezone.utc)

    def _issue(self) -> tuple[str, datetime]:
        r = httpx.post(
            f"{self._base_url}/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self._id,
                "client_secret": self._secret,
            },
            timeout=30.0,
        )
        if r.status_code != 200:
            raise RuntimeError(f"토큰 발급 실패 HTTP {r.status_code}: {r.text[:300]}")
        d = r.json()
        token = d["access_token"]
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(d["expires_in"]))

        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO toss_token (id, access_token, token_type, issued_at, expires_at, updated_at)
                VALUES (1, %s, %s, now(), %s, now())
                ON CONFLICT (id) DO UPDATE
                   SET access_token = EXCLUDED.access_token,
                       token_type   = EXCLUDED.token_type,
                       issued_at    = now(),
                       expires_at   = EXCLUDED.expires_at,
                       updated_at   = now()
                """,
                (token, d.get("token_type", "Bearer"), expires_at),
            )
        self._conn.commit()
        log.info("토큰 재발급 완료 (만료 %s)", expires_at.isoformat(timespec="seconds"))
        return token, expires_at

    # ── 공개 ────────────────────────────────────────────────
    def get(self, force: bool = False) -> str:
        """유효한 토큰을 반환. 필요하면 락을 잡고 재발급한다."""
        if not force:
            cached = self._read()
            if cached and self._fresh(cached[1]):
                return cached[0]

        with self._conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (TOKEN_LOCK_KEY,))
        try:
            # ★ 락 획득 후 재확인 — 대기 중에 남이 발급했을 수 있다
            if not force:
                again = self._read()
                if again and self._fresh(again[1]):
                    return again[0]
            return self._issue()[0]
        finally:
            with self._conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (TOKEN_LOCK_KEY,))
            self._conn.commit()
