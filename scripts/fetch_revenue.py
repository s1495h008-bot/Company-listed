#!/usr/bin/env python3
"""
Fetch monthly revenue from TWSE/TPEx OpenAPIs via CORS proxy.

Direct access to openapi.twse.com.tw / tpex.org.tw is blocked for
GitHub Actions IPs by WAF. Route requests through public CORS proxies
whose IPs are not on the blocklist.

Sources:
  上市 (sii): https://openapi.twse.com.tw/v1/opendata/t187ap05_L
  上櫃 (otc): https://www.tpex.org.tw/openapi/v1/t187ap05_O  (fallback: tpex_monthly_revenue)

Proxy chain (tried in order until one succeeds):
  1. corsproxy.io   — ?url=<encoded>
  2. allorigins.win — /raw?url=<encoded>
  3. Direct (no proxy) — works if run locally or from non-blocked IP

All revenue values are in 仟元 (thousands NTD).
Validation: 9921 巨大 當月營收 ≈ 5,357,327 仟元 (2026/05)
"""

import json
import os
import sys
import time
import urllib.parse
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

TWSE_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"
TPEX_URLS = [
    "https://www.tpex.org.tw/openapi/v1/t187ap05_O",        # 上櫃
    "https://www.tpex.org.tw/openapi/v1/t187ap05_R",        # 興櫃 (rotc)
    "https://www.tpex.org.tw/openapi/v1/tpex_monthly_revenue",  # fallback
]
# sii companies sometimes listed on TWSE emerging board — try this too
TWSE_URLS_EXTRA = [
    "https://openapi.twse.com.tw/v1/opendata/t187ap05_R",   # 上市興櫃
]
# Companies known to be missing; used to print targeted debug
WATCH_CODES = {"8933", "6804", "4559", "8938", "5291"}

# CORS proxies tried in order; None = direct (no proxy)
PROXIES = [
    "corsproxy",
    "allorigins",
    None,
]


def _proxy_url(target: str, proxy: str | None) -> str:
    if proxy is None:
        return target
    enc = urllib.parse.quote(target, safe="")
    if proxy == "corsproxy":
        return f"https://corsproxy.io/?url={enc}"
    if proxy == "allorigins":
        return f"https://api.allorigins.win/raw?url={enc}"
    return target


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

def _get_json(target_url: str, retries: int = 2) -> list[dict]:
    """Try each proxy in turn until we get a non-empty JSON list."""
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, */*",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    }
    last_exc = None
    for proxy in PROXIES:
        url = _proxy_url(target_url, proxy)
        proxy_label = proxy or "direct"
        for attempt in range(retries):
            try:
                r = requests.get(url, headers=headers, timeout=30)
                r.raise_for_status()
                data = r.json()
                records = data if isinstance(data, list) else data.get("data") or data.get("contents") or []
                # allorigins wraps in {"contents": "<json string>"}
                if isinstance(records, str):
                    records = json.loads(records)
                if not records:
                    raise ValueError("Empty list")
                print(f"  ✓ [{proxy_label}] {len(records)} records")
                return records
            except Exception as exc:
                last_exc = exc
                if attempt < retries - 1:
                    time.sleep(2)
        print(f"  ✗ [{proxy_label}] {last_exc}", file=sys.stderr)

    raise RuntimeError(f"All proxies failed for {target_url}. Last: {last_exc}")


def _normalize_keys(record: dict) -> dict:
    return {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in record.items()}


# ─── field detection ──────────────────────────────────────────────────────────

FIELD_VARIANTS = {
    # Underscore variants = official t187ap05_L field names per TWSE OpenAPI docs
    # Non-prefixed variants = fallback / TPEx field names
    "month":    ["營業收入_當月營收",    "當月營收",    "Revenue"],
    "prev_m":   ["營業收入_上月營收",    "上月營收",    "PreviousRevenue"],
    "prev_y":   ["營業收入_去年當月營收","去年當月營收","LastYearRevenue"],
    "ytd":      ["累計營業收入_當月累計營收","當月累計營收","當月累積營收","AccumulatedRevenue"],
    "prev_ytd": ["累計營業收入_去年累計營收","去年累計營收","去年累積營收","LastYearAccumulatedRevenue"],
}


def _pick(rec: dict, variants: list[str]):
    for k in variants:
        if k in rec:
            return rec[k]
    return None


def _parse_records(records: list[dict], label: str) -> dict[str, dict]:
    if not records:
        return {}
    sample = _normalize_keys(records[0])
    print(f"  [debug] {label} keys: {list(sample.keys())[:8]}")

    result: dict[str, dict] = {}
    for raw in records:
        rec  = _normalize_keys(raw)
        code = (rec.get("公司代號") or rec.get("CompanyID") or "").strip()
        if not code:
            continue
        rm   = safe_num(_pick(rec, FIELD_VARIANTS["month"]))
        rpm  = safe_num(_pick(rec, FIELD_VARIANTS["prev_m"]))
        rpy  = safe_num(_pick(rec, FIELD_VARIANTS["prev_y"]))
        ryt  = safe_num(_pick(rec, FIELD_VARIANTS["ytd"]))
        rpyt = safe_num(_pick(rec, FIELD_VARIANTS["prev_ytd"]))
        result[code] = {
            "revenue_month":           rm,
            "revenue_prev_month":      rpm,
            "revenue_prev_year_month": rpy,
            "revenue_ytd":             ryt,
            "revenue_prev_year_ytd":   rpyt,
        }
        if code == "9921":
            print(f"  [debug] 9921 raw={dict(list(rec.items())[:8])}")
            print(f"  [debug] 9921 revenue_month={rm}  (expect ~5357327)")
        if code in WATCH_CODES:
            print(f"  [debug] FOUND watch code {code}: revenue_month={rm}")
    return result


# ─── main fetchers ────────────────────────────────────────────────────────────

def fetch_twse() -> dict[str, dict]:
    result = {}
    for url in [TWSE_URL] + TWSE_URLS_EXTRA:
        print(f"Fetching via proxy → {url}")
        try:
            records = _get_json(url)
            label = url.split("/")[-1]
            parsed = _parse_records(records, label)
            result.update(parsed)
            found = [c for c in WATCH_CODES if c in parsed]
            if found:
                print(f"  [debug] watch codes found in {label}: {found}")
        except Exception as exc:
            print(f"  ✗ {url}: {exc}", file=sys.stderr)
    missing = [c for c in WATCH_CODES if c not in result]
    if missing:
        print(f"  [debug] watch codes NOT in TWSE data: {missing}")
    return result


def fetch_tpex() -> dict[str, dict]:
    result = {}
    last_exc = None
    for url in TPEX_URLS:
        print(f"Fetching via proxy → {url}")
        try:
            records = _get_json(url)
            if records:
                label = url.split("/")[-1]
                parsed = _parse_records(records, label)
                result.update(parsed)
                found = [c for c in WATCH_CODES if c in parsed]
                if found:
                    print(f"  [debug] watch codes found in {label}: {found}")
        except Exception as exc:
            print(f"  ✗ {url}: {exc}", file=sys.stderr)
            last_exc = exc
    missing = [c for c in WATCH_CODES if c not in result]
    if missing:
        print(f"  [debug] watch codes NOT in any TPEx endpoint: {missing}")
    if not result:
        print(f"  ✗ all TPEx endpoints failed: {last_exc}", file=sys.stderr)
    return result


# ─── main ────────────────────────────────────────────────────────────────────

def main():
    companies = load_companies()
    print(f"Loaded {len(companies)} companies\n")

    all_data: dict[str, dict] = {}
    try:
        all_data.update(fetch_twse())
    except Exception as exc:
        print(f"  ✗ TWSE failed: {exc}", file=sys.stderr)
    print()
    try:
        all_data.update(fetch_tpex())
    except Exception as exc:
        print(f"  ✗ TPEx failed: {exc}", file=sys.stderr)
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

# t187ap05_L = 上市公司月營收 (monthly revenue)
# t187ap05_O = 上櫃公司月營收
TWSE_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"
TPEX_URL = "https://www.tpex.org.tw/openapi/v1/t187ap05_O"


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
# Field names for t187ap05_L (月營收):
#   公司代號, 公司名稱,
#   當月營收, 上月營收, 去年當月營收,
#   上月比較增減(%), 去年同月增減(%),
#   當月累計營收, 去年累計營收, 前期比較增減(%)
#
# Validation target: 9921 當月營收 = 5,357,327 (仟元, 2026/05)

_TWSE_FIELD_MAPS = [
    # Primary: expected t187ap05_L field names
    {
        "month":    "當月營收",
        "prev_m":   "上月營收",
        "prev_y":   "去年當月營收",
        "ytd":      "當月累計營收",
        "prev_ytd": "去年累計營收",
    },
    # Fallback: in case API uses prefixed names
    {
        "month":    "營業收入-當月營收",
        "prev_m":   "營業收入-上月營收",
        "prev_y":   "營業收入-去年當月營收",
        "ytd":      "累計營業收入-當月累計",
        "prev_ytd": "累計營業收入-去年累計",
    },
]


def _detect_twse_fields(sample: dict) -> dict:
    for fm in _TWSE_FIELD_MAPS:
        if fm["month"] in sample:
            return fm
    # Unknown layout — return first map and let safe_num return None
    print(f"  [WARN] unknown TWSE field layout; keys={list(sample.keys())}", file=sys.stderr)
    return _TWSE_FIELD_MAPS[0]


def fetch_twse() -> dict[str, dict]:
    print("Fetching 上市 (sii) from TWSE OpenAPI ...")
    records = _get_json(TWSE_URL)
    print(f"  total records: {len(records)}")
    if not records:
        return {}

    sample = _normalize_keys(records[0])
    print(f"  [debug] keys: {list(sample.keys())}")
    fm = _detect_twse_fields(sample)
    print(f"  [debug] using field map: month='{fm['month']}'")

    result: dict[str, dict] = {}
    for raw in records:
        rec = _normalize_keys(raw)
        code = rec.get("公司代號", "").strip()
        if not code:
            continue
        entry = {
            "revenue_month":           safe_num(rec.get(fm["month"])),
            "revenue_prev_month":      safe_num(rec.get(fm["prev_m"])),
            "revenue_prev_year_month": safe_num(rec.get(fm["prev_y"])),
            "revenue_ytd":             safe_num(rec.get(fm["ytd"])),
            "revenue_prev_year_ytd":   safe_num(rec.get(fm["prev_ytd"])),
        }
        result[code] = entry
        # Validation spot-check
        if code == "9921":
            print(f"  [debug] 9921 raw={rec}")
            print(f"  [debug] 9921 parsed revenue_month={entry['revenue_month']} (expect ~5357327)")

    print(f"  ✓ {len(result)} companies parsed")
    return result


# ─── TPEx (上櫃, otc) ─────────────────────────────────────────────────────────
#
# t187ap05_O mirrors the TWSE naming convention for OTC companies.
# Expected fields (same as t187ap05_L):
#   公司代號, 公司名稱,
#   當月營收, 上月營收, 去年當月營收,
#   上月比較增減(%), 去年同月增減(%),
#   當月累計營收, 去年累計營收, 前期比較增減(%)
#
# Fallback: tpex_monthly_revenue endpoint with English field names

_TPEX_FIELD_MAPS = [
    # Same naming as t187ap05_L (most likely for t187ap05_O)
    {
        "code":     "公司代號",
        "month":    "當月營收",
        "prev_m":   "上月營收",
        "prev_y":   "去年當月營收",
        "ytd":      "當月累計營收",
        "prev_ytd": "去年累計營收",
    },
    # English field names (tpex_monthly_revenue endpoint)
    {
        "code":     "CompanyID",
        "month":    "Revenue",
        "prev_m":   "PreviousRevenue",
        "prev_y":   "LastYearRevenue",
        "ytd":      "AccumulatedRevenue",
        "prev_ytd": "LastYearAccumulatedRevenue",
    },
    # Alternate Chinese names
    {
        "code":     "公司代號",
        "month":    "當月營收",
        "prev_m":   "上月營收",
        "prev_y":   "去年當月營收",
        "ytd":      "當月累積營收",
        "prev_ytd": "去年累積營收",
    },
]

_TPEX_FALLBACK_URL = "https://www.tpex.org.tw/openapi/v1/tpex_monthly_revenue"


def _detect_tpex_fields(sample: dict) -> dict | None:
    for fm in _TPEX_FIELD_MAPS:
        if fm["code"] in sample and fm["month"] in sample:
            return fm
    return None


def fetch_tpex() -> dict[str, dict]:
    print("Fetching 上櫃 (otc) from TPEx OpenAPI ...")

    now   = datetime.now(TW_TZ)
    rev_m = now.month - 1 if now.month > 1 else 12
    rev_y = now.year      if now.month > 1 else now.year - 1
    roc_y = rev_y - 1911
    yearmonth = f"{roc_y}{rev_m:02d}"

    # Try primary endpoint then fallback with/without params
    attempts = [
        (TPEX_URL, None),
        (TPEX_URL, {"yearmonth": yearmonth}),
        (_TPEX_FALLBACK_URL, {"yearmonth": yearmonth}),
        (_TPEX_FALLBACK_URL, None),
    ]
    records = None
    for url, params in attempts:
        try:
            recs = _get_json(url, params=params)
            if recs:
                print(f"  url={url.split('/')[-1]}  params={params}  records={len(recs)}")
                records = recs
                break
            print(f"  url={url.split('/')[-1]}  params={params}  → empty")
        except Exception as exc:
            print(f"  url={url.split('/')[-1]}  params={params}  ERROR: {exc}", file=sys.stderr)

    if not records:
        print("  ✗ TPEx returned no data from any endpoint", file=sys.stderr)
        return {}

    sample = _normalize_keys(records[0])
    print(f"  [debug] keys: {list(sample.keys())}")
    fm = _detect_tpex_fields(sample)
    if fm is None:
        print(f"  ✗ unknown TPEx field layout; keys={list(sample.keys())}", file=sys.stderr)
        return {}

    print(f"  [debug] using field map: code='{fm['code']}' month='{fm['month']}'")
    result: dict[str, dict] = {}
    for raw in records:
        rec = _normalize_keys(raw)
        code = str(rec.get(fm["code"], "")).strip()
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
