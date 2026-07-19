"""데이터 보존 정리.

Neon 의 TimescaleDB 는 apache 라이선스라 add_retention_policy() 가 없다.
대신 drop_chunks() 는 동작하므로, 워커가 이 모듈을 주기적으로 호출해
직접 오래된 청크를 떨군다. (APScheduler 에서 하루 1회 실행 권장)

    python3 worker/db/retention.py --dry-run   # 무엇이 지워질지만 확인
    python3 worker/db/retention.py             # 실제 정리

⚠️ 512MB 상한이라 이걸 안 돌리면 1분봉만으로 40일 안에 꽉 찬다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import get_settings  # noqa: E402

# 하이퍼테이블별 보존 기간.
#  candle 은 1분봉/일봉이 한 테이블에 섞여 있어 청크 단위로 못 나눈다.
#  → 일봉은 장기 보관해야 하므로 drop_chunks 를 쓰지 않고,
#    1분봉만 DELETE 로 지운다 (아래 MINUTE_CANDLE_DAYS).
RETENTION = {
    "community_post": 180,   # 원문은 오래 들고 있을 이유가 없다
    "sentiment_score": 365,  # 점수는 가벼우니 더 길게 (백테스팅 재료)
}

MINUTE_CANDLE_DAYS = 90      # 1분봉 보존 (일봉은 무기한)
RATE_LIMIT_OBS_DAYS = 14     # 관측 로그
JOB_RUN_DAYS = 30


def run(conn: psycopg.Connection, dry: bool) -> None:
    tag = "[예상]" if dry else "[실행]"
    with conn.cursor() as cur:
        # 1) 하이퍼테이블 — drop_chunks (청크 통째로 떨궈서 빠르다)
        for table, days in RETENTION.items():
            cur.execute(
                "SELECT show_chunks(%s, older_than => %s::interval)",
                (table, f"{days} days"),
            )
            chunks = [r[0] for r in cur.fetchall()]
            print(f"{tag} {table:20} {days}일 초과 청크 {len(chunks)}개")
            if chunks and not dry:
                cur.execute(
                    "SELECT drop_chunks(%s, older_than => %s::interval)",
                    (table, f"{days} days"),
                )

        # 2) candle — 1분봉만 선별 삭제 (일봉은 유지)
        cur.execute(
            "SELECT count(*) FROM candle "
            "WHERE interval = '1m' AND ts < now() - %s::interval",
            (f"{MINUTE_CANDLE_DAYS} days",),
        )
        n = cur.fetchone()[0]
        print(f"{tag} candle(1m)           {MINUTE_CANDLE_DAYS}일 초과 {n:,}행")
        if n and not dry:
            cur.execute(
                "DELETE FROM candle "
                "WHERE interval = '1m' AND ts < now() - %s::interval",
                (f"{MINUTE_CANDLE_DAYS} days",),
            )

        # 3) 운영 로그
        for table, col, days in (
            ("rate_limit_observation", "observed_at", RATE_LIMIT_OBS_DAYS),
            ("job_run", "started_at", JOB_RUN_DAYS),
        ):
            cur.execute(
                f"SELECT count(*) FROM {table} WHERE {col} < now() - %s::interval",
                (f"{days} days",),
            )
            n = cur.fetchone()[0]
            print(f"{tag} {table:20} {days}일 초과 {n:,}행")
            if n and not dry:
                cur.execute(
                    f"DELETE FROM {table} WHERE {col} < now() - %s::interval",
                    (f"{days} days",),
                )

    if not dry:
        # DELETE 는 공간을 즉시 반환하지 않는다. 512MB 상한에선 중요.
        with conn.cursor() as cur:
            cur.execute("VACUUM (ANALYZE) candle")
            cur.execute("VACUUM (ANALYZE) rate_limit_observation")
        print("VACUUM 완료 (DELETE 로 회수된 공간 반환)")


def main() -> None:
    dry = "--dry-run" in sys.argv
    s = get_settings()
    if not s.database_url:
        sys.exit("DATABASE_URL 이 없습니다.")
    with psycopg.connect(s.database_url, autocommit=True) as conn:
        run(conn, dry)
        with conn.cursor() as cur:
            cur.execute("SELECT pg_database_size(current_database())")
            mb = cur.fetchone()[0] / 1024 / 1024
        print(f"\n현재 용량: {mb:.1f}MB / 512MB ({mb/512*100:.1f}%)")


if __name__ == "__main__":
    main()
