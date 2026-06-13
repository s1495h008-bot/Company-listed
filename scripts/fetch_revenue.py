#!/usr/bin/env python3
"""
Fetch monthly revenue from official Taiwan exchange OpenAPIs.

Sources:
  上市 (sii): https://openapi.twse.com.tw/v1/opendata/t187ap03_L
  上櫃 (otc): https://www.tpex.org.tw/openapi/v1/tpex_monthly_revenue

These REST APIs return JSON and are designed for programmatic access
(no session/cookie/WAF requirements unlike the MOPS web interface).

All revenue values are in 仟元 (thousands NTD) as returned by the APIs.
"""

import json
import os
import sys
import time
import requests
from datetime import datetime, timezone, timedelta

TW_TZ         = timezone(timedelta(hours=8))
BASE           = os.path.dirname(__file__)
COMPANIES_FILE = os.path.join(BASE, "..", "companies.json")
OUTPUT_FILE    = os.path.join(BASE, "..", "data", "revenue.json")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

TWSE_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_URL = "https://www.tpex.org.tw/openapi/v1/tpex_monthly_revenue"


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


# ─── API fetch helpers ────────────────────────────────────────────────────────

def _get_json(url: str, params: dict | None = None, retries: int = 3) -> list[dict]:
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, */*",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    }
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=30)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list):
                return data
            # Some endpoints wrap in a dict
            for key in ("data", "Data", "result", "Result"):
                if key in data and isinstance(data[key], list):
                    return data[key]
            raise ValueError(f"Unexpected JSON structure: {list(data.keys()) if isinstance(data, dict) else type(data)}")
        except Exception as exc:
            if attempt == retries - 1:
                raise
            wait = 2 ** attempt
            print(f"  retry {attempt+1} in {wait}s: {exc}", file=sys.stderr)
            time.sleep(wait)


def _normalize_keys(record: dict) -> dict:
    """Strip whitespace from all keys and values."""
    return {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in record.items()}


# ─── TWSE (上市, sii) ─────────────────────────────────────────────────────────
#
# Field names returned by openapi.twse.com.tw/v1/opendata/t187ap03_L:
#   公司代號, 公司名稱,
#   營業收入-當月營收, 營業收入-上月營收, 營業收入-去年當月營收,
#   營業收入-上月比較增減(%), 營業收入-去年同月增減(%),
#   累計營業收入-當月累計, 累計營業收入-去年累計,
#   累計營業收入-前期比較增減(%)

def fetch_twse() -> dict[str, dict]:
    print("Fetching 上市 (sii) from TWSE OpenAPI ...")
    records = _get_json(TWSE_URL)
    print(f"  total records: {len(records)}")
    if records:
        sample = _normalize_keys(records[0])
        print(f"  [debug] sample keys: {list(sample.keys())}")
        print(f"  [debug] sample values: {list(sample.values())[:5]}")

    result: dict[str, dict] = {}
    for raw in records:
        rec = _normalize_keys(raw)
        code = rec.get("公司代號", "").strip()
        if not code:
            continue
        result[code] = {
            "revenue_month":           safe_num(rec.get("營業收入-當月營收")),
            "revenue_prev_month":      safe_num(rec.get("營業收入-上月營收")),
            "revenue_prev_year_month": safe_num(rec.get("營業收入-去年當月營收")),
            "revenue_ytd":             safe_num(rec.get("累計營業收入-當月累計")),
            "revenue_prev_year_ytd":   safe_num(rec.get("累計營業收入-去年累計")),
        }
    print(f"  ✓ {len(result)} companies parsed")
    return result


# ─── TPEx (上櫃, otc) ─────────────────────────────────────────────────────────
#
# Field names returned by tpex.org.tw/openapi/v1/tpex_monthly_revenue:
#   CompanyID, CompanyName (or 公司代號, 公司名稱 — depends on API version)
#   Revenue, PreviousRevenue, LastYearRevenue,
#   MoMChangePercent, YoYChangePercent,
#   AccumulatedRevenue, LastYearAccumulatedRevenue, AccumulatedChangePercent

_TPEX_FIELD_MAPS = [
    # English field names (newer API)
    {
        "code":     "CompanyID",
        "month":    "Revenue",
        "prev_m":   "PreviousRevenue",
        "prev_y":   "LastYearRevenue",
        "ytd":      "AccumulatedRevenue",
        "prev_ytd": "LastYearAccumulatedRevenue",
    },
    # Chinese field names (older API / fallback)
    {
        "code":     "公司代號",
        "month":    "當月營收",
        "prev_m":   "上月營收",
        "prev_y":   "去年當月營收",
        "ytd":      "當月累積營收",
        "prev_ytd": "去年累積營收",
    },
]


def _detect_tpex_fields(sample: dict) -> dict | None:
    for fm in _TPEX_FIELD_MAPS:
        if fm["code"] in sample:
            return fm
    return None


def fetch_tpex() -> dict[str, dict]:
    print("Fetching 上櫃 (otc) from TPEx OpenAPI ...")

    # Compute ROC year/month for the most recent report month
    now   = datetime.now(TW_TZ)
    rev_m = now.month - 1 if now.month > 1 else 12
    rev_y = now.year      if now.month > 1 else now.year - 1
    roc_y = rev_y - 1911
    yearmonth = f"{roc_y}{rev_m:02d}"

    records = None
    for params in [{"yearmonth": yearmonth}, None]:
        try:
            records = _get_json(TPEX_URL, params=params)
            if records:
                print(f"  params={params}  total records: {len(records)}")
                break
            print(f"  params={params}  → empty, trying next ...")
        except Exception as exc:
            print(f"  params={params}  ERROR: {exc}", file=sys.stderr)

    if not records:
        print("  ✗ TPEx returned no data", file=sys.stderr)
        return {}

    sample = _normalize_keys(records[0])
    print(f"  [debug] sample keys: {list(sample.keys())}")
    print(f"  [debug] sample values: {list(sample.values())[:5]}")

    fm = _detect_tpex_fields(sample)
    if fm is None:
        print(f"  ✗ unknown TPEx field layout; keys={list(sample.keys())}", file=sys.stderr)
        return {}

    result: dict[str, dict] = {}
    for raw in records:
        rec = _normalize_keys(raw)
        code = rec.get(fm["code"], "").strip()
        if not code:
            continue
        result[code] = {
            "revenue_month":           safe_num(rec.get(fm["month"])),
            "revenue_prev_month":      safe_num(rec.get(fm["prev_m"])),
            "revenue_prev_year_month": safe_num(rec.get(fm["prev_y"])),
            "revenue_ytd":             safe_num(rec.get(fm["ytd"])),
            "revenue_prev_year_ytd":   safe_num(rec.get(fm["prev_ytd"])),
        }
    print(f"  ✓ {len(result)} companies parsed")
    return result


# ─── main ────────────────────────────────────────────────────────────────────

def main():
    companies = load_companies()
    print(f"Loaded {len(companies)} companies\n")

    all_data: dict[str, dict] = {}

    try:
        all_data.update(fetch_twse())
    except Exception as exc:
        print(f"  ✗ TWSE fetch failed: {exc}", file=sys.stderr)

    print()

    try:
        all_data.update(fetch_tpex())
    except Exception as exc:
        print(f"  ✗ TPEx fetch failed: {exc}", file=sys.stderr)

    print()

    results = []
    for company in companies:
        code, market = company["code"], company["market"]
        rec = all_data.get(code)

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
