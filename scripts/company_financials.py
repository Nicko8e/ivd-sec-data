#!/usr/bin/env python3
"""Every reported annual financial figure, for every US-listed company, one readable file per company.

Rows are line items (revenue, net income, total assets, cash from operations, EPS, ... every item the
company tagged in its filings); columns are fiscal years. Source: SEC EDGAR XBRL "company facts",
which covers filings from about 2009 onward (first filings often include 2007-2008 comparatives).

Least effort:
  - First run: one bulk download (companyfacts.zip) instead of 7,000+ separate requests.
  - Later runs: read the SEC daily filing lists since the last run and rebuild only companies that
    filed a new 10-K, 10-Q, 20-F or 40-F (or amendment), plus any newly listed company.
  - A record of what was extracted is kept in <cache>/fin/state.json and published in the manifest.

Output: <out>/company/<TICKER>.csv and <out>/company/index.csv
"""
import csv, datetime as dt, gzip, io, json, os, re, shutil, sys, time, urllib.error, urllib.request, zipfile

OUT = sys.argv[1] if len(sys.argv) > 1 else "_site"
CACHE = os.environ.get("SEC_CACHE_DIR", ".sec-cache")
UA = os.environ.get("SEC_USER_AGENT", "").strip()
FIN = os.path.join(CACHE, "fin")
STORE = os.path.join(FIN, "company")                 # kept between runs
STATE_FILE = os.path.join(FIN, "state.json")
TODAY = dt.date.today()
FULL_REBUILD_AFTER_DAYS = 45                          # if the record is older than this, do a bulk refresh
RULES = 3                                             # bump when the file-building rules change (2: share-unit fix, 3: latest-quarter shares) -> one bulk rebuild

ANNUAL_FORMS = {"10-K", "10-K/A", "10-KT", "10-KT/A", "20-F", "20-F/A", "40-F", "40-F/A"}
FILING_FORMS = ANNUAL_FORMS | {"10-Q", "10-Q/A"}
KEY_TAGS = [  # shown first, in this order, when present
    "Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet", "CostOfRevenue", "GrossProfit",
    "ResearchAndDevelopmentExpense", "SellingGeneralAndAdministrativeExpense", "OperatingExpenses", "OperatingIncomeLoss",
    "InterestExpense", "IncomeTaxExpenseBenefit", "NetIncomeLoss", "EarningsPerShareBasic", "EarningsPerShareDiluted",
    "WeightedAverageNumberOfSharesOutstandingBasic", "WeightedAverageNumberOfDilutedSharesOutstanding",
    "CashAndCashEquivalentsAtCarryingValue", "ShortTermInvestments", "MarketableSecuritiesCurrent", "AssetsCurrent", "Assets",
    "LiabilitiesCurrent", "LongTermDebtNoncurrent", "LongTermDebt", "Liabilities", "StockholdersEquity",
    "NetCashProvidedByUsedInOperatingActivities", "PaymentsToAcquirePropertyPlantAndEquipment",
    "NetCashProvidedByUsedInInvestingActivities", "NetCashProvidedByUsedInFinancingActivities",
    "PaymentsForRepurchaseOfCommonStock", "PaymentsOfDividends", "ShareBasedCompensation", "DepreciationDepletionAndAmortization",
]
KEY_RANK = {t: i for i, t in enumerate(KEY_TAGS)}

_requests = 0
def fetch(url, binary=False):
    global _requests
    if "@" not in UA:
        sys.exit("Set the repository variable SEC_USER_AGENT (SEC requirement).")
    time.sleep(0.12)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip": raw = gzip.decompress(raw)
                _requests += 1
                return raw if binary else raw.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 404: _requests += 1; return None
            if e.code in (429, 500, 502, 503) and attempt < 2: time.sleep(5 * (attempt + 1)); continue
            raise


def fy_label(end):
    e = dt.date.fromisoformat(end)
    return e.year - 1 if (e.month == 1 and e.day <= 7) else e.year


def unit_label(u):
    if u == "USD": return "USD millions"
    if u == "shares": return "shares, millions"
    if u == "USD/shares": return "USD per share"
    if re.fullmatch(r"[A-Z]{3}", u): return u + " millions"
    return u


def scale(u, v):
    if u == "USD" or u == "shares" or re.fullmatch(r"[A-Z]{3}", u): return round(v / 1e6, 3)
    return v


def company_table(facts):
    """Returns (header, rows, first_year, last_year) of annual figures from annual reports."""
    cells = {}          # (tag, unit) -> {year: (filed, end, value)}
    labels = {}
    for tax in ("us-gaap", "ifrs-full"):
        for tag, obj in (facts.get(tax) or {}).items():
            for unit, arr in (obj.get("units") or {}).items():
                for f in arr:
                    if f.get("form") not in ANNUAL_FORMS or not f.get("end"): continue
                    if f.get("start"):
                        days = (dt.date.fromisoformat(f["end"]) - dt.date.fromisoformat(f["start"])).days
                        if days < 350 or days > 380: continue          # full-year amounts only
                    elif f.get("fp") != "FY": continue                  # year-end balances only
                    y = fy_label(f["end"])
                    slot = cells.setdefault((tag, unit), {})
                    key = (f["end"], f.get("filed", ""))
                    if y not in slot or key >= slot[y][:2]:
                        slot[y] = (f["end"], f.get("filed", ""), f["val"])
                labels[(tag, unit)] = obj.get("label") or tag
    if not cells: return None
    fixed = fix_share_units(cells)
    for k in fixed: labels[k] = (labels[k] or k[0]) + " (corrected for a unit error in the filing: net income ÷ EPS)"
    years = sorted({y for s in cells.values() for y in s})
    order = sorted(cells, key=lambda k: (KEY_RANK.get(k[0], 10_000), (labels[k] or "").lower(), k[1]))
    header = ["Line item", "Unit", "SEC tag"] + [f"FY{y}" for y in years]
    rows = []
    for k in order:
        s = cells[k]
        if len(s) < 2 and k[0] not in KEY_RANK: continue        # skip one-off items to keep files small
        rows.append([labels[k], unit_label(k[1]), k[0]] + [scale(k[1], s[y][2]) if y in s else "" for y in years])
    return header, rows, years[0], years[-1]


def fix_share_units(cells):
    """Some filers state share counts in the wrong unit (McDonald's files 716.6 instead of 716.6 million from FY2021).
    Net income ÷ earnings per share gives the real count; use it where the filed count is more than 5x off."""
    ni = cells.get(("NetIncomeLoss", "USD")) or cells.get(("ProfitLoss", "USD")) or {}
    fixed = []
    for sh_tag, eps_tag in (("WeightedAverageNumberOfDilutedSharesOutstanding", "EarningsPerShareDiluted"),
                            ("WeightedAverageNumberOfSharesOutstandingBasic", "EarningsPerShareBasic")):
        sh, eps = cells.get((sh_tag, "shares")), cells.get((eps_tag, "USD/shares")) or {}
        if sh is None: continue
        for y in set(ni) & set(eps):
            n, e = ni[y][2], eps[y][2]
            if not n or abs(e) < 0.01: continue
            implied = n / e
            if implied <= 0: continue
            v = sh[y][2] if y in sh else None
            if v is None or v <= 0 or v / implied > 5 or v / implied < 0.2:
                end, filed = (sh[y][0], sh[y][1]) if y in sh else (ni[y][0], ni[y][1])
                sh[y] = (end, filed, round(implied))
                if (sh_tag, "shares") not in fixed: fixed.append((sh_tag, "shares"))
    return fixed


def latest_quarter_shares(facts):
    """Diluted shares from the most recent quarterly result: the latest three-month weighted-average diluted share
    count in a 10-Q (or 10-K). When the newest report is an annual one with no three-month figure, the fourth quarter
    is worked out from the full year and the first three quarters. Returns (shares, quarter_end, note) or None."""
    g = facts.get("us-gaap") or {}
    def entries(tag, unit):
        out = {}
        for f in ((g.get(tag) or {}).get("units") or {}).get(unit, []):
            if f.get("form") not in FILING_FORMS or not f.get("start") or not f.get("end"): continue
            days = (dt.date.fromisoformat(f["end"]) - dt.date.fromisoformat(f["start"])).days
            key = (f["start"], f["end"])
            if key not in out or f.get("filed", "") > out[key][1]: out[key] = (f["val"], f.get("filed", ""), days)
        return out
    sh = entries("WeightedAverageNumberOfDilutedSharesOutstanding", "shares")
    if not sh: return None
    ni = entries("NetIncomeLoss", "USD"); eps = entries("EarningsPerShareDiluted", "USD/shares")
    def fixed(key, v):                                   # same unit-error correction as the annual figures
        if key in ni and key in eps and abs(eps[key][0]) >= 0.01 and ni[key][0]:
            implied = ni[key][0] / eps[key][0]
            if implied > 0 and (v <= 0 or v / implied > 5 or v / implied < 0.2): return implied
        return v
    q = {k: v for k, v in sh.items() if 80 <= v[2] <= 100}
    y = {k: v for k, v in sh.items() if 350 <= v[2] <= 380}
    best_q = max(q, key=lambda k: k[1]) if q else None
    best_y = max(y, key=lambda k: k[1]) if y else None
    if best_y and (not best_q or best_y[1] > best_q[1]):
        # newest report is annual: Q4 = 4 x full year - Q1 - Q2 - Q3 (all from the same year), when available
        ys, ye = best_y
        qs = sorted(k for k in q if k[0] >= ys and k[1] < ye)
        full = fixed(best_y, y[best_y][0])
        if len(qs) == 3:
            parts = [fixed(k, q[k][0]) for k in qs]
            q4 = 4 * full - sum(parts)
            if 0.8 < q4 / parts[-1] < 1.25:
                return q4, ye, "fourth quarter worked out from the annual report"
        return full, ye, "full-year average from the annual report (no quarterly figure)"
    if not best_q: return None
    return fixed(best_q, q[best_q][0]), best_q[1], "quarterly report"


def write_company(cik, facts):
    """One file per company, named by SEC CIK (all of a company's tickers share it)."""
    t = company_table(facts)
    if not t: return None
    header, rows, y0, y1 = t
    lq = latest_quarter_shares(facts)
    if lq:                                                # shown first; the value sits in the newest year column
        val, end, how = lq
        rows.insert(0, [f"Diluted shares, latest quarter (quarter ended {end}; {how})", "shares, millions", "LatestQuarterDilutedShares"]
                    + [""] * (len(header) - 4) + [round(val / 1e6, 3)])
    os.makedirs(STORE, exist_ok=True)
    with open(os.path.join(STORE, f"{cik}.csv"), "w", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n"); w.writerow(header); w.writerows(rows)
    return {"first": y0, "last": y1, "items": len(rows)}


def listed_companies():
    raw = fetch("https://www.sec.gov/files/company_tickers_exchange.json")
    tj = json.loads(raw)
    by_cik = {}
    for row in tj["data"]:
        r = dict(zip(tj["fields"], row)); ex = (r.get("exchange") or "").strip()
        if not ex or ex.upper() == "OTC": continue
        t = re.sub(r"[^A-Z0-9._-]", "", str(r["ticker"]).upper())
        by_cik.setdefault(int(r["cik"]), []).append((t, r["name"], ex))
    return by_cik


def ciks_filed_since(since):
    """CIKs that filed a 10-K/10-Q/20-F/40-F (or amendment) on each business day after `since`."""
    ciks, day = set(), since + dt.timedelta(days=1)
    while day <= TODAY:
        if day.weekday() < 5:
            q = (day.month - 1) // 3 + 1
            txt = fetch(f"https://www.sec.gov/Archives/edgar/daily-index/{day.year}/QTR{q}/form.{day:%Y%m%d}.idx")
            for line in (txt or "").splitlines():
                m = re.match(r"^(\S+(?:/A)?)\s{2,}.*?\s{2,}(\d{1,10})\s{2,}\d{4}-?\d{2}-?\d{2}", line)
                if m and m.group(1) in FILING_FORMS: ciks.add(int(m.group(2)))
        day += dt.timedelta(days=1)
    return ciks


def main():
    os.makedirs(FIN, exist_ok=True)
    try: state = json.load(open(STATE_FILE))
    except Exception: state = {}
    companies = listed_companies()
    built = state.get("built", {})               # ticker -> {cik, first, last, items, updated}
    last = dt.date.fromisoformat(state["last_checked"]) if state.get("last_checked") else None
    bulk = last is None or (TODAY - last).days > FULL_REBUILD_AFTER_DAYS or not os.path.isdir(STORE) or state.get("rules") != RULES
    done_now = []

    if bulk:
        zpath = os.environ.get("COMPANYFACTS_ZIP") or os.path.join(FIN, "companyfacts.zip")
        if not os.environ.get("COMPANYFACTS_ZIP"):
            print("Downloading the SEC bulk company-facts file (one request)…", flush=True)
            req = urllib.request.Request("https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip", headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=1800) as r, open(zpath, "wb") as fh: shutil.copyfileobj(r, fh, 1 << 20)
            global _requests; _requests += 1
        with zipfile.ZipFile(zpath) as z:
            for name in z.namelist():
                m = re.search(r"CIK(\d{10})\.json$", name)
                if not m or int(m.group(1)) not in companies: continue
                cik = int(m.group(1))
                info = write_company(cik, json.loads(z.read(name)).get("facts", {}))
                if info: built[str(cik)] = {**info, "updated": TODAY.isoformat()}; done_now.append(cik)
        if not os.environ.get("COMPANYFACTS_ZIP"):
            try: os.remove(zpath)
            except OSError: pass
        method = "bulk"
    else:
        changed = ciks_filed_since(last)
        new = {cik for cik in companies if str(cik) not in built and cik not in state.get("no_data", [])}
        todo = sorted((changed | new) & set(companies))
        no_data = set(state.get("no_data", []))
        for cik in todo:
            raw = fetch(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json")
            if not raw: no_data.add(cik); continue
            info = write_company(cik, json.loads(raw).get("facts", {}))
            if info: built[str(cik)] = {**info, "updated": TODAY.isoformat()}; done_now.append(cik)
        state["no_data"] = sorted(no_data)
        method = f"incremental ({len(todo)} companies with new filings or newly listed)"

    # forget companies no longer listed
    for c in [c for c in built if int(c) not in companies]:
        built.pop(c, None)
        try: os.remove(os.path.join(STORE, c + ".csv"))
        except OSError: pass

    state.update({"built": built, "last_checked": TODAY.isoformat(), "last_method": method, "rules": RULES})
    json.dump(state, open(STATE_FILE, "w"))

    # publish
    dest = os.path.join(OUT, "company")
    if os.path.isdir(dest): shutil.rmtree(dest)
    shutil.copytree(STORE, dest)
    with open(os.path.join(dest, "index.csv"), "w", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["Ticker", "Company", "SEC CIK", "First year", "Last year", "Line items", "Updated", "File"])
        for cik, ts in sorted(companies.items(), key=lambda kv: kv[1][0][0]):
            b = built.get(str(cik))
            if not b: continue
            for t, n, _ in ts: w.writerow([t, n, cik, f"FY{b['first']}", f"FY{b['last']}", b["items"], b["updated"], f"company/{cik}.csv"])
    summary = {"updated": TODAY.isoformat(), "method": method, "companies_with_files": len(built),
               "rebuilt_this_run": len(done_now), "sec_requests_this_run": _requests}
    json.dump(summary, open(os.path.join(dest, "status.json"), "w"), indent=1)
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
