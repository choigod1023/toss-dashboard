#!/usr/bin/env python3
"""상담 질문 1건 처리 — 웹 API 가 호출한다.

    python3 worker/ask.py <user_id> <question>
결과를 stdout 에 JSON 으로 낸다.
"""
import json
import os
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
LINE = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$')
ROOT = pathlib.Path(__file__).resolve().parent.parent
for f in (ROOT / "web" / ".env.local", ROOT / ".env"):
    if f.is_file():
        for raw in f.read_text().splitlines():
            m = LINE.match(raw.strip())
            if m and not raw.strip().startswith("#"):
                os.environ.setdefault(m.group(1), m.group(2).strip().strip('"\''))

import psycopg                      # noqa: E402
from analysis import advisor        # noqa: E402
from config import get_settings     # noqa: E402

if len(sys.argv) < 3:
    print(json.dumps({"error": "usage: ask.py <user_id> <question>"}))
    sys.exit(1)

user_id, question = sys.argv[1], " ".join(sys.argv[2:])
s = get_settings()
try:
    with psycopg.connect(s.database_url) as conn:
        out = advisor.ask(conn, s.sentiment_model, s.gemini_api_key, user_id, question)
    print(json.dumps(out, ensure_ascii=False))
except Exception as e:
    print(json.dumps({"error": f"{type(e).__name__}: {str(e)[:250]}"}, ensure_ascii=False))
    sys.exit(1)
