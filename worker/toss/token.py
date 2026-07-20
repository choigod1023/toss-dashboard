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
    """공용 수집용 토큰.

    ⚠️ 자격증명을 환경변수에 박지 않는다.
       토스 시크릿은 사용자가 WTS 에서 언제든 재발급할 수 있는 **동적 값**이다.
       env 에 두면 재발급할 때마다 .env / Render / GitHub Secrets 를
       전부 고쳐야 하고, 하나라도 빠지면 조용히 401 이 난다(실제로 겪었다).

       → 단일 출처는 DB(user_credential)다. 웹 온보딩으로 갱신하면
         워커가 자동으로 새 값을 집어간다. 배포·재시작이 필요 없다.
    """

    def __init__(self, base_url: str, conn: psycopg.Connection) -> None:
        self._base_url = base_url
        self._conn = conn
        self._uid_cache: str | None = None
        self._creds_cache: tuple[str, str] | None = None

    def _creds(self) -> tuple[str, str]:
        """is_collector 로 지정된 자격증명. 없으면 가장 먼저 온보딩한 사용자."""
        if self._creds_cache:
            return self._creds_cache
        import os
        key = os.environ.get("CREDENTIAL_MASTER_KEY", "")
        if not key:
            raise RuntimeError("CREDENTIAL_MASTER_KEY 가 없습니다")
        with self._conn.cursor() as cur:
            cur.execute("""
                SELECT user_id, client_id, pgp_sym_decrypt(client_secret_enc, %s)
                  FROM user_credential
                 WHERE status = 'active'
                 ORDER BY is_collector DESC, created_at
                 LIMIT 1
            """, (key,))
            row = cur.fetchone()
        if not row:
            raise RuntimeError(
                "수집용 자격증명이 없습니다 — 웹에서 토스 계정을 한 번 연결하세요")
        self._uid_cache = str(row[0])
        self._creds_cache = (row[1], row[2])
        return self._creds_cache

    # ── 내부 ────────────────────────────────────────────────
    def _owner(self) -> str | None:
        self._creds()          # _uid_cache 를 채운다
        return self._uid_cache

    def _read(self) -> tuple[str, datetime] | None:
        uid = self._owner()
        if not uid:
            return None
        with self._conn.cursor() as cur:
            cur.execute("SELECT access_token, expires_at FROM toss_token WHERE user_id = %s",
                        (uid,))
            row = cur.fetchone()
        return (row[0], row[1]) if row else None

    @staticmethod
    def _fresh(exp: datetime) -> bool:
        return exp - REFRESH_MARGIN > datetime.now(timezone.utc)

    def _issue(self) -> tuple[str, datetime]:
        cid, csec = self._creds()
        r = httpx.post(
            f"{self._base_url}/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": cid,
                "client_secret": csec,
            },
            timeout=30.0,
        )
        if r.status_code != 200:
            raise RuntimeError(f"토큰 발급 실패 HTTP {r.status_code}: {r.text[:300]}")
        d = r.json()
        token = d["access_token"]
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(d["expires_in"]))

        uid = self._owner()
        if not uid:
            # 아직 온보딩되지 않은 자격증명 — DB 에 캐시하지 않고 그대로 쓴다
            log.info("소유자 미등록 client_id — 토큰을 캐시하지 않습니다")
            return token, expires_at
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO toss_token (user_id, access_token, token_type, issued_at, expires_at, updated_at)
                VALUES (%s, %s, %s, now(), %s, now())
                ON CONFLICT (user_id) DO UPDATE
                   SET access_token = EXCLUDED.access_token,
                       token_type   = EXCLUDED.token_type,
                       issued_at    = now(),
                       expires_at   = EXCLUDED.expires_at,
                       updated_at   = now()
                """,
                (uid, token, d.get("token_type", "Bearer"), expires_at),
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
