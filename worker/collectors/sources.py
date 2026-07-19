"""외부 텍스트 소스 수집 — 네이버 뉴스 / Reddit / DART.

토스 API 에는 뉴스·커뮤니티·리포트가 전혀 없다. 여기가 그 공백을 메운다.

법적·정책 리스크 순으로 단계 도입:
  1) 네이버 검색 API  — 공식, 안전
  2) DART OpenAPI     — 정부 공식, 안전
  3) Reddit Data API  — 공식 OAuth (⚠️ user_agent 필수)
  4) 크롤링           — 최후. 여기서는 구현하지 않는다.
"""

from __future__ import annotations

import html
import logging
import os
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx
import psycopg

log = logging.getLogger(__name__)

_TAG = re.compile(r"<[^>]+>")


def _clean(s: str | None) -> str:
    if not s:
        return ""
    return html.unescape(_TAG.sub("", s)).strip()


def _upsert_posts(conn: psycopg.Connection, rows: list[tuple]) -> int:
    """community_post 적재. (source, external_id, posted_at) 로 중복 차단."""
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO community_post
                (source, external_id, symbol, posted_at, title, body, url)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (source, external_id, posted_at) DO NOTHING
        """, rows)
    conn.commit()
    return len(rows)


# ── 1. 네이버 뉴스 ───────────────────────────────────────────
def collect_naver_news(conn: psycopg.Connection, symbol: str, query: str,
                       display: int = 30) -> int:
    """네이버 검색 API (공식).

    헤더: X-Naver-Client-Id / X-Naver-Client-Secret  (실측 확인)
    응답 헤더 x-rate-limit-* 로 쿼터를 알려준다.
    """
    cid, csec = os.environ.get("NAVER_CLIENT_ID"), os.environ.get("NAVER_CLIENT_SECRET")
    if not cid or not csec:
        log.info("네이버 키 없음 — 건너뜀")
        return 0

    r = httpx.get(
        "https://openapi.naver.com/v1/search/news.json",
        headers={"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": csec},
        params={"query": query, "display": min(display, 100), "sort": "date"},
        timeout=30.0,
    )
    if r.status_code != 200:
        log.warning("네이버 %s: %s", r.status_code, r.text[:200])
        return 0

    rows = []
    for it in r.json().get("items", []):
        try:
            posted = parsedate_to_datetime(it["pubDate"])
        except Exception:
            posted = datetime.now(timezone.utc)
        link = it.get("originallink") or it.get("link") or ""
        rows.append((
            "naver_news", link[:300] or it["title"][:300], symbol, posted,
            _clean(it.get("title"))[:500], _clean(it.get("description"))[:2000], link,
        ))
    return _upsert_posts(conn, rows)


# ── 2. Reddit ────────────────────────────────────────────────
#  ⚠️ developers.reddit.com 은 Devvit(리딧 '안'에서 도는 앱 플랫폼)이라
#     외부 데이터 수집용이 아니다. 우리가 쓰는 건 기존 Data API 이고,
#     앱 등록은 https://old.reddit.com/prefs/apps 에서 타입 script 로 한다.
#     client_id(14자+) / client_secret(27자+) / user_agent 3개가 전부 필수.
_REDDIT_TOKEN: tuple[str, float] | None = None


def _reddit_token() -> str | None:
    """read-only(client_credentials) 토큰. 앱 소유자 권한으로 공개글만 읽는다."""
    global _REDDIT_TOKEN
    import time
    cid = os.environ.get("REDDIT_CLIENT_ID")
    csec = os.environ.get("REDDIT_CLIENT_SECRET")
    ua = os.environ.get("REDDIT_USER_AGENT")
    if not cid or not csec:
        return None
    if not ua or "YOUR_REDDIT_USERNAME" in ua:
        log.warning("REDDIT_USER_AGENT 를 실제 계정명으로 바꾸세요 — 미설정 시 차단됩니다")
        return None

    if _REDDIT_TOKEN and _REDDIT_TOKEN[1] > time.time() + 60:
        return _REDDIT_TOKEN[0]

    r = httpx.post(
        "https://www.reddit.com/api/v1/access_token",
        auth=(cid, csec),
        data={"grant_type": "client_credentials"},
        headers={"User-Agent": ua},
        timeout=30.0,
    )
    if r.status_code != 200:
        log.warning("Reddit 토큰 실패 %s: %s", r.status_code, r.text[:200])
        return None
    d = r.json()
    _REDDIT_TOKEN = (d["access_token"], time.time() + int(d.get("expires_in", 3600)))
    return _REDDIT_TOKEN[0]


def collect_reddit(conn: psycopg.Connection, subreddit: str,
                   symbol: str | None = None, limit: int = 50,
                   match: list[str] | None = None) -> int:
    """서브레딧 최신 글 수집.

    match 가 주어지면 제목·본문에 그 키워드(티커 등)가 있는 글만 남긴다.
    """
    tok = _reddit_token()
    if not tok:
        return 0
    ua = os.environ["REDDIT_USER_AGENT"]

    r = httpx.get(
        f"https://oauth.reddit.com/r/{subreddit}/new",
        headers={"Authorization": f"Bearer {tok}", "User-Agent": ua},
        params={"limit": min(limit, 100)},
        timeout=30.0,
    )
    if r.status_code != 200:
        log.warning("Reddit r/%s %s: %s", subreddit, r.status_code, r.text[:200])
        return 0

    rows = []
    for child in r.json().get("data", {}).get("children", []):
        d = child.get("data", {})
        title, body = d.get("title", ""), d.get("selftext", "")
        if match:
            blob = f"{title} {body}".upper()
            if not any(m.upper() in blob for m in match):
                continue
        rows.append((
            f"reddit:{subreddit}", d["id"], symbol,
            datetime.fromtimestamp(d.get("created_utc", 0), tz=timezone.utc),
            title[:500], body[:2000],
            f"https://reddit.com{d.get('permalink', '')}",
        ))
    return _upsert_posts(conn, rows)


# ── 3. DART ──────────────────────────────────────────────────
def collect_dart_disclosures(conn: psycopg.Connection, corp_code: str,
                             symbol: str, days: int = 90) -> int:
    """공시 목록. 금감원 공식 API 라 크롤링과 달리 안전하다."""
    key = os.environ.get("DART_API_KEY")
    if not key:
        return 0
    from datetime import timedelta
    end = datetime.now().date()
    r = httpx.get("https://opendart.fss.or.kr/api/list.json", params={
        "crtfc_key": key, "corp_code": corp_code,
        "bgn_de": (end - timedelta(days=days)).strftime("%Y%m%d"),
        "end_de": end.strftime("%Y%m%d"),
        "page_count": 100,
    }, timeout=30.0)
    d = r.json()
    if d.get("status") != "000":
        log.info("DART 공시 %s: %s", d.get("status"), d.get("message"))
        return 0

    rows = [(
        it["rcept_no"], symbol, it.get("corp_name"), it.get("report_nm"),
        datetime.strptime(it["rcept_dt"], "%Y%m%d").date(), it.get("flr_nm"),
        f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={it['rcept_no']}",
    ) for it in d.get("list", [])]

    with conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO disclosure (rcept_no,symbol,corp_name,report_name,
                                    rcept_date,submitter,url)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (rcept_no) DO NOTHING
        """, rows)
    conn.commit()
    return len(rows)


def collect_dart_indicators(conn: psycopg.Connection, corp_code: str, symbol: str,
                            year: str, reprt: str = "11011") -> int:
    """재무비율. ⚠️ PER/PBR 은 DART 에 없다 — 시세와 결합해 직접 계산해야 한다."""
    key = os.environ.get("DART_API_KEY")
    if not key:
        return 0
    total = 0
    for idx_cl in ("M210000", "M220000", "M230000", "M240000"):  # 수익성/안정성/성장성/활동성
        r = httpx.get("https://opendart.fss.or.kr/api/fnlttSinglIndx.json", params={
            "crtfc_key": key, "corp_code": corp_code,
            "bsns_year": year, "reprt_code": reprt, "idx_cl_code": idx_cl,
        }, timeout=30.0)
        d = r.json()
        if d.get("status") != "000":
            continue
        rows = []
        for it in d.get("list", []):
            try:
                val = float(str(it.get("idx_val")).replace(",", ""))
            except (TypeError, ValueError):
                continue
            rows.append((symbol, year, reprt, idx_cl, it.get("idx_nm"), val))
        with conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO financial_indicator
                    (symbol,bsns_year,reprt_code,idx_code,idx_name,idx_value)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (symbol,bsns_year,reprt_code,idx_code,idx_name)
                DO UPDATE SET idx_value=EXCLUDED.idx_value, fetched_at=now()
            """, rows)
        conn.commit()
        total += len(rows)
    return total
