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
    "cloudflare",
    None,
]


def _proxy_url(target: str, proxy: str | None) -> str:
    if proxy is None:
        return target
    enc = urllib.parse.quote(target, safe="")
    if proxy == "cloudflare":
        return f"https://silent-bonus-fc0a.s1495h008.workers.dev/?url={enc}"
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
    "month":    ["營業收入_當月營收",    "當月營收",    "Revenue"],
    "prev_m":   ["營業收入_上月營收",    "上月營收",    "PreviousRevenue"],
    "prev_y":   ["營業收入_去年當月營收","去年當月營收","LastYearRevenue"],
    "ytd":      [
        "累計營業收入_當月累計營收",
        "累計營業收入_當月累計",
        "累計營業收入_本年累計",
        "當月累計營收", "當月累積營收", "AccumulatedRevenue",
    ],
    "prev_ytd": [
        "累計營業收入_去年累計營收",
        "累計營業收入_去年累計",
        "累計營業收入_去年同期",
        "去年累計營收", "去年累積營收", "LastYearAccumulatedRevenue",
    ],
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
    print(f"  [debug] {label} ALL keys: {list(sample.keys())}")
    ytd_hit = next((k for k in FIELD_VARIANTS["ytd"] if k in sample), None)
    prevytd_hit = next((k for k in FIELD_VARIANTS["prev_ytd"] if k in sample), None)
    print(f"  [debug] ytd field matched: {ytd_hit!r}  prev_ytd matched: {prevytd_hit!r}")

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
