#!/usr/bin/env python3
"""
Fetch monthly revenue from MOPS (公開資訊觀測站).
URL: https://mopsov.twse.com.tw/mops/web/t05st10_ifrs
     https://mops.twse.com.tw/mops/web/t05st10_ifrs  (fallback)

Strategy:
  1. Batch POST (step=0) per market type → parse all rows from HTML
  2. Individual POST (step=1) per company as fallback
  3. Debug output on first call so logs show actual field structure

All values in 仟元 (thousands NTD) as returned by MOPS.
"""

import json
import os
import re
import sys
import time
import requests
from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser

TW_TZ         = timezone(timedelta(hours=8))
BASE           = os.path.dirname(__file__)
COMPANIES_FILE = os.path.join(BASE, "..", "companies.json")
OUTPUT_FILE    = os.path.join(BASE, "..", "data", "revenue.json")

MOPS_HOSTS = [
    "https://mopsov.twse.com.tw",
    "https://mops.twse.com.tw",
]
MOPS_PATH = "/mops/web/t05st10_ifrs"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# ─── utilities ───────────────────────────────────────────────────────────────

def load_companies():
    with open(COMPANIES_FILE, encoding="utf-8") as f:
        return json.load(f)


def safe_num(s):
    if s is None:
        return None
    s = str(s).replace(",", "").strip()
    if not s or s in ("-", "--", "N/A", "—", "na", "NA", "─"):
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def pct_change(cur, base):
    if cur is None or base is None or base == 0:
        return None
    return round((cur - base) / abs(base) * 100, 2)


def infer_report_month() -> str:
    now = datetime.now(TW_TZ)
    if now.month == 1:
        return f"{now.year - 1}-12"
    return f"{now.year}-{now.month - 1:02d}"


# ─── HTML table parser (depth-agnostic) ─────────────────────────────────────
#
# Key fix: no depth tracking – collect rows from ALL nested tables.
# MOPS wraps its data table inside a layout table, so any depth-limited
# parser will silently skip the actual data rows.

class _TP(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] = []
        self._buf: list[str] = []
        self._in_cell = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in ("td", "th"):
            self._in_cell = True
            self._buf = []
        elif tag == "tr":
            self._row = []

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("td", "th") and self._in_cell:
            self._row.append("".join(self._buf).strip())
            self._in_cell = False
        elif tag == "tr" and self._row:
            self.rows.append(self._row[:])
            self._row = []

    def handle_data(self, data):
        if self._in_cell:
            self._buf.append(data)

    def handle_entityref(self, name):
        pass  # ignore &nbsp; etc. in cell text

    def handle_charref(self, name):
        pass


def html_to_rows(html: str) -> list[list[str]]:
    p = _TP()
    p.feed(html)
    return p.rows


# ─── MOPS request helpers ────────────────────────────────────────────────────
#
# MOPS requires an established session: GET the page first to receive a
# session cookie, then POST the query with the same session object.

def _mops_url(host: str) -> str:
    return host + MOPS_PATH


def _make_session(url: str) -> requests.Session:
    """GET the MOPS page to obtain a session cookie, return the session."""
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,*/*",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    })
    sess.get(url, timeout=20)
    return sess


def _post(url: str, body: str, retries: int = 2) -> str:
    sess = _make_session(url)
    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Referer": url,
        "X-Requested-With": "XMLHttpRequest",
    }
    for attempt in range(retries):
        try:
            r = sess.post(
                url, data=body.encode("utf-8"),
                headers=headers, timeout=30,
            )
            r.raise_for_status()
            r.encoding = "utf-8"
            text = r.text.strip()
            if not text:
                raise ValueError("Empty body")
            # Detect homepage redirect (no query result)
            if "<title>公開資訊觀測站</title>" in text and "t05st10" not in text[:500]:
                raise ValueError("Got MOPS homepage instead of query result")
            return r.text
        except Exception as exc:
            if attempt == retries - 1:
                raise
            wait = 3 * (attempt + 1)
            print(f"    retry {attempt+1} ({wait}s): {exc}", file=sys.stderr)
            time.sleep(wait)


def _post_with_fallback(body: str) -> tuple[str, str]:
    """Try each MOPS host in order; return (html, url_used)."""
    last_exc = None
    for host in MOPS_HOSTS:
        url = _mops_url(host)
        try:
            html = _post(url, body)
            return html, url
        except Exception as exc:
            print(f"  host {host} failed: {exc}", file=sys.stderr)
            last_exc = exc
    raise RuntimeError(f"All MOPS hosts failed. Last: {last_exc}")


# ─── batch (step=0) parsing ──────────────────────────────────────────────────
#
# Batch table columns (0-indexed):
#   0  公司代號   ← used to key the result dict
#   1  公司名稱
#   2  當月營收
#   3  上月營收
#   4  去年當月營收
#   5  上月比較增減(%)
#   6  去年同月增減(%)
#   7  當月累計營收
#   8  去年累計營收
#   9  前期比較增減(%)

def _parse_batch_row(row: list[str]) -> dict | None:
    if len(row) < 9:
        return None
    code = row[0].strip()
    if not re.match(r"^\d{4,5}$", code):
        return None
    return {
        "revenue_month":           safe_num(row[2]),
        "revenue_prev_month":      safe_num(row[3]),
        "revenue_prev_year_month": safe_num(row[4]),
        "revenue_ytd":             safe_num(row[7]),
        "revenue_prev_year_ytd":   safe_num(row[8]),
    }


def fetch_batch(typek: str) -> dict[str, dict]:
    body = (
        f"encodeURIComponent=1&step=0&firstin=1&off=1"
        f"&TYPEK={typek}&year=&season="
    )
    html, url = _post_with_fallback(body)
    rows = html_to_rows(html)

    # debug: show first few rows so we can validate column layout
    print(f"  [debug] total rows parsed from HTML: {len(rows)}")
    for i, r in enumerate(rows[:5]):
        print(f"  [debug] row[{i}]: {r[:6]}")

    result: dict[str, dict] = {}
    for row in rows:
        rec = _parse_batch_row(row)
        if rec:
            result[row[0].strip()] = rec
    return result


# ─── individual (step=1) parsing ─────────────────────────────────────────────
#
# Individual query returns one company's history; the most recent row is first.
# Column layout (0-indexed):
#   0  年度/月份 (e.g. "115/05" or "11505" – NOT company code)
#   1  當月營收
#   2  上月營收
#   3  去年當月營收
#   4  上月比較增減(%)
#   5  去年同月增減(%)
#   6  當月累計營收
#   7  去年累計營收
#   8  前期比較增減(%)

def _parse_individual_row(row: list[str]) -> dict | None:
    """Pick first row where col[1] looks like a valid revenue number."""
    if len(row) < 7:
        return None
    # col[0] should be a year/month indicator, NOT a company code
    if re.match(r"^\d{4,5}$", row[0].strip()):
        # might be matched as company code – skip (shouldn't happen but guard)
        return None
    rev = safe_num(row[1])
    if rev is None:
        return None
    return {
        "revenue_month":           rev,
        "revenue_prev_month":      safe_num(row[2]) if len(row) > 2 else None,
        "revenue_prev_year_month": safe_num(row[3]) if len(row) > 3 else None,
        "revenue_ytd":             safe_num(row[6]) if len(row) > 6 else None,
        "revenue_prev_year_ytd":   safe_num(row[7]) if len(row) > 7 else None,
    }


def fetch_single(code: str, typek: str) -> dict | None:
    now    = datetime.now(TW_TZ)
    rev_m  = now.month - 1 if now.month > 1 else 12
    rev_y  = now.year      if now.month > 1 else now.year - 1
    roc_y  = rev_y - 1911

    body = (
        f"encodeURIComponent=1&step=1&firstin=1&off=1"
        f"&TYPEK={typek}&co_id={code}"
        f"&year={roc_y}&season={rev_m:02d}"
    )
    try:
        html, _ = _post_with_fallback(body)
        rows = html_to_rows(html)
        print(f"    [debug] {code} individual rows: {len(rows)}, first: {rows[0][:5] if rows else []}")
        for row in rows:
            rec = _parse_individual_row(row)
            if rec:
                return rec
    except Exception as exc:
        print(f"  [WARN] individual fetch failed {code}/{typek}: {exc}", file=sys.stderr)
    return None


# ─── main ────────────────────────────────────────────────────────────────────

def main():
    companies = load_companies()
    print(f"Loaded {len(companies)} companies\n")

    # Batch fetch for both market types
    all_data: dict[str, dict] = {}
    for typek in ("sii", "otc"):
        label = "上市 (sii)" if typek == "sii" else "上櫃 (otc)"
        print(f"Fetching batch {label} ...")
        try:
            batch = fetch_batch(typek)
            all_data.update(batch)
            print(f"  ✓ {len(batch)} companies parsed")
        except Exception as exc:
            print(f"  ✗ batch {typek} failed: {exc}", file=sys.stderr)

    print()

    # Build result list
    results = []
    for company in companies:
        code, market = company["code"], company["market"]
        rec = all_data.get(code)

        if rec is None:
            print(f"  {code} missing from batch → individual query ...")
            # Try declared market, then opposite
            for mk in (market, "otc" if market == "sii" else "sii"):
                rec = fetch_single(code, mk)
                if rec:
                    print(f"    ✓ {code} found via individual ({mk})")
                    break

        if rec is None:
            print(f"  [WARN] No data: {code} {company['name_zh']}")
            results.append({
                "code": code, "name_zh": company["name_zh"],
                "name_en": company["name_en"], "market": market,
                "error": "no_data",
            })
            continue

        rm, rpm, rpy = rec["revenue_month"], rec["revenue_prev_month"], rec["revenue_prev_year_month"]
        ryt, rpyt    = rec["revenue_ytd"], rec["revenue_prev_year_ytd"]

        entry = {
            "code": code, "name_zh": company["name_zh"],
            "name_en": company["name_en"], "market": market,
            "revenue_month":           rm,
            "revenue_prev_month":      rpm,
            "revenue_prev_year_month": rpy,
            "revenue_ytd":             ryt,
            "revenue_prev_year_ytd":   rpyt,
            "revenue_month_yoy": pct_change(rm,  rpy),
            "revenue_month_mom": pct_change(rm,  rpm),
            "revenue_ytd_yoy":   pct_change(ryt, rpyt),
        }
        results.append(entry)
        print(f"  {code} {company['name_zh']}: 月營收={rm} 仟元  YoY={entry['revenue_month_yoy']}%")

    now_tw = datetime.now(TW_TZ)
    output = {
        "updated_at":   now_tw.isoformat(),
        "report_month": infer_report_month(),
        "companies":    results,
    }

    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n✓ Saved → {OUTPUT_FILE}  (report_month: {output['report_month']})")


if __name__ == "__main__":
    main()
