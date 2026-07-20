"""설정 로딩.

두 개의 env 파일을 합쳐서 읽는다:
  1. web/.env.local  — Vercel 이 관리 (DATABASE_URL 등). 직접 편집하지 말 것.
                       갱신: cd web && vercel env pull .env.local
  2. .env            — 사용자가 직접 채우는 시크릿 (토스 키, LLM 키 등)

.env 가 나중에 로드되므로 같은 키가 있으면 .env 가 이긴다.

⚠️ 이 모듈은 시크릿 '값'을 절대 로깅·출력하지 않는다.
   검증은 "키가 존재하는가"만 확인한다.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# key="value" / key=value / 주석·빈 줄 무시
_LINE = re.compile(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$')


def _load_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        out[key] = val
    return out


def load_env() -> dict[str, str]:
    """env 파일들을 읽어 os.environ 에 주입 (기존 환경변수가 우선)."""
    merged: dict[str, str] = {}
    merged.update(_load_env_file(ROOT / "web" / ".env.local"))
    merged.update(_load_env_file(ROOT / ".env"))
    for k, v in merged.items():
        os.environ.setdefault(k, v)
    return merged


def _bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # ── DB ──
    database_url: str

    # ── 토스 API ──
    toss_client_id: str
    toss_client_secret: str
    toss_account_seq: str
    toss_base_url: str = "https://openapi.tossinvest.com"

    # ── 주문 안전장치 ──
    execute_orders: bool = False
    max_orders_per_day: int = 10
    max_order_amount_krw: int = 100_000

    # ── 감성분석 ──
    sentiment_backend: str = "gemini"
    gemini_api_key: str = ""
    sentiment_model: str = "gemini-flash-lite-latest"
    sentiment_batch_size: int = 20

    # ── 외부 소스 ──
    dart_api_key: str = ""

    # ── 폴링 ──
    poll_prices_sec: float = 1.0
    poll_orderbook_sec: float = 1.0
    poll_trades_sec: float = 2.0

    def missing(self) -> list[str]:
        """비어 있는 필수 키 이름 목록. 값은 절대 반환하지 않는다."""
        # 토스 자격증명은 **DB(user_credential)** 가 단일 출처다.
        # 사용자가 WTS 에서 재발급하면 웹 온보딩으로 갱신되고,
        # 워커는 그걸 자동으로 집어간다. env 에 둘 이유가 없다.
        required = {
            "DATABASE_URL": self.database_url,
            "CREDENTIAL_MASTER_KEY": os.environ.get("CREDENTIAL_MASTER_KEY", ""),
        }
        if self.sentiment_backend == "gemini":
            required["GEMINI_API_KEY"] = self.gemini_api_key
        return [k for k, v in required.items() if not v]

    def redacted(self) -> dict[str, str]:
        """로그·디버그용. 시크릿은 존재 여부만 표시."""
        def mark(v: str) -> str:
            return f"<설정됨:{len(v)}자>" if v else "<비어있음>"

        return {
            "DATABASE_URL": mark(self.database_url),
            "TOSS_CLIENT_ID": mark(self.toss_client_id),
            "TOSS_CLIENT_SECRET": mark(self.toss_client_secret),
            "TOSS_ACCOUNT_SEQ": self.toss_account_seq or "<미설정: 런타임 조회>",
            "GEMINI_API_KEY": mark(self.gemini_api_key),
            "DART_API_KEY": mark(self.dart_api_key),
            "EXECUTE_ORDERS": str(self.execute_orders),
            "SENTIMENT_BACKEND": self.sentiment_backend,
            "SENTIMENT_MODEL": self.sentiment_model,
        }


def get_settings() -> Settings:
    load_env()
    # Neon 은 풀러(DATABASE_URL)와 직결(UNPOOLED) 두 가지를 준다.
    # 워커는 장기 연결 + 스키마 변경을 하므로 직결을 선호한다.
    db = (
        os.environ.get("DATABASE_URL_UNPOOLED")
        or os.environ.get("POSTGRES_URL_NON_POOLING")
        or os.environ.get("DATABASE_URL")
        or ""
    )
    return Settings(
        database_url=db,
        toss_client_id=os.environ.get("TOSS_CLIENT_ID", ""),
        toss_client_secret=os.environ.get("TOSS_CLIENT_SECRET", ""),
        toss_account_seq=os.environ.get("TOSS_ACCOUNT_SEQ", ""),
        execute_orders=_bool("EXECUTE_ORDERS", False),
        max_orders_per_day=_int("MAX_ORDERS_PER_DAY", 10),
        max_order_amount_krw=_int("MAX_ORDER_AMOUNT_KRW", 100_000),
        sentiment_backend=os.environ.get("SENTIMENT_BACKEND", "gemini").strip().lower(),
        gemini_api_key=os.environ.get("GEMINI_API_KEY", ""),
        sentiment_model=os.environ.get("SENTIMENT_MODEL", "gemini-flash-lite-latest"),
        sentiment_batch_size=_int("SENTIMENT_BATCH_SIZE", 20),
        dart_api_key=os.environ.get("DART_API_KEY", ""),
        poll_prices_sec=_float("POLL_PRICES_SEC", 1.0),
        poll_orderbook_sec=_float("POLL_ORDERBOOK_SEC", 1.0),
        poll_trades_sec=_float("POLL_TRADES_SEC", 2.0),
    )


if __name__ == "__main__":
    s = get_settings()
    print("=== 설정 상태 (시크릿 값은 표시하지 않음) ===")
    for k, v in s.redacted().items():
        print(f"  {k:22} {v}")
    miss = s.missing()
    print()
    if miss:
        print("❌ 아직 채워야 할 키:", ", ".join(miss))
        print(f"   → {ROOT / '.env'} 를 편집하세요")
    else:
        print("✅ 필수 키가 모두 설정되었습니다")
    if s.execute_orders:
        print("\n⚠️  EXECUTE_ORDERS=true — 실제 주문이 나갑니다!")
