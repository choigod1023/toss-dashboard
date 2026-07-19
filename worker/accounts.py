"""사용자 자격증명 · 세션 · 사용자별 토큰.

핵심 제약 (실측)
  • 토큰은 **client_id 당 1개**. 재발급하면 이전 토큰이 즉시 죽는다.
    → client_id UNIQUE + advisory lock 을 **사용자별 키**로 잡는다.
      (전역 락 하나면 사용자 수만큼 줄을 선다)
  • /oauth2/token 은 grant_type/client_id/client_secret 만 받는다.
    **읽기 전용 scope 가 없다** — 조회용 자격증명에도 주문 권한이 붙는다.

탈취 탐지
  만료 전인데 401 이 나면 = 같은 client_id 로 누군가 토큰을 재발급했다는 뜻.
  (토큰 1개 제약의 부작용을 역이용한다)
  반복되면 계정을 잠그고 사용자에게 알린다.
"""

from __future__ import annotations

import logging
import uuid
import zlib
from datetime import datetime, timedelta, timezone

import httpx
import psycopg

from crypto import hash_session, new_session_token

#  ⚠️ 암호화는 **pgcrypto 로 통일**한다.
#     Next.js 온보딩 API 와 Python 워커가 같은 데이터를 읽어야 하는데,
#     한쪽만 Fernet 을 쓰면 서로 복호화가 안 된다(실제로 물렸던 버그).
#     키는 CREDENTIAL_MASTER_KEY 를 양쪽이 공유한다.
MASTER = None


def _master_key() -> str:
    import os
    k = os.environ.get("CREDENTIAL_MASTER_KEY", "").strip()
    if not k:
        raise RuntimeError("CREDENTIAL_MASTER_KEY 가 없습니다")
    return k

log = logging.getLogger(__name__)

BASE = "https://openapi.tossinvest.com"
REFRESH_MARGIN = timedelta(minutes=30)
SESSION_TTL = timedelta(days=30)
LOCK_PREMATURE_401 = 3          # 이 횟수 넘으면 계정 잠금


def _lock_key(user_id: str) -> int:
    """사용자별 advisory lock 키. 전역 하나면 병목이 된다."""
    return zlib.crc32(str(user_id).encode()) & 0x7FFFFFFF


# ── 온보딩 ───────────────────────────────────────────────────
def verify_credentials(client_id: str, client_secret: str) -> dict:
    """저장 전에 실제로 동작하는지 확인한다.

    검증 없이 저장하면 오타 하나로 좀비 계정이 생긴다.
    ⚠️ 이 호출 자체가 토큰을 발급하므로, 해당 client_id 로 발급돼 있던
       기존 토큰(사용자의 다른 도구 등)은 이 시점에 무효화된다.
    """
    r = httpx.post(f"{BASE}/oauth2/token", data={
        "grant_type": "client_credentials",
        "client_id": client_id, "client_secret": client_secret,
    }, timeout=30.0)
    if r.status_code != 200:
        raise ValueError(f"자격증명이 유효하지 않습니다 (HTTP {r.status_code})")
    tok = r.json()

    a = httpx.get(f"{BASE}/api/v1/accounts",
                  headers={"Authorization": f"Bearer {tok['access_token']}"},
                  timeout=30.0)
    if a.status_code != 200:
        raise ValueError(f"계좌 조회 실패 (HTTP {a.status_code})")
    accounts = (a.json() or {}).get("result") or []
    if not accounts:
        raise ValueError("조회 가능한 계좌가 없습니다")

    return {
        "access_token": tok["access_token"],
        "expires_at": datetime.now(timezone.utc) + timedelta(seconds=int(tok["expires_in"])),
        "account_seq": str(accounts[0]["accountSeq"]),
        "account_type": accounts[0].get("accountType"),
    }


def onboard(conn: psycopg.Connection, client_id: str, client_secret: str,
            nickname: str | None = None, user_agent: str | None = None) -> dict:
    """키 검증 → 사용자 생성 → 봉인 저장 → 세션 발급.

    별도 로그인이 없다. **자격증명이 곧 신원**이다.
    """
    with conn.cursor() as cur:
        cur.execute("""SELECT user_id, status FROM user_credential
                       WHERE client_id = %s""", (client_id,))
        existing = cur.fetchone()

    verified = verify_credentials(client_id, client_secret)   # 실패 시 여기서 중단

    with conn.cursor() as cur:
        if existing:
            user_id, status = existing
            if status == "revoked":
                raise ValueError("해지된 계정입니다. 관리자에게 문의하세요.")
            # 이미 등록된 client_id — 새 시크릿을 제시했다는 건
            # 토스에서 재발급받을 수 있는 실소유자라는 뜻이다.
            cur.execute("""
                UPDATE user_credential
                   SET client_secret_enc=pgp_sym_encrypt(%s, %s)::bytea,
                       account_seq=%s, status='active',
                       premature_401_count=0, locked_reason=NULL, last_ok_at=now()
                 WHERE user_id=%s
            """, (client_secret, _master_key(), verified["account_seq"], user_id))
        else:
            cur.execute("INSERT INTO app_user (nickname) VALUES (%s) RETURNING id",
                        (nickname,))
            user_id = cur.fetchone()[0]
            cur.execute("""
                INSERT INTO user_credential
                    (user_id, client_id, client_secret_enc, account_seq,
                     verified_at, last_ok_at)
                VALUES (%s,%s, pgp_sym_encrypt(%s,%s)::bytea, %s, now(), now())
            """, (user_id, client_id, client_secret, _master_key(),
                  verified["account_seq"]))

        # 검증하며 받은 토큰을 그대로 재활용 (불필요한 재발급 방지)
        cur.execute("""
            INSERT INTO toss_token (user_id, access_token, expires_at)
            VALUES (%s,%s,%s)
            ON CONFLICT (user_id) DO UPDATE SET
                access_token=EXCLUDED.access_token, expires_at=EXCLUDED.expires_at,
                issued_at=now(), updated_at=now()
        """, (user_id, verified["access_token"], verified["expires_at"]))

        raw, digest = new_session_token()
        cur.execute("""
            INSERT INTO user_session (token_hash, user_id, expires_at, user_agent)
            VALUES (%s,%s,%s,%s)
        """, (digest, user_id, datetime.now(timezone.utc) + SESSION_TTL, user_agent))
    conn.commit()

    return {"user_id": str(user_id), "session_token": raw,
            "account_seq": verified["account_seq"]}


def resolve_session(conn: psycopg.Connection, raw_token: str) -> str | None:
    """쿠키 → user_id. 만료된 세션은 무시한다."""
    if not raw_token:
        return None
    with conn.cursor() as cur:
        cur.execute("""SELECT user_id FROM user_session
                       WHERE token_hash=%s AND expires_at > now()""",
                    (hash_session(raw_token),))
        row = cur.fetchone()
        if row:
            cur.execute("UPDATE app_user SET last_seen_at=now() WHERE id=%s", (row[0],))
            conn.commit()
    return str(row[0]) if row else None


def logout(conn: psycopg.Connection, raw_token: str) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM user_session WHERE token_hash=%s",
                    (hash_session(raw_token),))
    conn.commit()


def delete_user(conn: psycopg.Connection, user_id: str) -> None:
    """탈퇴 — 자격증명·세션·데이터 전부 삭제 (CASCADE)."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM app_user WHERE id=%s", (user_id,))
    conn.commit()
    log.info("사용자 %s 삭제 완료", user_id)


# ── 사용자별 토큰 ────────────────────────────────────────────
class UserTokenManager:
    def __init__(self, conn: psycopg.Connection, user_id: str) -> None:
        self._conn = conn
        self._uid = user_id

    def _creds(self) -> tuple[str, str]:
        with self._conn.cursor() as cur:
            cur.execute("""
                SELECT client_id,
                       pgp_sym_decrypt(client_secret_enc, %s) AS secret,
                       status
                  FROM user_credential WHERE user_id=%s
            """, (_master_key(), self._uid))
            row = cur.fetchone()
        if not row:
            raise RuntimeError("자격증명이 없습니다")
        if row[2] != "active":
            raise RuntimeError(f"계정 상태: {row[2]}")
        return row[0], row[1]

    def _read(self) -> tuple[str, datetime] | None:
        with self._conn.cursor() as cur:
            cur.execute("""SELECT access_token, expires_at FROM toss_token
                           WHERE user_id=%s""", (self._uid,))
            row = cur.fetchone()
        return (row[0], row[1]) if row else None

    @staticmethod
    def _fresh(exp: datetime) -> bool:
        return exp - REFRESH_MARGIN > datetime.now(timezone.utc)

    def _issue(self) -> str:
        cid, csec = self._creds()
        r = httpx.post(f"{BASE}/oauth2/token", data={
            "grant_type": "client_credentials",
            "client_id": cid, "client_secret": csec}, timeout=30.0)
        if r.status_code != 200:
            raise RuntimeError(f"토큰 발급 실패 HTTP {r.status_code}")
        d = r.json()
        exp = datetime.now(timezone.utc) + timedelta(seconds=int(d["expires_in"]))
        with self._conn.cursor() as cur:
            cur.execute("""
                INSERT INTO toss_token (user_id, access_token, expires_at)
                VALUES (%s,%s,%s)
                ON CONFLICT (user_id) DO UPDATE SET
                    access_token=EXCLUDED.access_token,
                    expires_at=EXCLUDED.expires_at,
                    issued_at=now(), updated_at=now()
            """, (self._uid, d["access_token"], exp))
            cur.execute("UPDATE user_credential SET last_ok_at=now() WHERE user_id=%s",
                        (self._uid,))
        self._conn.commit()
        return d["access_token"]

    def get(self, force: bool = False) -> str:
        if not force:
            cached = self._read()
            if cached and self._fresh(cached[1]):
                return cached[0]

        key = _lock_key(self._uid)
        with self._conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (key,))
        try:
            if not force:   # 락 대기 중 남이 발급했을 수 있다
                again = self._read()
                if again and self._fresh(again[1]):
                    return again[0]
            return self._issue()
        finally:
            with self._conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (key,))
            self._conn.commit()

    def note_401(self) -> bool:
        """만료 전 401 = 제3자가 같은 client_id 로 토큰을 발급했다는 신호.

        반복되면 계정을 잠근다. 잠겼으면 True 를 반환한다.
        """
        cached = self._read()
        if not cached or not self._fresh(cached[1]):
            return False        # 정상 만료 — 탈취 신호 아님

        with self._conn.cursor() as cur:
            cur.execute("""
                UPDATE user_credential
                   SET premature_401_count = premature_401_count + 1
                 WHERE user_id=%s
             RETURNING premature_401_count
            """, (self._uid,))
            n = cur.fetchone()[0]
            locked = n >= LOCK_PREMATURE_401
            if locked:
                cur.execute("""
                    UPDATE user_credential
                       SET status='locked',
                           locked_reason='만료 전 401 반복 — 제3자 사용 의심'
                     WHERE user_id=%s
                """, (self._uid,))
        self._conn.commit()
        if locked:
            log.warning("사용자 %s 계정 잠금 — 자격증명 탈취 의심", self._uid)
        return locked


def active_users(conn: psycopg.Connection) -> list[tuple[str, str]]:
    """워커가 순회할 사용자 목록. (user_id, account_seq)"""
    with conn.cursor() as cur:
        cur.execute("""SELECT user_id, account_seq FROM user_credential
                       WHERE status='active' ORDER BY created_at""")
        return [(str(r[0]), r[1]) for r in cur.fetchall()]
