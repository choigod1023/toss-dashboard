"""스키마 적용 + 용량 점검.

    python3 worker/db/apply.py           # 스키마 적용 후 현황 출력
    python3 worker/db/apply.py --status  # 적용 없이 현황만

Neon 무료 티어 상한이 512MB 라서, 적용할 때마다 사용량을 같이 보여준다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import get_settings  # noqa: E402

SCHEMA = Path(__file__).with_name("schema.sql")
LIMIT_BYTES = 512 * 1024 * 1024


def apply_schema(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(SCHEMA.read_text(encoding="utf-8"))
    print(f"✅ 스키마 적용 완료 ({SCHEMA.name})")


def show_status(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_database_size(current_database())")
        used = cur.fetchone()[0]
        pct = used / LIMIT_BYTES * 100
        bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
        print(f"\n── 용량 ──  {used/1024/1024:.1f}MB / 512MB  [{bar}] {pct:.1f}%")
        if pct > 70:
            print("   ⚠️  70% 초과 — retention.py 로 오래된 청크를 정리하세요")

        cur.execute("""
            SELECT c.relname,
                   pg_total_relation_size(c.oid) AS bytes,
                   (SELECT reltuples::bigint FROM pg_class WHERE oid = c.oid) AS approx_rows
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
            ORDER BY bytes DESC
        """)
        rows = cur.fetchall()
        if rows:
            print("\n── 테이블 ──")
            for name, size, approx in rows:
                if size > 0:
                    print(f"   {name:26} {size/1024:>9.0f} KB  (~{max(approx,0):,}행)")

        cur.execute("""
            SELECT hypertable_name, num_chunks
            FROM timescaledb_information.hypertables
            WHERE hypertable_schema = 'public'
            ORDER BY hypertable_name
        """)
        hts = cur.fetchall()
        if hts:
            print("\n── 하이퍼테이블 ──")
            for name, chunks in hts:
                print(f"   {name:26} 청크 {chunks}개")
        print("\n   (압축·보존정책은 Neon 의 apache 라이선스에서 미지원 →")
        print("    보존은 worker/db/retention.py 의 drop_chunks 로 처리)")


def main() -> None:
    s = get_settings()
    if not s.database_url:
        sys.exit("DATABASE_URL 이 없습니다. cd web && vercel env pull .env.local")

    with psycopg.connect(s.database_url, autocommit=True) as conn:
        if "--status" not in sys.argv:
            apply_schema(conn)
        show_status(conn)


if __name__ == "__main__":
    main()
