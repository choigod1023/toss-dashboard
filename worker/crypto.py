"""세션 토큰 해시 유틸.

⚠️ 자격증명 암호화는 **pgcrypto(pgp_sym_encrypt)** 로 통일했다.
   Next.js 온보딩 API 와 Python 워커가 같은 행을 읽어야 하기 때문이다.
   아래 seal/unseal 은 남겨두지만 자격증명에는 쓰지 않는다.

원래 설명:

토스 client_secret 은 **주문 권한을 포함**한다 (읽기 전용 scope 가 없다).
평문으로 DB 에 두면 덤프 한 번에 남의 계좌 거래 권한이 통째로 나간다.

마스터 키는 DB 밖(.env / KMS)에 둔다. DB 만 털려서는 복호화가 안 되게.

    python3 worker/crypto.py --genkey    # 마스터 키 생성
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import sys

from cryptography.fernet import Fernet, InvalidToken


def _master() -> Fernet:
    key = os.environ.get("CREDENTIAL_MASTER_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "CREDENTIAL_MASTER_KEY 가 없습니다.\n"
            "  python3 worker/crypto.py --genkey  로 만들어 .env 에 넣으세요."
        )
    try:
        return Fernet(key.encode())
    except Exception as e:
        raise RuntimeError(f"CREDENTIAL_MASTER_KEY 형식 오류: {e}") from e


def seal(plaintext: str) -> bytes:
    """client_secret 봉인."""
    return _master().encrypt(plaintext.encode())


def unseal(blob: bytes | memoryview) -> str:
    """복호화. 마스터 키가 바뀌었으면 InvalidToken 이 난다."""
    try:
        return _master().decrypt(bytes(blob)).decode()
    except InvalidToken as e:
        raise RuntimeError(
            "복호화 실패 — 마스터 키가 바뀌었거나 데이터가 손상됐습니다."
        ) from e


# ── 세션 토큰 ────────────────────────────────────────────────
#  쿠키에는 원문을 주고, DB 에는 해시만 저장한다.
#  DB 가 털려도 세션을 위조할 수 없게.
def new_session_token() -> tuple[str, bytes]:
    raw = secrets.token_urlsafe(32)
    return raw, hash_session(raw)


def hash_session(raw: str) -> bytes:
    pepper = os.environ.get("SESSION_PEPPER", "").encode()
    return hashlib.sha256(pepper + raw.encode()).digest()


def constant_eq(a: bytes, b: bytes) -> bool:
    return hmac.compare_digest(a, b)


if __name__ == "__main__":
    if "--genkey" in sys.argv:
        print("# .env 에 아래 두 줄을 추가하세요 (이미 있으면 바꾸지 마세요 —")
        print("#  바꾸면 기존에 저장된 자격증명을 복호화할 수 없습니다)")
        print(f"CREDENTIAL_MASTER_KEY={Fernet.generate_key().decode()}")
        print(f"SESSION_PEPPER={base64.urlsafe_b64encode(os.urandom(24)).decode()}")
    else:
        s = seal("test-secret-value")
        assert unseal(s) == "test-secret-value"
        raw, h = new_session_token()
        assert constant_eq(hash_session(raw), h)
        print("✅ 봉인·복호화·세션 해시 정상")
