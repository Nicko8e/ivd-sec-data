#!/usr/bin/env python3
"""Every reported financial figure, annual and quarterly, for every US-listed company, organised by statement.

Two readable files per company:
  company/<CIK>.csv            every figure from the annual reports; columns are fiscal years (full history)
  company/quarterly/<CIK>.csv  every figure by quarter; columns are quarter-end dates (latest MAX_QUARTERS)
Rows are line items (every item the company tagged in its filings, in every taxonomy: US GAAP, IFRS,
cover-page "dei" and others), sorted into categories: Income statement, Balance sheet, Cash flow,
Per share, Shares, Employee share pay, Company information, Other disclosures.
Quarterly amounts are three-month figures. When a company reports only year-to-date amounts (common for
cash flow) or only the full year for the fourth quarter, the quarter is worked out as the difference. Source: SEC EDGAR XBRL "company facts",
which covers filings from about 2009 onward (first filings often include 2007-2008 comparatives).

Least effort:
  - First run: one bulk download (companyfacts.zip) instead of 7,000+ separate requests.
  - Later runs: read the SEC daily filing lists since the last run and rebuild only companies that
    filed a new 10-K, 10-Q, 20-F or 40-F (or amendment), plus any newly listed company.
  - A record of what was extracted is kept in <cache>/fin/state.json and published in the manifest.

Output: <out>/company/<CIK>.csv, <out>/company/quarterly/<CIK>.csv and <out>/company/index.csv
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
RULES = 5                                             # bump when the file-building rules change (2: share-unit fix, 3: latest-quarter shares, 4: all taxonomies, categories, quarterly files, 5: sales by business line) -> one bulk rebuild
MAX_QUARTERS = 32                                     # quarterly files keep the latest 8 years (annual files keep everything); keeps the published site well under GitHub Pages' 1 GB limit
STORE_Q = os.path.join(STORE, "quarterly")
CATEGORIES = ["Income statement", "Sales by business line", "Balance sheet", "Cash flow", "Per share", "Shares", "Employee share pay", "Company information", "Other disclosures"]
CAT_RANK = {c: i for i, c in enumerate(CATEGORIES)}

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


def category(tax, tag, unit, instant):
    """Which part of the financial statements a line item belongs to."""
    if tax == "dei": return "Company information"
    if "/shares" in unit or unit.endswith("/share"): return "Per share"
    if re.search(r"ShareBased|SharebasedCompensation|EmployeeStockOwnership|StockOption|RestrictedStock", tag): return "Employee share pay"
    if unit == "shares": return "Shares"
    if instant: return "Balance sheet"
    if re.search(r"^NetCashProvidedBy|^CashProvidedBy|^Payments|^Proceeds|^Repayments|^IncreaseDecrease|CashEquivalentsPeriodIncreaseDecrease|"
                 r"^CashCashEquivalents.*PeriodIncreaseDecrease|EffectOfExchangeRateOnCash|^DepreciationDepletionAndAmortization$|^OtherNoncashIncomeExpense|"
                 r"CashFlow|^Cash(Received|Paid)", tag): return "Cash flow"
    if re.search(r"Revenue|Sales|CostOf|Cost|Expense|Income|Profit|Loss|Earnings|Gain|Tax|Interest|Depreciation|Amortization|Impairment|"
                 r"ComprehensiveIncome|Margin|GrossProfit|OperatingIncome|NetIncome", tag): return "Income statement"
    return "Other disclosures"


def all_facts(facts):
    """(taxonomy, tag, unit, label, list of facts) for every item in every taxonomy."""
    for tax, items in facts.items():
        for tag, obj in (items or {}).items():
            for unit, arr in (obj.get("units") or {}).items():
                yield tax, tag, unit, obj.get("label") or tag, arr


def order_key(cat, tag, label, unit):
    return (CAT_RANK.get(cat, 99), KEY_RANK.get(tag, 10_000), (label or "").lower(), unit)


def company_table(facts):
    """Returns (header, rows, first_year, last_year) of annual figures from annual reports, every item, sorted by category."""
    cells = {}          # (tax, tag, unit) -> {year: (end, filed, value)}
    meta = {}
    for tax, tag, unit, label, arr in all_facts(facts):
        for f in arr:
            if f.get("form") not in ANNUAL_FORMS or not f.get("end"): continue
            if f.get("start"):
                days = (dt.date.fromisoformat(f["end"]) - dt.date.fromisoformat(f["start"])).days
                if days < 350 or days > 380: continue          # full-year amounts only
                instant = False
            elif f.get("fp") != "FY": continue                  # year-end balances only
            else: instant = True
            y = fy_label(f["end"])
            slot = cells.setdefault((tax, tag, unit), {})
            key = (f["end"], f.get("filed", ""))
            if y not in slot or key >= slot[y][:2]:
                slot[y] = (f["end"], f.get("filed", ""), f["val"])
            meta[(tax, tag, unit)] = (label, instant)
    if not cells: return None
    flat = {(tag, unit): {y: v for y, v in s_.items()} for (tax, tag, unit), s_ in cells.items() if tax != "dei"}
    fixed = set(fix_share_units(flat))                  # repairs share counts filed in the wrong unit
    for (tax, tag, unit) in cells:
        if (tag, unit) in flat and (tag, unit) in fixed: cells[(tax, tag, unit)] = flat[(tag, unit)]
    years = sorted({y for s_ in cells.values() for y in s_})
    header = ["Line item", "Unit", "SEC tag", "Category"] + [f"FY{y}" for y in years]
    rows = []
    for k in sorted(cells, key=lambda k: order_key(category(k[0], k[1], k[2], meta[k][1]), k[1], meta[k][0], k[2])):
        tax, tag, unit = k; label, instant = meta[k]; s_ = cells[k]
        if (tag, unit) in fixed: label += " (corrected for a unit error in the filing: net income ÷ EPS)"
        rows.append([label, unit_label(unit), tag, category(tax, tag, unit, instant)] + [scale(unit, s_[y][2]) if y in s_ else "" for y in years])
    return header, rows, years[0], years[-1]


def quarter_table(facts):
    """Every figure by quarter. Three-month amounts as reported; otherwise worked out from year-to-date or full-year
    amounts (amounts in money only: per-share figures and share counts are never subtracted). Balances at each quarter end."""
    import bisect
    D = {}
    def d(x):
        v = D.get(x)
        if v is None: v = D[x] = dt.date.fromisoformat(x)
        return v
    ends = set()                                         # the company's quarter ends: three-month period ends + fiscal year ends
    for tax, tag, unit, label, arr in all_facts(facts):
        for f in arr:
            if f.get("start") and f.get("end") and f.get("form") in FILING_FORMS:
                days = (d(f["end"]) - d(f["start"])).days
                if 80 <= days <= 100 or 350 <= days <= 380: ends.add(f["end"])
    if not ends: return None
    canon = []                                           # merge dates a few days apart (52/53-week years)
    for e in sorted(ends):
        if canon and (d(e) - d(canon[-1])).days <= 7: canon[-1] = e
        else: canon.append(e)
    canon = canon[-MAX_QUARTERS:]
    cdates = [d(c) for c in canon]; first = cdates[0] - dt.timedelta(days=400)
    NEAR = {}
    def near(e):
        if e in NEAR: return NEAR[e]
        x = d(e); i = bisect.bisect_left(cdates, x); best = None
        for j in (i - 1, i):
            if 0 <= j < len(cdates) and abs((cdates[j] - x).days) <= 7: best = canon[j]
        NEAR[e] = best; return best
    rows_out, meta = {}, {}
    for tax, tag, unit, label, arr in all_facts(facts):
        dur, inst = {}, {}
        for f in arr:
            e = f.get("end")
            if not e or f.get("form") not in FILING_FORMS or d(e) < first: continue
            if f.get("start"):
                k = (f["start"], e)
                if k not in dur or f.get("filed", "") >= dur[k][1]: dur[k] = (f["val"], f.get("filed", ""))
            else:
                c = near(e)
                if c and (c not in inst or f.get("filed", "") >= inst[c][1]): inst[c] = (f["val"], f.get("filed", ""))
        vals = {}
        if inst and not dur:
            vals = {c: v[0] for c, v in inst.items()}; instant = True
        elif dur:
            instant = False
            additive = unit not in ("shares", "pure") and "/" not in unit
            by_end, by_start = {}, {}
            for (st, en), (v, _) in dur.items():
                c = near(en)
                if c: by_end.setdefault(c, []).append((st, en, v))
                by_start.setdefault(st, []).append((en, v))
            for c in canon:
                cands = by_end.get(c, [])
                q = [x for x in cands if 80 <= (d(x[1]) - d(x[0])).days <= 100]
                if q: vals[c] = q[0][2]; continue
                if not additive: continue
                for st, en, v in sorted(cands, key=lambda x: x[0], reverse=True):       # e.g. 9 months to Sep minus 6 months to Jun
                    prev = [pv for pe, pv in by_start.get(st, []) if 80 <= (d(en) - d(pe)).days <= 100]
                    if prev: vals[c] = v - prev[0]; break
            if additive:                                  # fourth quarter = full year minus the three quarters before it
                for i, c in enumerate(canon):
                    if c in vals: continue
                    for st, en, v in by_end.get(c, []):
                        if not 350 <= (d(en) - d(st)).days <= 380: continue
                        inside = [x for x in canon[max(0, i - 4):i] if d(st) < d(x) < d(c) - dt.timedelta(days=7)]
                        if len(inside) == 3 and all(x in vals for x in inside):
                            vals[c] = v - sum(vals[x] for x in inside); break
        else: continue
        if len(vals) < 2: continue
        k = (tax, tag, unit); rows_out[k] = vals; meta[k] = (label, instant)
    if not rows_out: return None
    header = ["Line item", "Unit", "SEC tag", "Category"] + ["Quarter to " + c for c in canon]
    rows = []
    for k in sorted(rows_out, key=lambda k: order_key(category(k[0], k[1], k[2], meta[k][1]), k[1], meta[k][0], k[2])):
        tax, tag, unit = k; label, instant = meta[k]; v = rows_out[k]
        rows.append([label, unit_label(unit), tag, category(tax, tag, unit, instant)] + [scale(unit, v[c]) if c in v else "" for c in canon])
    return header, rows, canon[-1]


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


def segment_rows(cik, years):
    """Sales by business segment or by product, as read from the annual reports by segments.py (exact figures)."""
    try: seg = json.load(open(os.path.join(CACHE, "seg", f"{cik}.json")))
    except Exception: return []
    out = []
    for key in sorted(seg.get("items", {}), key=lambda k: (k.split(" [")[1], seg["items"][k]["label"].lower())):
        it = seg["items"][key]; v = it["vals"]
        if not any(y in v for y in years): continue
        out.append([it["label"], unit_label(it["unit"]), key, "Sales by business line"] + [scale(it["unit"], v[y]) if y in v else "" for y in years])
    return out


def write_company(cik, facts):
    """One file per company, named by SEC CIK (all of a company's tickers share it)."""
    t = company_table(facts)
    if not t: return None
    header, rows, y0, y1 = t
    lq = latest_quarter_shares(facts)
    if lq:                                                # shown first; the value sits in the newest year column
        val, end, how = lq
        rows.insert(0, [f"Diluted shares, latest quarter (quarter ended {end}; {how})", "shares, millions", "LatestQuarterDilutedShares", "Shares"]
                    + [""] * (len(header) - 5) + [round(val / 1e6, 3)])
    years = [h[2:] for h in header[4:]]
    seg = segment_rows(cik, years)
    if seg:                                               # placed right after the income statement
        at = next((i for i, r in enumerate(rows) if r[3] not in ("Income statement",) and r[2] != "LatestQuarterDilutedShares"), len(rows))
        rows[at:at] = seg
    os.makedirs(STORE_Q, exist_ok=True)
    with open(os.path.join(STORE, f"{cik}.csv"), "w", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n"); w.writerow(header); w.writerows(rows)
    info = {"first": y0, "last": y1, "items": len(rows), "q_items": 0, "last_q": ""}
    q = quarter_table(facts)
    qpath = os.path.join(STORE_Q, f"{cik}.csv")
    if q:
        qh, qr, lastq = q
        with open(qpath, "w", newline="") as fh:
            w = csv.writer(fh, lineterminator="\n"); w.writerow(qh); w.writerows(qr)
        info.update(q_items=len(qr), last_q=lastq)
    elif os.path.exists(qpath): os.remove(qpath)
    return info


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
        try: changed |= set(json.load(open(os.path.join(CACHE, "seg", "state.json"))).get("updated", []))   # new sales breakdowns
        except Exception: pass
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
        for path in (os.path.join(STORE, c + ".csv"), os.path.join(STORE_Q, c + ".csv")):
            try: os.remove(path)
            except OSError: pass

    state.update({"built": built, "last_checked": TODAY.isoformat(), "last_method": method, "rules": RULES})
    json.dump(state, open(STATE_FILE, "w"))

    # publish
    dest = os.path.join(OUT, "company")
    if os.path.isdir(dest): shutil.rmtree(dest)
    shutil.copytree(STORE, dest)
    with open(os.path.join(dest, "index.csv"), "w", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["Ticker", "Company", "SEC CIK", "First year", "Last year", "Annual line items", "Latest quarter", "Quarterly line items", "Updated", "Annual file", "Quarterly file"])
        for cik, ts in sorted(companies.items(), key=lambda kv: kv[1][0][0]):
            b = built.get(str(cik))
            if not b: continue
            qf = f"company/quarterly/{cik}.csv" if b.get("q_items") else ""
            for t, n, _ in ts: w.writerow([t, n, cik, f"FY{b['first']}", f"FY{b['last']}", b["items"], b.get("last_q", ""), b.get("q_items", 0), b["updated"], f"company/{cik}.csv", qf])
    size = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(dest) for f in fs)
    summary = {"updated": TODAY.isoformat(), "method": method, "companies_with_files": len(built),
               "companies_with_quarterly_files": sum(1 for b in built.values() if b.get("q_items")),
               "published_megabytes": round(size / 1e6, 1), "categories": CATEGORIES,
               "rebuilt_this_run": len(done_now), "sec_requests_this_run": _requests}
    json.dump(summary, open(os.path.join(dest, "status.json"), "w"), indent=1)
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
