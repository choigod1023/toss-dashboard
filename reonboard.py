#!/usr/bin/env python3
"""토스 자격증명 갱신 — 재발급했을 때 이것만 돌리면 된다.

    python3 reonboard.py <CLIENT_ID> <CLIENT_SECRET>

DB(user_credential)가 단일 출처이므로 여기만 갱신하면
워커·대시보드가 자동으로 새 값을 쓴다.
.env / Render / GitHub Secrets 를 고칠 필요가 없다.
"""
import os, sys, pathlib, re, psycopg

sys.path.insert(0, "worker")
LINE = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$')
for f in (".env", "web/.env.local"):
    p = pathlib.Path(f)
    if p.is_file():
        for raw in p.read_text().splitlines():
            m = LINE.match(raw.strip())
            if m and not raw.strip().startswith("#"):
                v = m.group(2).strip().strip('"').strip("'")
                os.environ.setdefault(m.group(1), v)

from config import get_settings          # noqa: E402
import accounts as A                     # noqa: E402

if len(sys.argv) != 3:
    sys.exit(__doc__)
cid, csec = sys.argv[1].strip(), sys.argv[2].strip()

s = get_settings()
conn = psycopg.connect(s.database_url)
try:
    r = A.onboard(conn, cid, csec, nickname="collector")
    with conn.cursor() as cur:
        cur.execute("UPDATE user_credential SET is_collector=false WHERE is_collector")
        cur.execute("UPDATE user_credential SET is_collector=true WHERE user_id=%s",
                    (r["user_id"],))
    conn.commit()
    print(f"✅ 갱신 완료")
    print(f"   user_id     {r['user_id']}")
    print(f"   accountSeq  {r['account_seq']}")
    print(f"   수집용 자격증명으로 지정됨")
except ValueError as e:
    sys.exit(f"❌ {e}")
finally:
    conn.close()
