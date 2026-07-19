"""RSS 수집기.

왜 RSS 인가
  • Reddit 은 2025-11 **Responsible Builder Policy** 이후 API 앱 생성에
    사전 승인이 필요해졌다. 반면 `/.rss` 는 승인 없이 열려 있다.
  • RSS 는 애초에 '배포 목적으로 발행된' 피드다. 임의 HTML 크롤링과
    달리 약관·저작권 리스크가 낮다.
  • 국내 언론사 증권 섹션도 RSS 를 제공한다 — 애널리스트 목표주가
    기사가 여기로 들어온다.

주의
  • Reddit 은 연속 호출 시 429 를 준다. 피드 간 딜레이를 반드시 둔다.
  • RSS 는 제목+요약만 준다 (본문 전체 아님). 감성 분류에는 대체로 충분.
  • 전문은 링크로만 참조한다 — 원문을 통째로 복사 저장하지 않는다.
"""

from __future__ import annotations

import html
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx
import psycopg

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) toss-dashboard/0.1 (personal use)"
ATOM = {"a": "http://www.w3.org/2005/Atom"}
_TAG = re.compile(r"<[^>]+>")

# 피드 레지스트리. (source_key, url, 국내여부)
#  ⚠️ 실제 응답을 확인한 것만 등록한다 (404/차단 피드는 뺐다).
FEEDS: list[tuple[str, str, bool]] = [
    # ── 국내 증권·경제 (애널리스트 목표주가 기사가 여기로 들어온다) ──
    ("hankyung_finance", "https://www.hankyung.com/feed/finance", True),
    ("hankyung_economy", "https://www.hankyung.com/feed/economy", True),
    ("mk_stock",         "https://www.mk.co.kr/rss/50200011/", True),
    ("mk_economy",       "https://www.mk.co.kr/rss/30100041/", True),
    ("yna_economy",      "https://www.yna.co.kr/rss/economy.xml", True),
    ("yna_market",       "https://www.yna.co.kr/rss/market.xml", True),
    ("fnnews_stock",     "https://www.fnnews.com/rss/r20/fn_realnews_stock.xml", True),
    ("infostock",        "https://www.infostockdaily.co.kr/rss/allArticle.xml", True),
    # ── 해외 시장 ──
    ("yahoo_finance",    "https://finance.yahoo.com/news/rssindex", False),
    ("marketwatch",      "https://feeds.content.dowjones.io/public/rss/mw_topstories", False),
    ("investing_com",    "https://www.investing.com/rss/news_25.rss", False),
    # ── 해외 커뮤니티 (Responsible Builder Policy 로 API 막힘 → RSS) ──
    ("reddit:stocks",    "https://www.reddit.com/r/stocks/.rss", False),
    ("reddit:investing", "https://www.reddit.com/r/investing/.rss", False),
    ("reddit:wsb",       "https://www.reddit.com/r/wallstreetbets/.rss", False),
    # ── 유튜브 (채널 RSS — API 키 불필요) ──
    #  영상 '제목+설명'만 들어온다. 내용 분석은 안 되지만
    #  어떤 종목·테마가 다뤄지는지 신호로는 충분하다.
    ("yt:삼프로TV",      "https://www.youtube.com/feeds/videos.xml?channel_id=UChlv4GSd7OQl3js-jkLOnFA", True),
    ("yt:슈카월드",      "https://www.youtube.com/feeds/videos.xml?channel_id=UCsJ6RuBiTVWRX156FVbeaGg", True),
    ("yt:Bloomberg",   "https://www.youtube.com/feeds/videos.xml?channel_id=UCIALMKvObZNtJ6AmdCLP7Lg", False),
]

# Reddit 은 특히 민감 — 피드 사이 딜레이(초)
DELAY_DEFAULT = 1.5
DELAY_REDDIT = 12.0


def _clean(s: str | None, limit: int) -> str:
    if not s:
        return ""
    return html.unescape(_TAG.sub(" ", s)).replace("\xa0", " ").strip()[:limit]


def _when(entry: ET.Element) -> datetime:
    for tag, ns in (("a:updated", ATOM), ("a:published", ATOM)):
        el = entry.find(tag, ns)
        if el is not None and el.text:
            try:
                return datetime.fromisoformat(el.text.replace("Z", "+00:00"))
            except ValueError:
                pass
    for tag in ("pubDate", "date"):
        el = entry.find(tag)
        if el is not None and el.text:
            try:
                return parsedate_to_datetime(el.text)
            except Exception:
                pass
    return datetime.now(timezone.utc)


def _parse(xml: bytes) -> list[dict]:
    root = ET.fromstring(xml)
    out: list[dict] = []

    for e in root.findall(".//a:entry", ATOM):        # Atom (Reddit)
        link = ""
        le = e.find("a:link", ATOM)
        if le is not None:
            link = le.get("href") or ""
        out.append({
            "id": (e.findtext("a:id", "", ATOM) or link),
            "title": _clean(e.findtext("a:title", "", ATOM), 500),
            "body": _clean(e.findtext("a:content", "", ATOM), 2000),
            "url": link,
            "at": _when(e),
        })

    for e in root.findall(".//item"):                  # RSS 2.0 (국내 언론)
        link = e.findtext("link", "") or ""
        out.append({
            "id": (e.findtext("guid", "") or link),
            "title": _clean(e.findtext("title", ""), 500),
            "body": _clean(e.findtext("description", ""), 2000),
            "url": link,
            "at": _when(e),
        })
    return out


def _match_symbol(text: str, index: list[tuple[str, list[str]]]) -> str | None:
    """제목·본문에서 종목을 찾는다.

    index: [(symbol, [별칭...]), ...]
    한글 종목명은 그대로, 미국 티커는 단어 경계로 매칭한다
    (ONDS 가 'seconds' 안에서 잡히면 안 된다).
    """
    up = text.upper()
    for sym, aliases in index:
        for a in aliases:
            if not a:
                continue
            if re.search(r"[가-힣]", a):
                if a in text:
                    return sym
            elif re.search(rf"\b{re.escape(a.upper())}\b", up):
                return sym
    return None


def build_symbol_index(conn: psycopg.Connection) -> list[tuple[str, list[str]]]:
    with conn.cursor() as cur:
        cur.execute("SELECT symbol, name, english_name, market_country FROM ("
                    "SELECT symbol, name, english_name, NULL AS market_country "
                    "FROM stock) s")
        rows = cur.fetchall()
    idx = []
    for sym, name, eng, _ in rows:
        aliases = [name]
        # 미국 티커는 심볼 자체가 별칭 (KRX 6자리 숫자는 본문에 안 나온다)
        if not sym.isdigit():
            aliases.append(sym)
            if eng:
                aliases.append(eng)
        idx.append((sym, [a for a in aliases if a]))
    return idx


def collect_feed(conn: psycopg.Connection, source: str, url: str,
                 index: list[tuple[str, list[str]]],
                 require_symbol: bool = False) -> tuple[int, int]:
    """피드 1개 수집. (수집, 종목매칭) 반환."""
    try:
        r = httpx.get(url, headers={"User-Agent": UA}, timeout=25.0,
                      follow_redirects=True)
    except Exception as e:
        log.warning("[%s] 요청 실패: %s", source, str(e)[:120])
        return 0, 0
    if r.status_code != 200:
        log.warning("[%s] HTTP %s%s", source, r.status_code,
                    " (연속 호출 제한 — 딜레이를 늘리세요)" if r.status_code == 429 else "")
        return 0, 0

    try:
        entries = _parse(r.content)
    except ET.ParseError as e:
        log.warning("[%s] 파싱 실패: %s", source, str(e)[:120])
        return 0, 0

    rows, matched = [], 0
    for e in entries:
        if not e["title"]:
            continue
        sym = _match_symbol(f"{e['title']} {e['body']}", index)
        if sym:
            matched += 1
        elif require_symbol:
            continue
        rows.append((source, (e["id"] or e["url"])[:300], sym, e["at"],
                     e["title"], e["body"], e["url"]))

    if rows:
        with conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO community_post
                    (source, external_id, symbol, posted_at, title, body, url)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (source, external_id, posted_at) DO NOTHING
            """, rows)
        conn.commit()
    return len(rows), matched


def collect_all(conn: psycopg.Connection, only_matched_domestic: bool = True) -> int:
    """전체 피드 순회.

    only_matched_domestic: 국내 뉴스는 종목이 매칭된 기사만 저장한다.
      (512MB 상한 — 시황 기사를 전부 넣으면 금방 찬다)
    """
    index = build_symbol_index(conn)
    log.info("종목 인덱스 %d개로 매칭", len(index))
    total = 0
    for i, (source, url, domestic) in enumerate(FEEDS):
        if i:
            time.sleep(DELAY_REDDIT if "reddit" in source else DELAY_DEFAULT)
        got, matched = collect_feed(
            conn, source, url, index,
            require_symbol=(domestic and only_matched_domestic),
        )
        total += got
        log.info("  %-20s %3d건 저장 (종목매칭 %d)", source, got, matched)
    return total
