"""온보딩 검증 프록시.

왜 필요한가
  토스 Open API 는 IP 화이트리스트 방식이다. Vercel 은 요청마다 IP 가
  달라서 등록이 불가능하고, 직접 호출하면 403 ip-not-allowed 가 난다.
  → 고정 egress IP 를 가진 이 워커가 대신 호출한다.
    사용자는 **이 서버 IP 하나만** 토스에 등록하면 된다.

보안
  이 엔드포인트는 남의 자격증명을 받는다. 반드시
    • INTERNAL_API_TOKEN 으로 호출자를 인증한다 (Vercel 만 알고 있음)
    • 자격증명을 로그에 남기지 않는다
    • 검증 실패 시 저장하지 않는다
  주문 API 는 이 프로세스에 존재하지 않는다 (main.py 의 하드 게이트와 별개로,
  여기서는 아예 호출 경로가 없다).
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import psycopg  # noqa: E402

import accounts as ACC  # noqa: E402
from collectors import jobs as J  # noqa: E402
from config import get_settings  # noqa: E402

log = logging.getLogger("api")
S = get_settings()
TOKEN = os.environ.get("INTERNAL_API_TOKEN", "")


def _auth(headers) -> bool:
    if not TOKEN:
        log.error("INTERNAL_API_TOKEN 미설정 — 모든 요청을 거부한다")
        return False
    got = (headers.get("Authorization") or "").removeprefix("Bearer ").strip()
    return hmac.compare_digest(got, TOKEN)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: dict) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):
        # 기본 로거는 쿼리스트링까지 찍는다. 경로만 남긴다.
        log.info("%s %s", self.command, self.path.split("?")[0])

    def do_GET(self):
        if self.path.startswith("/health"):
            ip = None
            try:
                import httpx
                ip = httpx.get("https://api.ipify.org", timeout=8).text.strip()
            except Exception:
                pass
            return self._send(200, {"ok": True, "outbound_ip": ip})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.startswith("/verify"):
            return self._send(404, {"error": "not found"})
        if not _auth(self.headers):
            return self._send(401, {"error": "unauthorized"})

        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, {"error": "잘못된 요청 형식입니다."})

        cid = str(body.get("clientId") or "").strip()
        csec = str(body.get("clientSecret") or "").strip()
        if not cid or not csec:
            return self._send(400, {"error": "Client ID 와 Secret 을 모두 입력해주세요."})

        try:
            with psycopg.connect(S.database_url) as conn:
                # 검증 → 저장 → 세션 발급까지 한 번에 (accounts.onboard)
                r = ACC.onboard(conn, cid, csec,
                                user_agent=self.headers.get("X-Forwarded-UA"))
                # 방금 검증에 성공한 자격증명을 수집용으로 승격한다.
                with conn.cursor() as cur:
                    cur.execute("UPDATE user_credential SET is_collector=false WHERE is_collector")
                    cur.execute("UPDATE user_credential SET is_collector=true WHERE user_id=%s",
                                (r["user_id"],))
                conn.commit()
        except ValueError as e:
            msg = str(e)
            if "403" in msg:
                return self._send(403, {
                    "error": "IP_NOT_ALLOWED",
                    "message": "토스증권에 이 서버 IP 가 등록되지 않았습니다.",
                })
            return self._send(401, {"error": msg[:200]})
        except Exception as e:
            log.exception("verify 실패")
            return self._send(500, {"error": f"{type(e).__name__}"})

        # 검증 성공 → 즉시 수집 시작.
        # 이게 없으면 가입 직후 대시보드가 텅 비어 있고 다음 스케줄까지
        # 기다려야 한다. 응답은 먼저 보내고 수집은 백그라운드로 돌린다.
        fresh = _is_fresh()
        if not fresh:
            threading.Thread(target=_kickoff, args=(r["user_id"],),
                             daemon=True).start()

        self._send(200, {"ok": True, "userId": r["user_id"],
                         "sessionToken": r["session_token"],
                         "accountSeq": r["account_seq"],
                         "collecting": not fresh,
                         "reusedData": fresh})


_kick_lock = threading.Lock()

# 최근에 수집했으면 다시 긁지 않는다.
# 일봉·지표·13F 는 하루 단위로 바뀌는 데이터라 재온보딩마다 다시 받을
# 이유가 없다. 토스 rate limit 과 Gemini 호출만 낭비된다.
FRESH_MINUTES = 30


def _is_fresh() -> bool:
    try:
        with psycopg.connect(S.database_url) as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT max(started_at) > now() - make_interval(mins => %s)
                  FROM job_run WHERE job_name = 'portfolio_all' AND ok
            """, (FRESH_MINUTES,))
            row = cur.fetchone()
        return bool(row and row[0])
    except Exception as e:
        log.warning("신선도 판정 실패 — 수집을 진행한다: %s", str(e)[:120])
        return False


def _kickoff(user_id: str) -> None:
    """온보딩 직후 첫 수집. 별도 프로세스로 띄운다.

    같은 프로세스에서 돌리면 HTTP 스레드를 몇 분 붙잡고,
    실패 시 API 전체가 흔들린다. subprocess 로 격리한다.
    """
    if not _kick_lock.acquire(blocking=False):
        log.info("이미 수집이 진행 중 — 건너뜀")
        return
    if _is_fresh():
        log.info("최근 %d분 내 수집 기록이 있어 재수집을 건너뛴다", FRESH_MINUTES)
        _kick_lock.release()
        return
    try:
        import subprocess
        log.info("첫 수집 시작 (user=%s)", user_id[:8])
        subprocess.run([sys.executable, "worker/main.py", "daily"],
                       cwd=str(Path(__file__).resolve().parent.parent),
                       timeout=1800, check=False)
        log.info("첫 수집 완료")
    except Exception as e:
        log.warning("첫 수집 실패: %s", str(e)[:200])
    finally:
        _kick_lock.release()


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s api | %(message)s",
                        datefmt="%H:%M:%S")
    if not TOKEN:
        log.warning("INTERNAL_API_TOKEN 이 없습니다 — /verify 는 전부 401 입니다")
    # 부팅 시 자기 IP 를 DB 에 보고해 온보딩 화면이 안내할 수 있게 한다
    try:
        with psycopg.connect(S.database_url) as conn:
            J.report_ip(conn)
    except Exception as e:
        log.warning("IP 보고 실패: %s", str(e)[:120])

    port = int(os.environ.get("PORT", "8080"))

    # ⚠️ Fly 내부망은 IPv6 다. 0.0.0.0(IPv4 전용)으로 바인딩하면
    #    프로세스는 살아있는데 프록시가 못 붙어 Connection refused 가 난다.
    #    dual-stack 으로 열어야 한다.
    import socket

    class DualStack(ThreadingHTTPServer):
        address_family = socket.AF_INET6
        daemon_threads = True

        def server_bind(self):
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            super().server_bind()

    log.info("검증 프록시 시작 — [::]:%d (dual-stack)", port)
    DualStack(("::", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
