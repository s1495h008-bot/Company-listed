#!/usr/bin/env python3
"""
Fetch monthly revenue data for listed/OTC companies from TWSE/TPEx OpenAPI
and MOPS (公開資訊觀測站). Saves results to data/revenue.json.

Units: all revenue values stored as-is in 仟元 (thousands NTD),
matching the official source data.
"""

import json
import os
import sys
import time
import requests
from datetime import datetime, timezone, timedelta

TW_TZ = timezone(timedelta(hours=8))
COMPANIES_FILE = os.path.join(os.path.dirname(__file__), '..', 'companies.json')
OUTPUT_FILE = os.path.join(os.path.dirname(__file__), '..', 'data', 'revenue.json')

TWSE_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_URL = "https://www.tpex.org.tw/openapi/v1/tpex_monthly_revenue"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; revenue-fetcher/1.0)",
    "Accept": "application/json",
}


def load_companies():
    with open(COMPANIES_FILE, encoding="utf-8") as f:
        return json.load(f)


def fetch_json(url, retries=3, backoff=4):
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            if attempt == retries - 1:
                raise
            print(f"  Retry {attempt+1} for {url}: {exc}", file=sys.stderr)
            time.sleep(backoff * (attempt + 1))


def safe_num(val):
    """Parse a possibly comma-separated numeric string to float, or None."""
    if val is None:
        return None
    try:
        return float(str(val).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def pct_change(current, base):
    if current is None or base is None or base == 0:
        return None
    return round((current - base) / abs(base) * 100, 2)


def parse_twse_item(item):
    """Extract fields from a TWSE t187ap03_L row."""
    # Field names may include full-width spaces; strip all values
    def g(key):
        return item.get(key, "").strip() if isinstance(item.get(key), str) else item.get(key)

    return {
        "report_date": g("出表日期") or g("資料日期") or "",
        "revenue_month":           safe_num(g("當月營收")),
        "revenue_prev_month":      safe_num(g("上月營收")),
        "revenue_prev_year_month": safe_num(g("去年當月營收")),
        "revenue_ytd":             safe_num(g("當月累計營收")),
        "revenue_prev_year_ytd":   safe_num(g("去年累計營收")),
    }


def parse_tpex_item(item):
    """Extract fields from a TPEx tpex_monthly_revenue row."""
    def g(key):
        return item.get(key, "").strip() if isinstance(item.get(key), str) else item.get(key)

    # TPEx uses the same field names as TWSE for this endpoint
    return {
        "report_date": g("出表日期") or g("資料日期") or "",
        "revenue_month":           safe_num(g("當月營收")),
        "revenue_prev_month":      safe_num(g("上月營收")),
        "revenue_prev_year_month": safe_num(g("去年當月營收")),
        "revenue_ytd":             safe_num(g("當月累計營收")),
        "revenue_prev_year_ytd":   safe_num(g("去年累計營收")),
    }


def infer_report_month(report_date_str: str) -> str:
    """
    The revenue report published on the 12th covers last month's revenue.
    Try to parse the date string first; fall back to (now - 1 month).
    Returns 'YYYY-MM' of the revenue month.
    """
    now_tw = datetime.now(TW_TZ)
    if report_date_str:
        # Could be ROC format: 1140512 → 2025-05-12
        # or Gregorian: 20250512 / 2025/05/12 / 2025-05-12
        ds = report_date_str.replace("/", "").replace("-", "").strip()
        if len(ds) == 7:
            # ROC format: YYYMMDD
            roc_year = int(ds[:3])
            month = int(ds[3:5])
            greg_year = roc_year + 1911
            # Revenue month is one month before the report date
            if month == 1:
                return f"{greg_year - 1}-12"
            return f"{greg_year}-{month - 1:02d}"
        elif len(ds) == 8:
            greg_year = int(ds[:4])
            month = int(ds[4:6])
            if month == 1:
                return f"{greg_year - 1}-12"
            return f"{greg_year}-{month - 1:02d}"
    # Fall back: assume report day >= 10, so revenue is previous calendar month
    if now_tw.month == 1:
        return f"{now_tw.year - 1}-12"
    return f"{now_tw.year}-{now_tw.month - 1:02d}"


def main():
    companies = load_companies()
    print(f"Loaded {len(companies)} companies")

    # Fetch TWSE (上市) data
    twse_map = {}
    print("Fetching TWSE (上市) monthly revenue...")
    try:
        twse_raw = fetch_json(TWSE_URL)
        for item in twse_raw:
            code = str(item.get("公司代號", "")).strip()
            if code:
                twse_map[code] = item
        print(f"  Got {len(twse_map)} TWSE records")
    except Exception as exc:
        print(f"  TWSE fetch FAILED: {exc}", file=sys.stderr)

    # Fetch TPEx (上櫃) data
    tpex_map = {}
    print("Fetching TPEx (上櫃) monthly revenue...")
    try:
        tpex_raw = fetch_json(TPEX_URL)
        for item in tpex_raw:
            code = str(item.get("公司代號", "")).strip()
            if code:
                tpex_map[code] = item
        print(f"  Got {len(tpex_map)} TPEx records")
    except Exception as exc:
        print(f"  TPEx fetch FAILED: {exc}", file=sys.stderr)

    results = []
    first_report_date = ""

    for company in companies:
        code = company["code"]
        market = company["market"]
        raw = twse_map.get(code) if market == "sii" else tpex_map.get(code)

        if not raw:
            print(f"  [WARN] No data for {code} {company['name_zh']}")
            results.append({
                "code": code,
                "name_zh": company["name_zh"],
                "name_en": company["name_en"],
                "market": market,
                "error": "no_data",
            })
            continue

        parsed = parse_twse_item(raw) if market == "sii" else parse_tpex_item(raw)
        if not first_report_date and parsed["report_date"]:
            first_report_date = parsed["report_date"]

        r_m  = parsed["revenue_month"]
        r_pm = parsed["revenue_prev_month"]
        r_py = parsed["revenue_prev_year_month"]
        r_ytd    = parsed["revenue_ytd"]
        r_py_ytd = parsed["revenue_prev_year_ytd"]

        entry = {
            "code":     code,
            "name_zh":  company["name_zh"],
            "name_en":  company["name_en"],
            "market":   market,
            "revenue_month":           r_m,
            "revenue_prev_month":      r_pm,
            "revenue_prev_year_month": r_py,
            "revenue_ytd":             r_ytd,
            "revenue_prev_year_ytd":   r_py_ytd,
            "revenue_month_yoy":       pct_change(r_m, r_py),
            "revenue_month_mom":       pct_change(r_m, r_pm),
            "revenue_ytd_yoy":         pct_change(r_ytd, r_py_ytd),
        }
        results.append(entry)
        print(f"  {code} {company['name_zh']}: 月營收={r_m} 仟元, YoY={entry['revenue_month_yoy']}%")

    report_month = infer_report_month(first_report_date)
    now_tw = datetime.now(TW_TZ)

    output = {
        "updated_at":    now_tw.isoformat(),
        "report_month":  report_month,
        "companies":     results,
    }

    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\nSaved → {OUTPUT_FILE}  (report_month: {report_month})")


if __name__ == "__main__":
    main()
