#!/usr/bin/env python3
"""Famous investors' portfolios, from their SEC Form 13F filings, ranked by performance and concentration.

Source: Form 13F-HR. Every investment manager with over $100 million in US-listed stocks must report its
long positions in those stocks within 45 days of each quarter end. 13F does not show short positions,
cash, bonds, most non-US holdings or trades made during the quarter.

Performance is a "13F replication" estimate: each quarter's reported portfolio is held, unchanged, until
the next quarter end, and priced with the share prices implied by all 13F reports for that quarter end
(value ÷ shares). Dividends are not included. The US market comparison is the same calculation applied
to Vanguard's 13F holdings (which cover almost every US stock, weighted by size).

Least effort: each filing is downloaded once and remembered in <cache>/13f/; later runs only fetch new filings.
Output: <out>/sheet/investors.csv, <out>/sheet/investor_holdings.csv (added to sheet/manifest.json) and
<out>/investors/status.json
"""
import csv, datetime as dt, gzip, hashlib, io, json, os, re, statistics, sys, time, urllib.error, urllib.request
import xml.etree.ElementTree as ET

OUT = sys.argv[1] if len(sys.argv) > 1 else "_site"
CACHE = os.path.join(os.environ.get("SEC_CACHE_DIR", ".sec-cache"), "13f")
UA = os.environ.get("SEC_USER_AGENT", "").strip()
QUARTERS = 21                  # latest 21 quarter-ends: enough for a 5-year record
CONCENTRATED_TOP10 = 0.60      # "concentrated": the 10 largest positions are at least 60% of the portfolio
TOP_HOLDINGS = 30              # holdings listed per investor

# (person, firm as filed, SEC CIK, word that must appear in the filer name, style)
INVESTORS = [
    ("Warren Buffett", "Berkshire Hathaway", 1067983, "BERKSHIRE", "Quality businesses held for decades"),
    ("Stanley Druckenmiller", "Duquesne Family Office", 1536411, "DUQUESNE", "Macro and growth, concentrated, trades actively"),
    ("Brad Gerstner", "Altimeter Capital Management", 1541617, "ALTIMETER", "Technology growth"),
    ("Bill Ackman", "Pershing Square Capital Management", 1336528, "PERSHING", "Concentrated quality, activist"),
    ("Michael Burry", "Scion Asset Management", 1649339, "SCION", "Deep value and contrarian"),
    ("Li Lu", "Himalaya Capital Management", 1709323, "HIMALAYA", "Concentrated value (Munger-backed)"),
    ("Terry Smith", "Fundsmith", 1569205, "FUNDSMITH", "Quality compounders"),
    ("Chuck Akre", "Akre Capital Management", 1112520, "AKRE", "Compounders (three-legged stool)"),
    ("Seth Klarman", "Baupost Group", 1061768, "BAUPOST", "Value with a margin of safety"),
    ("David Tepper", "Appaloosa", 1656456, "APPALOOSA", "Opportunistic, macro-aware"),
    ("Mohnish Pabrai", "Dalal Street (Pabrai)", 1549575, "DALAL", "Concentrated deep value"),
    ("Chase Coleman", "Tiger Global Management", 1167483, "TIGER GLOBAL", "Technology growth"),
    ("Philippe Laffont", "Coatue Management", 1135730, "COATUE", "Technology growth"),
    ("Cathie Wood", "ARK Investment Management", 1697748, "ARK", "Disruptive innovation"),
    ("Dan Loeb", "Third Point", 1040273, "THIRD POINT", "Event-driven, activist"),
    ("David Einhorn", "Greenlight Capital", 1079114, "GREENLIGHT", "Value, long/short"),
    ("Stephen Mandel", "Lone Pine Capital", 1061165, "LONE PINE", "Growth, long/short"),
    ("Andreas Halvorsen", "Viking Global Investors", 1103804, "VIKING", "Long/short fundamental"),
    ("George Soros", "Soros Fund Management", 1029160, "SOROS", "Macro"),
    ("Carl Icahn", "Icahn Carl C", 921669, "ICAHN", "Activist"),
    ("Bill Gates", "Gates Foundation Trust", 1166559, "GATES", "Long-term (foundation endowment)"),
    ("Tom Gayner", "Markel Group", 1096343, "MARKEL", "Quality compounders"),
    ("Howard Marks", "Oaktree Capital Management", 949509, "OAKTREE", "Credit and distressed value"),
    ("Charlie Munger (legacy)", "Daily Journal", 783412, "DAILY JOURNAL", "Concentrated quality"),
    ("Dan Sundheim", "D1 Capital Partners", 1747057, "D1 CAPITAL", "Growth, long/short"),
    ("Ray Dalio", "Bridgewater Associates", 1350694, "BRIDGEWATER", "Macro, diversified"),
    ("Joel Greenblatt", "Gotham Asset Management", 1510387, "GOTHAM", "Quantitative value"),
    ("Bill Nygren", "Harris Associates", 813917, "HARRIS", "Value"),
    ("Glenn Greenberg", "Brave Warrior Advisors", 1553733, "BRAVE WARRIOR", "Concentrated value"),
    ("Bruce Berkowitz", "Fairholme Capital Management", 1056831, "FAIRHOLME", "Concentrated value"),
]
REFERENCE = [("Vanguard Group", 102909, "VANGUARD")]   # prices for almost every US stock, and the market comparison

_requests = 0
def get(url, binary=False):
    global _requests
    if "@" not in UA: sys.exit("Set the repository variable SEC_USER_AGENT (SEC requirement).")
    time.sleep(0.12)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip": raw = gzip.decompress(raw)
                _requests += 1
                return raw if binary else raw.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 404: _requests += 1; return None
            if e.code in (429, 500, 502, 503) and attempt < 2: time.sleep(5 * (attempt + 1)); continue
            raise


def filings_13f(cik):
    """(filer name, [{acc, period, filed}] newest first) for original 13F-HR reports (one per quarter end)."""
    raw = get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
    if not raw: return None, []
    j = json.loads(raw)
    out, seen = [], set()
    def take(r):
        for form, acc, filed, period in zip(r.get("form", []), r.get("accessionNumber", []), r.get("filingDate", []), r.get("reportDate", [])):
            if form != "13F-HR" or not period or period in seen: continue      # newest original report for each quarter end
            seen.add(period); out.append({"acc": acc, "period": period, "filed": filed})
    take(j.get("filings", {}).get("recent", {}))
    # busy filers (Vanguard, Berkshire) push older 13F reports onto further pages of the filing list
    for extra in j.get("filings", {}).get("files", []):
        if len(out) >= QUARTERS: break
        more = get("https://data.sec.gov/submissions/" + extra["name"])
        if more: take(json.loads(more))
    out.sort(key=lambda x: x["period"], reverse=True)
    return j.get("name", ""), out


def parse_infotable(xml_bytes, filed):
    """{cusip: {name, cls, value, shares}} for share positions (options and principal amounts left out). Values in dollars."""
    scale = 1000 if filed < "2023-01-03" else 1                          # before 2023, 13F values were in thousands
    hold = {}
    for ev, el in ET.iterparse(io.BytesIO(xml_bytes)):
        if not el.tag.endswith("infoTable"): continue
        f = {c.tag.split("}")[-1]: c for c in el.iter()}
        txt = lambda k: (f[k].text or "").strip() if k in f and f[k].text else ""
        if txt("putCall") or (txt("sshPrnamtType") or "SH") != "SH": el.clear(); continue
        cusip = txt("cusip").upper()
        try: val = float(txt("value")) * scale; sh = float(txt("sshPrnamt"))
        except ValueError: el.clear(); continue
        if not cusip or sh <= 0: el.clear(); continue
        h = hold.setdefault(cusip, {"name": txt("nameOfIssuer"), "cls": txt("titleOfClass"), "value": 0.0, "shares": 0.0})
        h["value"] += val; h["shares"] += sh
        el.clear()
    return hold


def holdings(cik, f):
    """Parsed holdings of one filing, cached on disk."""
    path = os.path.join(CACHE, f"{cik}_{f['acc']}.json")
    try: return json.load(open(path))
    except Exception: pass
    acc = f["acc"].replace("-", "")
    idx = get(f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/index.json")
    if not idx: return None
    items = [i["name"] for i in json.loads(idx).get("directory", {}).get("item", [])]
    xmls = [n for n in items if n.lower().endswith(".xml") and "primary_doc" not in n.lower()]
    xmls.sort(key=lambda n: ("info" not in n.lower(), n))
    for n in xmls:
        b = get(f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{n}", binary=True)
        if not b: continue
        try: h = parse_infotable(b, f["filed"])
        except ET.ParseError: continue
        if h:
            os.makedirs(CACHE, exist_ok=True); json.dump(h, open(path, "w")); return h
    return None


SPLITS = [2, 3, 4, 5, 6, 8, 10, 15, 20, 25, 30, 40, 50, 100]
def split_factor(k):
    """k = shares after / shares before for a big index holder; returns the split factor if k looks like a split."""
    if not k: return 1
    for s in SPLITS:
        for f in (s, 1 / s):
            if abs(k / f - 1) < 0.08 and (k >= 1.8 or k <= 0.56): return f
    return 1


def portfolio_return(h0, prices1, ref0, ref1):
    """Return of portfolio h0 held to the next quarter end; (return, share of the portfolio that could be priced)."""
    tot = sum(x["value"] for x in h0.values())
    if tot <= 0: return None, 0
    covered, acc = 0.0, 0.0
    for cusip, x in h0.items():
        p0 = x["value"] / x["shares"]; p1 = prices1.get(cusip)
        if not p1 or p0 <= 0: continue
        r = p1 / p0
        k = (ref1.get(cusip, {}).get("shares") or 0) / (ref0.get(cusip, {}).get("shares") or 1) if cusip in ref0 and cusip in ref1 else None
        if (r < 0.56 or r > 1.8) and k: r *= split_factor(k)       # restate for a stock split between the two quarter ends
        if r < 0.05 or r > 20: continue                                # unusable price
        w = x["value"] / tot; covered += w; acc += w * (r - 1)
    return (acc / covered if covered > 0 else None), covered


def annualised(rets):
    if not rets or any(r is None for r in rets): return None
    g = 1.0
    for r in rets: g *= 1 + r
    return g ** (4 / len(rets)) - 1 if len(rets) >= 4 else g - 1


def main():
    os.makedirs(CACHE, exist_ok=True)
    managers, problems = [], []
    for person, firm, cik, word, style in INVESTORS:
        name, fl = filings_13f(cik)
        if not fl or word not in (name or "").upper():
            problems.append(f"{person}: CIK {cik} is '{name or 'not found'}', no 13F-HR filings matched"); continue
        managers.append({"person": person, "firm": firm, "cik": cik, "filer": name, "style": style, "filings": fl[:QUARTERS]})
    refs = []
    for firm, cik, word in REFERENCE:
        name, fl = filings_13f(cik)
        if fl and word in (name or "").upper(): refs.append({"firm": firm, "cik": cik, "filings": fl[:QUARTERS]})

    # holdings per filing (cached) and implied prices per quarter end (median of value ÷ shares over every report)
    per_period = {}
    for m in managers + refs:
        m["h"] = {}
        for f in m["filings"]:
            h = holdings(m["cik"], f)
            if h: m["h"][f["period"]] = h; per_period.setdefault(f["period"], []).append(h)
    prices = {}
    for period, hs in per_period.items():
        acc = {}
        for h in hs:
            for c, x in h.items(): acc.setdefault(c, []).append(x["value"] / x["shares"])
        prices[period] = {c: statistics.median(v) for c, v in acc.items()}
    periods = sorted(prices)
    ref = refs[0]["h"] if refs else {}

    def returns(hmap):
        out = []
        for a, b in zip(periods, periods[1:]):
            if a not in hmap: out.append((b, None, 0)); continue
            r, cov = portfolio_return(hmap[a], prices.get(b, {}), ref.get(a, {}), ref.get(b, {}))
            out.append((b, r if cov >= 0.6 else None, cov))
        return out
    market = returns(ref) if ref else []

    rows, hold_rows = [], []
    for m in managers:
        if not m["h"]: problems.append(f"{m['person']}: no readable holdings"); continue
        latest = max(m["h"]); h = m["h"][latest]; tot = sum(x["value"] for x in h.values())
        stale = (dt.date.today() - dt.date.fromisoformat(latest)).days > 200     # no report for the latest quarter end
        ws = sorted(((x["value"] / tot, c, x) for c, x in h.items()), reverse=True)
        top10 = sum(w for w, _, _ in ws[:10]); hhi = sum(w * w for w, _, _ in ws)
        q = returns(m["h"])
        def window(n, series):
            s = [r for _, r, _ in series][-n:]
            return annualised(s) if len(s) == n else None
        cov = [c for _, r, c in q[-12:] if r is not None]
        prev_periods = [p for p in sorted(m["h"]) if p < latest]; prev = m["h"][prev_periods[-1]] if prev_periods else {}
        f_latest = next(f for f in m["filings"] if f["period"] == latest)
        acc = f_latest["acc"].replace("-", "")
        rows.append({"Investor": m["person"], "Firm": m["firm"], "Style": m["style"], "Latest quarter": latest, "Filed": f_latest["filed"],
                     "Portfolio value ($M)": round(tot / 1e6, 1), "Positions": len(h), "Top 10 weight (%)": round(top10 * 100, 1),
                     "Largest position": ws[0][2]["name"] if ws else "", "Largest weight (%)": round(ws[0][0] * 100, 1) if ws else "",
                     "Effective positions": round(1 / hhi, 1) if hhi else "", "Concentrated": "yes" if top10 >= CONCENTRATED_TOP10 else "no", "Still filing": "no" if stale else "yes",
                     "Return 1y (%)": pct(window(4, q)), "Return 3y a year (%)": pct(window(12, q)), "Return 5y a year (%)": pct(window(20, q)),
                     "US market 1y (%)": pct(window(4, market)), "US market 3y a year (%)": pct(window(12, market)), "US market 5y a year (%)": pct(window(20, market)),
                     "Quarters measured": sum(1 for _, r, _ in q if r is not None), "Priced share of portfolio (%)": round(statistics.mean(cov) * 100) if cov else "",
                     "SEC CIK": m["cik"], "Latest filing": f"https://www.sec.gov/Archives/edgar/data/{m['cik']}/{acc}/"})
        for i, (w, c, x) in enumerate(ws[:TOP_HOLDINGS]):
            p = prev.get(c)
            if not p: chg, chp = ("New" if prev else ""), ""
            else:
                d = x["shares"] / p["shares"] - 1
                chg = "Added" if d > 0.02 else "Reduced" if d < -0.02 else "Unchanged"; chp = round(d * 100, 1)
            hold_rows.append([m["person"], m["firm"], latest, i + 1, x["name"], c, x["cls"], round(x["value"] / 1e6, 2), round(w * 100, 2), int(x["shares"]), chg, chp])
        for c, p in prev.items():
            if c not in h: hold_rows.append([m["person"], m["firm"], latest, "", p["name"], c, p["cls"], 0, 0, 0, "Sold", -100])

    # ranking: investors still filing first; among them concentrated portfolios first, then the best 3-year (else 1-year) performance
    def key(r):
        perf = r["Return 3y a year (%)"] if r["Return 3y a year (%)"] != "" else r["Return 1y (%)"]
        return (r["Still filing"] != "yes", r["Concentrated"] != "yes", -(perf if perf != "" else -1e9))
    rows.sort(key=key)
    for i, r in enumerate(rows): r["Rank"] = i + 1

    sheet = os.path.join(OUT, "sheet"); os.makedirs(sheet, exist_ok=True)
    cols = ["Rank"] + [k for k in rows[0] if k != "Rank"] if rows else ["Rank"]
    files = {}
    files["investors.csv"] = write_csv(os.path.join(sheet, "investors.csv"), cols, [[r.get(k, "") for k in cols] for r in rows])
    files["investor_holdings.csv"] = write_csv(os.path.join(sheet, "investor_holdings.csv"),
        ["Investor", "Firm", "Quarter", "Rank in portfolio", "Company", "CUSIP", "Class", "Value ($M)", "Weight (%)", "Shares", "Change vs previous quarter", "Change in shares (%)"], hold_rows)
    # add both tables to the sheet manifest so the Google Sheet imports them
    mpath = os.path.join(sheet, "manifest.json")
    try: man = json.load(open(mpath))
    except Exception: man = {"files": {}}
    order = max([v.get("order", 0) for v in man.get("files", {}).values()] + [0]) + 1
    for i, (f, tab) in enumerate((("investors.csv", "Famous investors"), ("investor_holdings.csv", "Investor holdings"))):
        man.setdefault("files", {})[f] = {**files[f], "tab": tab, "category": "Famous investors", "kind": "latest", "order": order + i}
    if "Famous investors" not in man.setdefault("categories", []): man["categories"].append("Famous investors")
    json.dump(man, open(mpath, "w"), indent=1)
    status = {"updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "investors": len(rows), "quarter_ends": periods[-QUARTERS:],
              "sec_requests_this_run": _requests, "problems": problems,
              "source": "SEC Form 13F-HR filings (EDGAR). Long US stock positions only, reported up to 45 days after each quarter end.",
              "method": "13F replication: each reported portfolio held to the next quarter end, priced with 13F-implied prices; dividends excluded."}
    os.makedirs(os.path.join(OUT, "investors"), exist_ok=True)
    json.dump(status, open(os.path.join(OUT, "investors", "status.json"), "w"), indent=1)
    print(json.dumps({k: status[k] for k in ("investors", "sec_requests_this_run", "problems")}))


def pct(x): return "" if x is None else round(x * 100, 1)


def write_csv(path, header, rows):
    buf = io.StringIO(); w = csv.writer(buf, lineterminator="\n"); w.writerow(header); w.writerows(rows)
    data = buf.getvalue().encode(); open(path, "wb").write(data)
    return {"rows": len(rows), "sha": hashlib.sha256(data).hexdigest()[:16]}


if __name__ == "__main__":
    main()
