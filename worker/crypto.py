"""세션 토큰 해시.

⚠️ 자격증명 암호화는 **pgcrypto(pgp_sym_encrypt)** 로 한다 — 여기가 아니다.
   Next.js 온보딩 API 와 Python 워커가 같은 행을 읽어야 해서,
   한쪽만 앱 레벨 암호화를 쓰면 서로 복호화가 안 된다(실제로 물렸던 버그).
   키는 CREDENTIAL_MASTER_KEY 를 양쪽이 공유한다.

여기서는 세션 토큰만 다룬다:
  쿠키에는 원문을, DB 에는 해시만 저장한다 → DB 가 털려도 세션 위조 불가.

    python3 worker/crypto.py --genkey    # 마스터 키 · 페퍼 생성
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import sys


def new_session_token() -> tuple[str, bytes]:
    """(쿠키에 넣을 원문, DB 에 넣을 해시)"""
    raw = secrets.token_urlsafe(32)
    return raw, hash_session(raw)


def hash_session(raw: str) -> bytes:
    pepper = os.environ.get("SESSION_PEPPER", "").encode()
    return hashlib.sha256(pepper + raw.encode()).digest()


def constant_eq(a: bytes, b: bytes) -> bool:
    return hmac.compare_digest(a, b)


if __name__ == "__main__":
    if "--genkey" in sys.argv:
        # pgcrypto 대칭키 + 세션 페퍼. 한 번 정하면 바꾸지 말 것 —
        # 마스터 키를 바꾸면 저장된 자격증명을 복호화할 수 없다.
        print("CREDENTIAL_MASTER_KEY=" + secrets.token_urlsafe(48))
        print("SESSION_PEPPER=" + base64.urlsafe_b64encode(os.urandom(24)).decode())
    else:
        raw, h = new_session_token()
        assert constant_eq(hash_session(raw), h)
        assert not constant_eq(hash_session("other"), h)
        print("✅ 세션 해시 정상 (cryptography 의존성 불필요)")
