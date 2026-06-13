#!/usr/bin/env python3
"""
Fetch monthly revenue for all listed/OTC companies from MOPS (公開資訊觀測站).

Single source for both 上市 (sii) and 上櫃 (otc):
  https://mopsov.twse.com.tw/mops/web/t05st10_ifrs

Strategy:
  1. POST step=0 (batch) for sii → parse HTML table
  2. POST step=0 (batch) for otc → parse HTML table
  3. For companies still missing, POST step=1 (individual) per company
  4. Compute YoY / MoM from raw revenue figures

All values stored in 仟元 (thousands NTD) as returned by MOPS.
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

MOPS_URL = "https://mopsov.twse.com.tw/mops/web/t05st10_ifrs"

_BROWSER_UA = (
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
    if not s or s in ("-", "--", "N/A", "—", "na", "NA"):
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def pct_change(cur, base):
    if cur is None or base is None or base == 0:
        return None
    return round((cur - base) / abs(base) * 100, 2)


def infer_report_month(date_str: str) -> str:
    """
    The report published on the 12th covers last month.
    date_str may be ROC 7-digit (YYYMMDD) or blank.
    Returns 'YYYY-MM' for the revenue month.
    """
    now = datetime.now(TW_TZ)
    if date_str:
        ds = date_str.replace("/", "").replace("-", "").strip()
        try:
            if len(ds) == 7:   # ROC: YYYMMDD
                year, month = int(ds[:3]) + 1911, int(ds[3:5])
            elif len(ds) == 8: # Gregorian: YYYYMMDD
                year, month = int(ds[:4]), int(ds[4:6])
            else:
                raise ValueError
            # The report date is the *filing* date; revenue month is month-1
            if month == 1:
                return f"{year - 1}-12"
            return f"{year}-{month - 1:02d}"
        except (ValueError, IndexError):
            pass
    if now.month == 1:
        return f"{now.year - 1}-12"
    return f"{now.year}-{now.month - 1:02d}"


# ─── HTML table parser ───────────────────────────────────────────────────────

class _TP(HTMLParser):
    """Minimal, tolerant HTML table → list[list[str]] parser."""
    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] = []
        self._buf: list[str] = []
        self._in_cell = False
        self._depth = 0  # handle nested tables

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._depth += 1
        elif self._depth == 1:
            if tag in ("td", "th"):
                self._in_cell = True
                self._buf = []
            elif tag == "tr":
                self._row = []

    def handle_endtag(self, tag):
        if tag == "table":
            self._depth = max(0, self._depth - 1)
        elif self._depth == 1:
            if tag in ("td", "th") and self._in_cell:
                self._row.append("".join(self._buf).strip())
                self._in_cell = False
            elif tag == "tr" and self._row:
                self.rows.append(self._row[:])
                self._row = []

    def handle_data(self, data):
        if self._in_cell:
            self._buf.append(data)


def _html_to_table(html: str) -> list[list[str]]:
    p = _TP()
    p.feed(html)
    return p.rows


# ─── MOPS request helpers ────────────────────────────────────────────────────

def _mops_headers():
    return {
        "User-Agent": _BROWSER_UA,
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Referer": MOPS_URL,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "text/html,application/xhtml+xml,*/*",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    }


def _post(body: str, retries: int = 3) -> str:
    for attempt in range(retries):
        try:
            r = requests.post(
                MOPS_URL,
                data=body.encode("utf-8"),
                headers=_mops_headers(),
                timeout=60,
            )
            r.raise_for_status()
            r.encoding = "utf-8"
            if r.text.strip():
                return r.text
            raise ValueError("Empty response body")
        except Exception as exc:
            if attempt == retries - 1:
                raise
            wait = 4 * (attempt + 1)
            print(f"  Retry {attempt + 1}/{retries - 1} (wait {wait}s): {exc}", file=sys.stderr)
            time.sleep(wait)


# ─── MOPS column layout ──────────────────────────────────────────────────────
#
# The MOPS t05st10_ifrs batch table (step=0) has these columns (0-indexed):
#   0  公司代號
#   1  公司名稱
#   2  當月營收         ← revenue_month
#   3  上月營收         ← revenue_prev_month
#   4  去年當月營收     ← revenue_prev_year_month
#   5  上月比較增減(%)
#   6  去年同月增減(%)
#   7  當月累計營收     ← revenue_ytd
#   8  去年累計營收     ← revenue_prev_year_ytd
#   9  前期比較增減(%)
#
# For individual (step=1) queries the columns are the same but each row
# represents one month; the first data row is the most recent month.

_COL = dict(
    code=0, name=1,
    rev_month=2, rev_prev_month=3, rev_prev_year_month=4,
    rev_ytd=7, rev_prev_year_ytd=8,
)


def _row_to_record(row: list[str]) -> dict | None:
    """Convert a MOPS table row → revenue dict, or None if not a data row."""
    if len(row) < 9:
        return None
    code = row[_COL["code"]].strip()
    if not re.match(r"^\d{4,5}$", code):
        return None
    return {
        "code":                    code,
        "revenue_month":           safe_num(row[_COL["rev_month"]]),
        "revenue_prev_month":      safe_num(row[_COL["rev_prev_month"]]),
        "revenue_prev_year_month": safe_num(row[_COL["rev_prev_year_month"]]),
        "revenue_ytd":             safe_num(row[_COL["rev_ytd"]]),
        "revenue_prev_year_ytd":   safe_num(row[_COL["rev_prev_year_ytd"]]),
    }


def fetch_batch(typek: str) -> dict[str, dict]:
    """Fetch all companies of one market type via step=0."""
    body = (
        f"encodeURIComponent=1&step=0&firstin=1&off=1"
        f"&TYPEK={typek}&year=&season="
    )
    html  = _post(body)
    rows  = _html_to_table(html)
    result: dict[str, dict] = {}
    for row in rows:
        rec = _row_to_record(row)
        if rec:
            result[rec["code"]] = rec
    return result


def fetch_single(code: str, typek: str) -> dict | None:
    """Fetch one company via step=1 (individual query)."""
    # Calculate previous month in ROC format
    now      = datetime.now(TW_TZ)
    rev_m    = now.month - 1 if now.month > 1 else 12
    rev_y    = now.year      if now.month > 1 else now.year - 1
    roc_year = rev_y - 1911

    body = (
        f"encodeURIComponent=1&step=1&firstin=1&off=1"
        f"&TYPEK={typek}&co_id={code}"
        f"&year={roc_year}&season={rev_m:02d}"
    )
    try:
        html = _post(body)
        rows = _html_to_table(html)
        # First data row (skip header rows) = most recent month
        for row in rows:
            rec = _row_to_record(row)
            if rec:
                # Individual queries return a table where col 0 may be year/month,
                # not code. Detect by checking if code matches.
                # Try without code validation for individual queries:
                if safe_num(row[2]) is not None:  # has revenue value
                    return {
                        "code":                    code,
                        "revenue_month":           safe_num(row[2]),
                        "revenue_prev_month":      safe_num(row[3]) if len(row) > 3 else None,
                        "revenue_prev_year_month": safe_num(row[4]) if len(row) > 4 else None,
                        "revenue_ytd":             safe_num(row[7]) if len(row) > 7 else None,
                        "revenue_prev_year_ytd":   safe_num(row[8]) if len(row) > 8 else None,
                    }
    except Exception as exc:
        print(f"  [WARN] single fetch failed for {code}: {exc}", file=sys.stderr)
    return None


# ─── report-date extraction ──────────────────────────────────────────────────

def extract_report_date(html: str) -> str:
    """Try to find the report/publish date in the MOPS HTML."""
    m = re.search(r"(\d{3})\s*年\s*(\d{1,2})\s*月", html)
    if m:
        roc_y, month = int(m.group(1)), int(m.group(2))
        return f"{roc_y + 1911:04d}{month:02d}01"
    return ""


# ─── main ────────────────────────────────────────────────────────────────────

def main():
    companies = load_companies()
    print(f"Loaded {len(companies)} companies\n")

    # ── batch fetch for both market types ──────────────────────────────────
    all_data: dict[str, dict] = {}
    report_date_str = ""

    for typek in ("sii", "otc"):
        label = "上市 (sii)" if typek == "sii" else "上櫃 (otc)"
        print(f"Fetching batch {label} from MOPS...")
        try:
            batch = fetch_batch(typek)
            all_data.update(batch)
            print(f"  ✓ {len(batch)} records")
        except Exception as exc:
            print(f"  ✗ batch {typek} failed: {exc}", file=sys.stderr)

    # ── build results ───────────────────────────────────────────────────────
    print()
    results = []

    for company in companies:
        code, market = company["code"], company["market"]
        rec = all_data.get(code)

        if rec is None:
            # Try individual query with declared market type first, then opposite
            print(f"  {code} not in batch, trying individual query...")
            for mk in (market, "otc" if market == "sii" else "sii"):
                rec = fetch_single(code, mk)
                if rec:
                    print(f"    ✓ found via individual ({mk})")
                    rec["code"] = code
                    break
            if rec is None:
                print(f"  [WARN] No data for {code} {company['name_zh']}")
                results.append({
                    "code": code, "name_zh": company["name_zh"],
                    "name_en": company["name_en"], "market": market,
                    "error": "no_data",
                })
                continue

        rm   = rec["revenue_month"]
        rpm  = rec["revenue_prev_month"]
        rpy  = rec["revenue_prev_year_month"]
        ryt  = rec["revenue_ytd"]
        rpyt = rec["revenue_prev_year_ytd"]

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
        print(
            f"  {code} {company['name_zh']}: "
            f"月營收={rm} 仟元  YoY={entry['revenue_month_yoy']}%"
        )

    now_tw = datetime.now(TW_TZ)
    output = {
        "updated_at":   now_tw.isoformat(),
        "report_month": infer_report_month(report_date_str),
        "companies":    results,
    }

    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n✓ Saved → {OUTPUT_FILE}  (report_month: {output['report_month']})")


if __name__ == "__main__":
    main()
