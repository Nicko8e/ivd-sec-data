#!/usr/bin/env python3
"""Sales by business line, exactly as each company reports them in its annual report.

The SEC "company facts" feed used by company_financials.py leaves out every figure that is broken down by a dimension,
so a company's sales by segment or by product never reach it. This step reads them from the XBRL data filed with each
annual report (10-K, 10-KT, 20-F, 40-F): sales figures broken down along exactly one of
  - reportable business segments (us-gaap:StatementBusinessSegmentsAxis, ifrs-full:SegmentsAxis)
  - products and services        (srt:ProductOrServiceAxis, ifrs-full:ProductsAndServicesAxis)
for every full fiscal year in the filing (usually the latest three). Names come from the company's own labels.

Least effort:
  - First run: the SEC quarterly filing lists for the last five quarters (5 requests) give each company's latest
    annual report; then 2-3 requests per company (filing index, XBRL data, labels). Long first runs stop after
    SEG_MAX_MINUTES and carry on where they left off the next day.
  - Later runs: the SEC daily filing lists since the last run; only companies with a new annual report are read.

Output: <cache>/seg/<CIK>.json per company (merged into company/<CIK>.csv by company_financials.py, category
"Sales by business line"), and <cache>/seg/state.json, whose "updated" list tells company_financials.py which
company files to rebuild.
"""
import datetime as dt, gzip, json, os, re, sys, time, urllib.error, urllib.request
import xml.etree.ElementTree as ET

CACHE = os.environ.get("SEC_CACHE_DIR", ".sec-cache")
UA = os.environ.get("SEC_USER_AGENT", "").strip()
SEG = os.path.join(CACHE, "seg")
STATE_FILE = os.path.join(SEG, "state.json")
TODAY = dt.date.today()
MAX_MINUTES = float(os.environ.get("SEG_MAX_MINUTES", "90"))
RULES = 1                                             # bump when the reading rules change -> every company is read again
ANNUAL = {"10-K", "10-KT", "20-F", "40-F"}            # amendments rarely repeat the figures, so they are skipped
REVENUE = ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "RevenueFromContractWithCustomerIncludingAssessedTax",
           "SalesRevenueNet", "SalesRevenueGoodsNet", "SalesRevenueServicesNet", "RevenuesNetOfInterestExpense",
           "Revenue", "RevenueFromContractsWithCustomers"]          # last two: IFRS
AXES = {"StatementBusinessSegmentsAxis": "Business segment", "SegmentsAxis": "Business segment",
        "ProductOrServiceAxis": "Product or service", "ProductsAndServicesAxis": "Product or service"}
XBRLI = "{http://www.xbrl.org/2003/instance}"
XBRLDI = "{http://xbrl.org/2006/xbrldi}"
XLINK = "{http://www.w3.org/1999/xlink}"

_requests = 0
def fetch(url, binary=False):
    global _requests
    if "@" not in UA:
        sys.exit("Set the repository variable SEC_USER_AGENT (SEC requirement).")
    time.sleep(0.12)                                  # stays under the SEC's 10 requests a second
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
        except (urllib.error.URLError, TimeoutError):
            if attempt < 2: time.sleep(5 * (attempt + 1)); continue
            raise


def fy_label(end):                                    # same fiscal-year rule as company_financials.py
    e = dt.date.fromisoformat(end)
    return e.year - 1 if (e.month == 1 and e.day <= 7) else e.year


IDX_LINE = re.compile(r"^(\S+)\s{2,}.*?\s{2,}(\d{1,10})\s{2,}(\d{4}-?\d{2}-?\d{2})\s+(edgar/data/\S+\.txt)\s*$")
def parse_index(txt, out):
    """Annual reports in an EDGAR form index -> out[cik] = (filed, accession), keeping each company's newest."""
    for line in (txt or "").splitlines():
        m = IDX_LINE.match(line)
        if not m or m.group(1) not in ANNUAL: continue
        cik, filed = int(m.group(2)), m.group(3).replace("-", "")
        acc = m.group(4).rsplit("/", 1)[-1][:-4]
        if cik not in out or filed > out[cik][0]: out[cik] = (filed, acc)


def daily_index(day):
    """The SEC's list of filings for one day, "" for a day with none (weekends, holidays), or None when the list
    isn't published yet (a missing list older than three days is a holiday). The SEC publishes each day's list late that evening (US time) and answers 403 until then."""
    if day.weekday() >= 5: return ""
    q = (day.month - 1) // 3 + 1
    try: txt = fetch(f"https://www.sec.gov/Archives/edgar/daily-index/{day.year}/QTR{q}/form.{day:%Y%m%d}.idx")
    except urllib.error.HTTPError as e:
        if e.code != 403: raise
        txt = None
    if txt is None:                                       # missing: not published yet, or a holiday once a few days have passed
        return "" if day < TODAY - dt.timedelta(days=3) else None
    return txt


def annual_reports_since(since):
    """Annual reports filed on each day after `since`, and the last day read (unpublished days wait for the next run)."""
    out, day, through = {}, since + dt.timedelta(days=1), since
    while day <= TODAY:
        txt = daily_index(day)
        if txt is None: break
        parse_index(txt, out)
        through = day; day += dt.timedelta(days=1)
    return out, through


def annual_reports_recent():
    out, y, q = {}, TODAY.year, (TODAY.month - 1) // 3 + 1
    for _ in range(5):
        parse_index(fetch(f"https://www.sec.gov/Archives/edgar/full-index/{y}/QTR{q}/form.idx"), out)
        q -= 1
        if q == 0: y, q = y - 1, 4
    return out


def listed_ciks():
    tj = json.loads(fetch("https://www.sec.gov/files/company_tickers_exchange.json"))
    out = set()
    for row in tj["data"]:
        r = dict(zip(tj["fields"], row)); ex = (r.get("exchange") or "").strip()
        if ex and ex.upper() != "OTC": out.add(int(r["cik"]))
    return out


def local(tag): return tag.rsplit("}", 1)[-1]


def read_instance(raw):
    """Sales figures for full fiscal years, total and broken down along one segment or product axis."""
    contexts, units, facts = {}, {}, []
    for _, el in ET.iterparse(__import__("io").BytesIO(raw), events=("end",)):
        t = el.tag
        if t == XBRLI + "context":
            p = el.find(XBRLI + "period")
            start = p.findtext(XBRLI + "startDate") if p is not None else None
            end = p.findtext(XBRLI + "endDate") if p is not None else None
            dims = [(m.get("dimension", ""), (m.text or "").strip()) for m in el.iter(XBRLDI + "explicitMember")]
            typed = any(True for _ in el.iter(XBRLDI + "typedMember"))
            contexts[el.get("id")] = (start and start.strip(), end and end.strip(), dims, typed)
            el.clear()
        elif t == XBRLI + "unit":
            ms = [(m.text or "").strip() for m in el.iter(XBRLI + "measure")]
            units[el.get("id")] = ms[0].split(":")[-1] if len(ms) == 1 else ""
            el.clear()
        elif el.get("contextRef") and local(t) in REVENUE and "{http://www.w3.org/2001/XMLSchema-instance}nil" not in el.attrib:
            try: v = float((el.text or "").strip())
            except ValueError: continue
            ns = t[1:].split("}")[0] if t.startswith("{") else ""
            facts.append((local(t), "ifrs" if "ifrs" in ns else "us-gaap", el.get("contextRef"), el.get("unitRef"), v))
    out = {}                                           # (concept, axis, member) -> {"unit":…, "vals": {fy: value}}
    for concept, tax, cref, uref, v in facts:
        c = contexts.get(cref)
        if not c: continue
        start, end, dims, typed = c
        if not start or not end or typed: continue
        try: days = (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days
        except ValueError: continue
        if not 350 <= days <= 380: continue                       # full years only
        if len(dims) == 0: axis, member = "", ""
        elif len(dims) == 1 and dims[0][0].split(":")[-1] in AXES: axis, member = dims[0][0].split(":")[-1], dims[0][1]
        else: continue
        unit = units.get(uref, "")
        if not re.fullmatch(r"[A-Z]{3}", unit): continue          # money only
        slot = out.setdefault((concept, axis, member), {"unit": unit, "vals": {}})
        slot["vals"][fy_label(end)] = v
    return out


def read_labels(raw):
    """Element id (prefix_Name) -> the company's label, preferring the short ("terse") one."""
    if not raw: return {}
    loc, res, arcs = {}, {}, []
    for _, el in ET.iterparse(__import__("io").BytesIO(raw), events=("end",)):
        n = local(el.tag)
        if n == "loc": loc[el.get(XLINK + "label")] = el.get(XLINK + "href", "").split("#")[-1]
        elif n == "label" and el.get(XLINK + "label"):
            res.setdefault(el.get(XLINK + "label"), []).append((el.get(XLINK + "role", ""), (el.text or "").strip()))
        elif n == "labelArc": arcs.append((el.get(XLINK + "from"), el.get(XLINK + "to")))
    out = {}
    for fr, to in arcs:
        eid = loc.get(fr)
        if not eid: continue
        for role, text in res.get(to, []):
            rank = 0 if role.endswith("/terseLabel") else 1 if role.endswith("/label") else 9
            if text and rank < 9 and (eid not in out or rank < out[eid][0]): out[eid] = (rank, text)
    return {k: v[1] for k, v in out.items()}


def member_name(member, labels):
    eid = member.replace(":", "_")
    name = labels.get(eid)
    if not name:                                       # no label filed: split the element name, e.g. DataCenterMember -> Data Center
        base = member.split(":")[-1]
        base = re.sub(r"(Segment)?Member$", "", base)
        name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", base)
    name = re.sub(r"\s*\[(Member|Domain|Axis)\]\s*$", "", name, flags=re.I).strip()
    name = re.sub(r"\s+(segment|member)$", "", name, flags=re.I).strip()
    return name or member


def read_filing(cik, acc):
    """(form data for one annual report) -> {"items": {...}} or None when it has no sales breakdown."""
    folder = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}"
    idx = fetch(folder + "/index.json")
    if not idx: return None
    files = [f.get("name", "") for f in json.loads(idx).get("directory", {}).get("item", [])]
    xmls = [f for f in files if f.lower().endswith(".xml") and f != "FilingSummary.xml" and not re.search(r"_(cal|def|lab|pre)\.xml$|MetaLinks", f, re.I)]
    inst = next((f for f in xmls if f.lower().endswith("_htm.xml")), None) or (xmls[0] if len(xmls) == 1 else None)
    if not inst: return None
    raw = fetch(folder + "/" + inst, binary=True)
    if not raw: return None
    data = read_instance(raw)
    if not any(axis for (_, axis, _) in data): return None
    lab = next((f for f in files if f.lower().endswith("_lab.xml")), None)
    labels = read_labels(fetch(folder + "/" + lab, binary=True)) if lab else {}
    items = {}
    for (concept, axis, member), d in data.items():
        if not axis: continue
        key = f"{concept} [{axis}: {member}]"
        items[key] = {"label": f"{AXES[axis]}: {member_name(member, labels)}", "unit": d["unit"],
                      "vals": {str(y): v for y, v in d["vals"].items()}}
    return {"items": items}


def main():
    os.makedirs(SEG, exist_ok=True)
    try: state = json.load(open(STATE_FILE))
    except Exception: state = {}
    if state.get("rules") != RULES: state = {"rules": RULES}
    t0 = time.time()
    listed = listed_ciks()
    done = state.get("done", {})                       # cik -> accession already read
    pending = {int(k): tuple(v) for k, v in state.get("pending", {}).items()}
    last = dt.date.fromisoformat(state["last_checked"]) if state.get("last_checked") else None
    if last: found, through = annual_reports_since(last)
    else: found, through = annual_reports_recent(), TODAY - dt.timedelta(days=1)
    for cik, fa in found.items():
        if cik in listed and done.get(str(cik)) != fa[1] and (cik not in pending or fa[0] >= pending[cik][0]): pending[cik] = fa
    updated, read, stopped = [], 0, False
    for cik in sorted(pending, key=lambda c: pending[c][0], reverse=True):     # newest reports first
        if (time.time() - t0) / 60 > MAX_MINUTES: stopped = True; break
        filed, acc = pending[cik]
        try: got = read_filing(cik, acc)
        except Exception as e:
            print(f"CIK {cik} {acc}: {e}", flush=True); got = None
        read += 1
        path = os.path.join(SEG, f"{cik}.json")
        if got:
            try: old = json.load(open(path))
            except Exception: old = {"items": {}}
            for k, it in got["items"].items():           # newer reports restate older years; keep years they no longer show
                o = old["items"].get(k)
                if o: o["vals"].update(it["vals"]); o["label"] = it["label"]; o["unit"] = it["unit"]
                else: old["items"][k] = it
            old.update({"accession": acc, "filed": filed})
            json.dump(old, open(path, "w"))
            updated.append(cik)
        done[str(cik)] = acc
        pending.pop(cik, None)
    state.update({"rules": RULES, "done": done, "pending": {str(k): list(v) for k, v in pending.items()},
                  "last_checked": through.isoformat(), "updated": updated})
    json.dump(state, open(STATE_FILE, "w"))
    print(json.dumps({"updated": TODAY.isoformat(), "annual_reports_read": read, "companies_with_new_breakdowns": len(updated),
                      "still_to_read": len(pending), "stopped_for_time": stopped, "sec_requests_this_run": _requests}))


if __name__ == "__main__":
    main()
