# ivd-sec-data

Weekly SEC EDGAR extraction for the Intrinsic Value Desk Google Sheet.

GitHub fetches the data (the SEC blocks Google's servers), keeps a record of what it has
already extracted, and each week fetches only new years, the latest two years and the latest
balance sheet. It publishes readable CSV tables at:

    https://YOUR-GITHUB-NAME.github.io/ivd-sec-data/sheet/

## Setup (once)
1. Create a new **public** repository named `ivd-sec-data` and upload these files
   (including the hidden `.github` folder).
2. Settings → Secrets and variables → Actions → **Variables** → New repository variable:
   `SEC_USER_AGENT` = `Intrinsic Value Desk your-email@example.com` (the SEC requires a contact).
3. Settings → Pages → Source: **GitHub Actions**.
4. Actions → "SEC data for Google Sheets" → **Run workflow**. The first run takes about 5 minutes.
5. Open `https://YOUR-GITHUB-NAME.github.io/ivd-sec-data/sheet/manifest.json` to check it worked.
6. In the Google Sheet's Apps Script, put that address (ending in `/sheet/`) in `DATA_URL`, then Run `update`.

## Full financial statements
Every annual figure each company reported to the SEC (about 2009 onward), one file per company at
`company/<SEC CIK>.csv` (look up the CIK in `company/index.csv`). First run: one bulk download.
Daily runs: only companies that filed a new 10-K, 10-Q, 20-F or 40-F since the last run.

## Files published
companies.csv, revenue.csv, net_income.csv, diluted_shares.csv, balance_sheet.csv, and manifest.json
(update time, a fingerprint per file so the sheet only re-imports changed tables, and the extraction log).
