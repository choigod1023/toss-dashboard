"""SEC 13F — 기관 보유내역.

운용자산 1억달러 이상 기관투자자는 분기말 보유 주식을 공시해야 한다.
"유명 운용사가 이 종목을 샀는가"를 **추측이 아니라 공시로** 확인할 수 있다.

API 키가 필요 없다. 대신 SEC 는 **연락처가 포함된 User-Agent 를 요구**하고,
초당 10회 이하 요청을 요구한다 (미준수 시 차단).

⚠️ 이 데이터의 한계를 반드시 이해하고 써야 한다
  • 분기말 기준이고 **최대 45일 뒤에** 공시된다. '지금' 보유가 아니다.
  • 롱 주식 포지션만 담긴다. 공매도·채권·현금·해외상장은 빠진다.
  • 따라서 매매 신호가 아니라 **후행 참고 지표**다.
  • value 단위는 2023년 이후 '달러'다 (그 전 보고서는 천달러).
"""

from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime

import httpx
import psycopg

log = logging.getLogger(__name__)

# SEC 규정: 연락처를 포함한 User-Agent 필수
UA = {"User-Agent": "toss-dashboard/0.1 (personal research; j07801@hanyang.ac.kr)"}
NS = {"i": "http://www.sec.gov/edgar/document/thirteenf/informationtable"}
RATE_DELAY = 0.15   # 초당 10회 미만 유지

# 추적할 기관 (CIK). 필요하면 추가하면 된다.
INSTITUTIONS: dict[str, str] = {
    "0001067983": "Berkshire Hathaway",
    "0001350694": "Bridgewater Associates",
    "0001037389": "Renaissance Technologies",
    "0001167483": "Tiger Global",
    "0001656456": "Scion Asset Management",   # 마이클 버리
    "0000102909": "Vanguard Group",
    "0001364742": "BlackRock",
}


def _get(url: str, **kw) -> httpx.Response:
    time.sleep(RATE_DELAY)
    return httpx.get(url, headers=UA, timeout=30.0, follow_redirects=True, **kw)


def _text(el: ET.Element, tag: str) -> str | None:
    """빈 Element 는 falsy 라서 `or` 로 폴백하면 안 된다 (실제로 물렸던 버그)."""
    x = el.find(f"i:{tag}", NS)
    if x is None:
        x = el.find(tag)
    return x.text if x is not None else None


def latest_13f(cik: str) -> tuple[str, date, date] | None:
    """가장 최근 13F-HR 의 (accession, 기준일, 공시일)."""
    r = _get(f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json")
    if r.status_code != 200:
        log.warning("submissions %s HTTP %s", cik, r.status_code)
        return None
    rec = r.json()["filings"]["recent"]
    for i, form in enumerate(rec["form"]):
        if form == "13F-HR":
            filed = datetime.strptime(rec["filingDate"][i], "%Y-%m-%d").date()
            period_s = rec.get("reportDate", [None] * len(rec["form"]))[i]
            period = (datetime.strptime(period_s, "%Y-%m-%d").date()
                      if period_s else filed)
            return rec["accessionNumber"][i].replace("-", ""), period, filed
    return None


def fetch_holdings(cik: str, accession: str) -> list[dict]:
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession}/"
    r = _get(base + "index.json")
    if r.status_code != 200:
        return []
    names = [f["name"] for f in r.json()["directory"]["item"]]
    # infoTable 파일 찾기 (이름 규칙이 제출자마다 달라 확장자로 거른다)
    cands = [n for n in names
             if n.lower().endswith(".xml") and "primary_doc" not in n.lower()]
    for name in cands:
        rx = _get(base + name)
        if rx.status_code != 200:
            continue
        try:
            root = ET.fromstring(rx.content)
        except ET.ParseError:
            continue
        rows = root.findall(".//i:infoTable", NS) or root.findall(".//infoTable")
        if not rows:
            continue
        out = []
        for e in rows:
            try:
                val = float(_text(e, "value") or 0)
            except ValueError:
                val = 0.0
            sh_el = e.find("i:shrsOrPrnAmt", NS) or e.find("shrsOrPrnAmt")
            shares = None
            if sh_el is not None:
                try:
                    shares = float(_text(sh_el, "sshPrnamt") or 0)
                except ValueError:
                    shares = None
            out.append({
                "cusip": _text(e, "cusip"),
                "issuer": _text(e, "nameOfIssuer"),
                "value_usd": val,
                "shares": shares,
            })
        if out:
            return out
    return []


def collect(conn: psycopg.Connection,
            institutions: dict[str, str] | None = None) -> int:
    institutions = institutions or INSTITUTIONS
    total = 0
    for cik, name in institutions.items():
        meta = latest_13f(cik)
        if not meta:
            continue
        accession, period, filed = meta

        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM institution_holding WHERE cik=%s AND period=%s LIMIT 1",
                        (cik, period))
            if cur.fetchone():
                log.info("  %-26s %s 이미 적재됨", name, period)
                continue

        holds = fetch_holdings(cik, accession)
        if not holds:
            log.warning("  %-26s infoTable 없음", name)
            continue

        gross = sum(h["value_usd"] for h in holds) or 1
        rows = [(cik, name, period, filed, h["cusip"], h["issuer"],
                 h["value_usd"], h["shares"], h["value_usd"] / gross)
                for h in holds if h["cusip"]]
        with conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO institution_holding
                    (cik,institution,period,filed_at,cusip,issuer,value_usd,shares,weight)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (cik,period,cusip) DO UPDATE SET
                    value_usd=EXCLUDED.value_usd, shares=EXCLUDED.shares,
                    weight=EXCLUDED.weight
            """, rows)
        conn.commit()
        total += len(rows)
        log.info("  %-26s %s 기준 %3d종목  총 $%.1fB (공시 %s)",
                 name, period, len(rows), gross / 1e9, filed)
    return total


def link_tickers(conn: psycopg.Connection, limit: int = 4000) -> int:
    """CUSIP→티커 매핑.

    ⚠️ CUSIP↔티커 공식 무료 매핑은 없다 (CUSIP 은 유료 라이선스).
       그래서 SEC 의 company_tickers.json 발행사명으로 근사 매칭한다.

    성능: 발행사가 수만 건이라 중첩 루프를 쓰면 안 된다.
    정규화 키 + 접두 12자 딕셔너리로 O(n) 조회한다.
    """
    r = _get("https://www.sec.gov/files/company_tickers.json")
    if r.status_code != 200:
        return 0

    exact: dict[str, str] = {}
    prefix: dict[str, str] = {}
    for v in r.json().values():
        title = v["title"].upper().strip()
        exact.setdefault(title, v["ticker"])
        prefix.setdefault(title[:12], v["ticker"])

    with conn.cursor() as cur:
        # 비중 큰 것부터 — 꼬리까지 다 맞출 필요는 없다
        cur.execute("""SELECT issuer, sum(value_usd) FROM institution_holding
                       WHERE ticker IS NULL AND issuer IS NOT NULL
                       GROUP BY issuer ORDER BY 2 DESC LIMIT %s""", (limit,))
        issuers = [r[0] for r in cur.fetchall()]

        pairs = []
        for iss in issuers:
            up = iss.upper().strip()
            tk = exact.get(up) or prefix.get(up[:12])
            if tk:
                pairs.append((tk, iss))

        if pairs:
            cur.executemany(
                "UPDATE institution_holding SET ticker=%s WHERE issuer=%s AND ticker IS NULL",
                pairs)
    conn.commit()
    return len(pairs)
