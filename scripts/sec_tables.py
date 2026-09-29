#!/usr/bin/env python3
"""Build readable SEC EDGAR tables for every US-listed company, fetching only what is new.

Uses SEC "frames" (one request = one figure for every company for one period), so the full
history is about 175 requests and a weekly refresh about 40.

Output (for the Google Sheet to import):
  <out>/sheet/companies.csv, revenue.csv, net_income.csv, diluted_shares.csv, balance_sheet.csv
  <out>/sheet/manifest.json   update time, a fingerprint per file (the sheet imports only changed files),
                              and the extraction log (what was fetched and when)
  <out>/sheet/check.json      coverage counts and sample rows, for quick verification
Cache (kept between runs by GitHub Actions): <cache>/frames/*.json and <cache>/log.json
"""
import csv, datetime as dt, gzip, hashlib, io, json, os, sys, time, urllib.error, urllib.request

OUT = sys.argv[1] if len(sys.argv) > 1 else "_site"
CACHE = os.environ.get("SEC_CACHE_DIR", ".sec-cache")
UA = os.environ.get("SEC_USER_AGENT", "").strip()
FIRST_YEAR = 2009
RECENT_YEARS_TO_REFRESH = 2
REFRESH_DAYS = 6
NOW = dt.datetime.now(dt.timezone.utc)

REVENUE = ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet",
           "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueGoodsNet", "RevenuesNetOfInterestExpense"]
INCOME = ["NetIncomeLoss", "ProfitLoss"]
SHARES = ["WeightedAverageNumberOfDilutedSharesOutstanding"]
BALANCE = [("Cash", ["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"]),
           ("Short-term investments", ["ShortTermInvestments", "MarketableSecuritiesCurrent", "AvailableForSaleSecuritiesDebtSecuritiesCurrent"]),
           ("Long-term debt", ["LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations"]),
           ("Current debt", ["DebtCurrent", "LongTermDebtCurrent"])]

SPLIT_FACTORS = [2, 3, 4, 5, 6, 7, 8, 10, 15, 20, 25, 30, 40, 50, 100]
CHECK_TICKERS = ["NVDA", "AAPL", "MSFT", "AMZN", "JPM", "TSLA", "KO"]

def split_adjust(vals):
    """vals: share counts oldest→newest (None allowed). Scale older years when a jump looks like a stock split."""
    v = list(vals)
    idx = [i for i, x in enumerate(v) if x]
    for a, b in reversed(list(zip(idx, idx[1:]))):
        ratio = v[b] / v[a]
        for k in SPLIT_FACTORS:
            f = k if ratio > 1 else 1 / k
            if (ratio >= 1.8 or ratio <= 0.56) and abs(ratio / f - 1) < 0.12:
                for j in range(a + 1):
                    if v[j]: v[j] *= f
                break
    return v

_fetch_count = 0
def get(url):
    """Fetch JSON from the SEC; None when not published (404)."""
    global _fetch_count
    if "@" not in UA:
        sys.exit("Set the repository variable SEC_USER_AGENT, e.g. 'Intrinsic Value Desk you@example.com' (SEC requirement).")
    time.sleep(0.12)                                   # SEC allows up to 10 requests a second
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                _fetch_count += 1
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                _fetch_count += 1
                return None
            if e.code in (429, 500, 502, 503) and attempt < 2:
                time.sleep(5 * (attempt + 1)); continue
            raise


def load_log():
    try: return json.load(open(os.path.join(CACHE, "log.json")))
    except Exception: return {}
def save_log(log):
    os.makedirs(CACHE, exist_ok=True)
    json.dump(log, open(os.path.join(CACHE, "log.json"), "w"), indent=0, sort_keys=True)
def due(log, key, refresh_days):
    e = log.get(key)
    if not e: return True
    if refresh_days is None: return False
    return (NOW - dt.datetime.fromisoformat(e)).days >= refresh_days


def frame_path(tag, unit, period):
    return os.path.join(CACHE, "frames", f"{tag}_{unit}_{period}.json")
def fetch_frame(tag, unit, period):
    j = get(f"https://data.sec.gov/api/xbrl/frames/us-gaap/{tag}/{unit}/{period}.json")
    rows = [[d["cik"], d["val"], d.get("end", "")] for d in (j or {}).get("data", [])]
    os.makedirs(os.path.join(CACHE, "frames"), exist_ok=True)
    json.dump(rows, open(frame_path(tag, unit, period), "w"))
    return rows
def cached_frame(tag, unit, period):
    try: return json.load(open(frame_path(tag, unit, period)))
    except Exception: return []
def merged(tags, unit, period):
    out = {}
    for t in tags:
        for cik, val, end in cached_frame(t, unit, period):
            out.setdefault(cik, (val, end))
    return out


def main():
    log = load_log()
    fetched = []
    last_full = NOW.year - 1

    # 1) Company list: small, refreshed every run
    tj = get("https://www.sec.gov/files/company_tickers_exchange.json")
    fields = tj["fields"]
    companies = []
    for row in tj["data"]:
        r = dict(zip(fields, row)); ex = (r.get("exchange") or "").strip()
        if not ex or ex.upper() == "OTC": continue
        companies.append([str(r["ticker"]).upper(), r["name"], ex, int(r["cik"])])
    companies.sort(key=lambda c: c[0])
    log["tickers"] = NOW.isoformat(); fetched.append("tickers")

    # 2) Yearly figures: fetch only years never fetched, plus the latest years weekly
    years = list(range(FIRST_YEAR, last_full + 1))
    for y in sorted(years, reverse=True):
        recent = y > last_full - RECENT_YEARS_TO_REFRESH
        for name, tags, unit in (("revenue", REVENUE, "USD"), ("income", INCOME, "USD"), ("shares", SHARES, "shares")):
            key = f"{name}:CY{y}"
            if not due(log, key, REFRESH_DAYS if recent else None): continue
            for t in tags: fetch_frame(t, unit, f"CY{y}")
            log[key] = NOW.isoformat(); fetched.append(key)
            save_log(log)

    # 3) Latest balance sheet: the two newest quarters with broad coverage
    if due(log, "balance:latest", REFRESH_DAYS) or not log.get("balance:periods"):
        periods, y, q = [], NOW.year, (NOW.month - 1) // 3 + 1
        for _ in range(6):
            p = f"CY{y}Q{q}I"
            if len(fetch_frame(BALANCE[0][1][0], "USD", p)) > 1000: periods.append(p)
            if len(periods) == 2: break
            q -= 1
            if q == 0: q, y = 4, y - 1
        for p in periods:
            for _, tags in BALANCE:
                for t in tags:
                    if t != BALANCE[0][1][0]: fetch_frame(t, "USD", p)
        log["balance:latest"] = NOW.isoformat(); log["balance:periods"] = periods; fetched.append("balance:" + ",".join(periods))
    save_log(log)

    # 4) Build readable tables
    sheet = os.path.join(OUT, "sheet"); os.makedirs(sheet, exist_ok=True)
    files, tables = {}, {}
    def write(name, header, rows):
        buf = io.StringIO(); w = csv.writer(buf, lineterminator="\n"); w.writerow(header); w.writerows(rows)
        data = buf.getvalue().encode()
        open(os.path.join(sheet, name), "wb").write(data)
        files[name] = {"rows": len(rows), "sha": hashlib.sha256(data).hexdigest()[:16]}
    mil = lambda v: "" if v is None else round(v / 1e6, 3)

    write("companies.csv", ["Ticker", "Company", "Exchange", "SEC CIK", "SEC filings"],
          [c + [f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={c[3]:010d}"] for c in companies])
    for fname, tags, unit in (("revenue.csv", REVENUE, "USD"), ("net_income.csv", INCOME, "USD"), ("diluted_shares.csv", SHARES, "shares")):
        by_year = {y: merged(tags, unit, f"CY{y}") for y in years}
        rows = []
        for c in companies:
            vals = [by_year[y].get(c[3], (None,))[0] for y in years]
            if fname == "diluted_shares.csv": vals = split_adjust(vals)   # restate older years to today's share basis
            rows.append([c[0], c[1], c[3]] + [mil(v) for v in vals])
        write(fname, ["Ticker", "Company", "SEC CIK"] + [str(y) for y in years], rows)
        tables[fname] = rows
    cols, as_of = [dict() for _ in BALANCE], {}
    for p in reversed(log.get("balance:periods", [])):          # older first, newest overwrites
        for i, (_, tags) in enumerate(BALANCE):
            for cik, (val, end) in merged(tags, "USD", p).items():
                cols[i][cik] = val
                if i == 0 or end > as_of.get(cik, ""): as_of[cik] = end
    rows = []
    for c in companies:
        v = [m.get(c[3]) for m in cols]
        total = "" if v[2] is None and v[3] is None else mil((v[2] or 0) + (v[3] or 0))
        rows.append([c[0], c[1], c[3]] + [mil(x) for x in v] + [total, as_of.get(c[3], "")])
    write("balance_sheet.csv", ["Ticker", "Company", "SEC CIK"] + [b[0] for b in BALANCE] + ["Total debt", "As of"], rows)
    tables["balance_sheet.csv"] = rows

    # Small check file: how many companies have each figure, plus sample rows (easy to read and verify)
    check = {"updated": NOW.isoformat(timespec="seconds"), "coverage": {}, "samples": {}}
    for fname, rows_ in tables.items():
        width = len(rows_[0]) if rows_ else 0
        head = ["Ticker", "Company", "SEC CIK"] + ([str(y) for y in years] if fname != "balance_sheet.csv" else [b[0] for b in BALANCE] + ["Total debt", "As of"])
        check["coverage"][fname] = {head[j]: sum(1 for r in rows_ if r[j] != "") for j in range(3, width)}
        check["samples"][fname] = {r[0]: dict(zip(head[3:], r[3:])) for r in rows_ if r[0] in CHECK_TICKERS}
    json.dump(check, open(os.path.join(sheet, "check.json"), "w"), indent=1)

    json.dump({"updated": NOW.isoformat(timespec="seconds"), "companies": len(companies), "files": files,
               "fetched_this_run": fetched, "sec_requests_this_run": _fetch_count,
               "extraction_log": {k: v for k, v in sorted(log.items()) if k != "balance:periods"}},
              open(os.path.join(sheet, "manifest.json"), "w"), indent=1)
    print(f"{len(companies)} companies · {_fetch_count} SEC requests · updated: {', '.join(fetched[:8])}{' …' if len(fetched) > 8 else ''}")


if __name__ == "__main__":
    main()
