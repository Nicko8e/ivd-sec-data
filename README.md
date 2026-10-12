# ivd-sec-data

Daily SEC EDGAR extraction for the Intrinsic Value Desk Google Sheet and calculator.

GitHub fetches the data (the SEC blocks Google's servers), keeps a record of what it has already
extracted, and each day fetches only what is new. It publishes readable CSV files at:

    https://nicko8e.github.io/ivd-sec-data/

## What is extracted

### 1. Every figure for every company (`company/`)
Every figure each US-listed company has tagged in its SEC filings, in every taxonomy (US GAAP, IFRS,
cover-page "dei" and others). Two files per company:

| File | Contents |
|---|---|
| `company/<CIK>.csv` | Annual: every figure from the annual reports (10-K, 20-F, 40-F), one column per fiscal year, full history (about 2009 onward) |
| `company/quarterly/<CIK>.csv` | Quarterly: every figure by quarter, one column per quarter end, latest 8 years |
| `company/index.csv` | Ticker → CIK, years covered, latest quarter, number of figures, file names |

Each row has a **Category** column, and rows are sorted by it:

1. Income statement
2. Sales by business line (annual files only)
3. Balance sheet
4. Cash flow
5. Per share
6. Shares
7. Employee share pay
8. Company information
9. Other disclosures

**Sales by business line** comes from `scripts/segments.py`, which reads the XBRL data filed with each annual report,
because the SEC's company-facts feed leaves out figures broken down by segment. Each row is the company's sales for one
reportable business segment (`Business segment: …`) or one product or service line (`Product or service: …`), exactly
as reported, under the company's own name for it. The SEC tag column reads `<concept> [<axis>: <member>]`. The first
run reads every listed company's latest annual report and may take a few daily runs; afterwards only new annual
reports are read.

Quarterly amounts are three-month figures. When a company reports only year-to-date amounts (common for cash flow),
or only the full year for its fourth quarter, the quarter is worked out as the difference. Per-share figures and
share counts are never worked out this way, only taken as reported. Balances are as at each quarter end. The first
row of each annual file is the diluted share count from the latest quarterly result.

First run: one bulk download. Daily runs: only companies that filed a new 10-K, 10-Q, 20-F or 40-F since the last run.

### 2. Key figures for all companies side by side (`sheet/`)
One table per figure: one row per company, one column per year (2009 onward) or calendar quarter (latest 10 years).
The Google Sheet imports these into tabs, coloured and ordered by category:

| Category | Annual tables | Quarterly tables |
|---|---|---|
| Income statement | Revenue, Gross profit, Operating income, Research & development, Net income | Revenue, Gross profit, Operating income, Net income |
| Per share | EPS (diluted), Dividends per share | EPS (diluted) |
| Shares | Diluted shares (split-adjusted, unit errors corrected) | Diluted shares |
| Cash flow | Operating cash flow, Capital spending, Share buybacks, Dividends paid, Share-based pay | (in the company files) |
| Balance sheet | Latest balance sheet, Total assets, Total liabilities, Shareholders' equity, Cash | Total assets, Total liabilities, Shareholders' equity, Cash |

`sheet/manifest.json` lists every table with its tab name, category, a fingerprint (so the sheet re-imports only
changed tables) and the extraction log. `sheet/check.json` has coverage counts and sample rows for checking.

### 3. Famous investors' portfolios (`sheet/investors.csv`, `sheet/investor_holdings.csv`)
`scripts/investors.py` reads each tracked investor's SEC Form 13F-HR filings (long US stock positions, reported up to 45 days
after each quarter end), estimates performance by holding each reported portfolio until the next quarter end (13F replication,
13F-implied prices, no dividends) and measures concentration. Investors still filing come first, then concentrated portfolios
(top 10 holdings ≥ 60%), then the best 3-year return. Each filing is fetched once and remembered.

## Setup (once)
1. Public repository `ivd-sec-data` with these files (including the hidden `.github` folder).
2. Settings → Secrets and variables → Actions → **Variables**: `SEC_USER_AGENT` = `Intrinsic Value Desk your-email@example.com`.
3. Settings → Pages → Source: **GitHub Actions**.
4. Actions → "SEC data for Google Sheets" → **Run workflow**.
5. In the Google Sheet: Extensions → Apps Script, paste `SecData.gs`, Save, Run `update`, then SEC data → Start daily updates.
