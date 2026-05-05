# RIA Pipeline

A small Python pipeline that turns the SEC's free Form ADV bulk file into a
segmented contact list of US Registered Investment Advisers, then politely
scrapes firm websites to enrich it with advisor emails. No paid data vendors.

## What it does

1. **Download** the latest "Registered Investment Advisers" monthly snapshot from the SEC.
2. **Parse** the firm-roster file (CSV or XLSX, depending on snapshot vintage) into a clean dataframe with stable column names.
3. **Filter** firms to a configurable Ideal Customer Profile (AUM, employees, HNW mix, state).
4. **Scrape** firm websites for advisor emails (async, robots-respecting, rate-limited).
5. **Output** a single CSV ready for CRM/sequencer import.

## What you get for free from the SEC

The bulk file gives you firm name, address, phone, website, AUM, employee count,
client mix, and custody status for ~16K SEC-registered RIAs. **Advisor emails
come entirely from website scraping** — the current SEC firm-roster CSV does
not include any compliance-officer or contact emails.

## Setup (Windows / PowerShell)

Python 3.11+ required.

```powershell
cd C:\Users\<you>\Documents\python_projects\RIA
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If activation is blocked, run once per shell:
`Set-ExecutionPolicy -Scope Process Bypass`.

Edit `config.py` and replace the placeholder email in `USER_AGENT` with a real
contact address before running anything live.

## Usage

End-to-end:

```powershell
python .\main.py
```

Common iteration flags:

```powershell
# Reuse the xlsx you've already downloaded
python .\main.py --skip-download

# Skip the scraper (only build firms_targeted.csv)
python .\main.py --skip-scrape

# Test the scraper on a small batch
python .\main.py --skip-download --limit 50
```

Each stage is also runnable on its own:

```powershell
python -m src.download_adv
python -m src.parse_adv .\data\raw\<your-snapshot>.xlsx
python -m src.filter_firms
python -m src.scrape_websites --limit 50
```

## Outputs

- `data/raw/ia_YYYY_MM.zip` — the SEC bulk download
- `data/raw/<snapshot>.xlsx` — extracted workbook
- `data/raw/scrape_cache/<domain>/<path>.html` — scraped pages (so you can
  re-run extraction without re-hitting sites)
- `data/processed/firms_clean.parquet` — full clean dataset
- `data/processed/firms_targeted.csv` — ICP-filtered firms with `match_score`
- `data/enriched/ria_targets_YYYYMMDD.csv` — final list, one row per
  (firm, contact)
- `logs/pipeline.log`, `logs/scrape.log`, `logs/column_mapping.log`

## ICP

Edit the `ICP` defaults in `config.py`. The default profile targets:

- Total regulatory AUM in `$250M–$5B`
- `≥ $100M` of HNW client AUM (`min_hnw_aum`)
- `≥ 10` HNW client relationships (`min_hnw_clients`)
- `5–200` employees
- All US states
- Requires a working website (social-media URLs are treated as no website)

The HNW gate is on absolute dollars + count rather than a percentage because
the SEC's current firm-roster CSV exposes Item 5D as count + dollar columns,
not as a percentage. (The fields are `5D(b)(1)` = HNW client count, `5D(b)(3)`
= HNW AUM in USD.)

`match_score` is in [0, 1] and weights:

- **50%** AUM centrality (geometric midpoint of `[aum_min, aum_max]`)
- **35%** HNW-AUM lift on a log scale (10x the threshold = 1.0)
- **15%** HNW client-count lift (3x the threshold = 1.0)

Higher is better.

## Politeness

- One in-flight request per domain at a time, 2-second delay between successive
  requests to the same domain.
- robots.txt is fetched once per domain; disallowed paths are skipped.
- 10-second per-request timeout, exponential back-off on transport errors and
  on 429/503. Plain 4xx is fatal — no retries.
- Identifies as `RIA-Research-Bot/0.1 (contact: ...)`. Set a real email in
  `config.py:USER_AGENT` before running.
- Pages are cached locally so re-runs of the extraction stage do not re-fetch.

## Compliance reminder

Any cold email you send from this list still has to comply with **CAN-SPAM**:
- A real, working unsubscribe mechanism honored within 10 business days.
- Accurate "From", "Reply-To", and routing info (no header forgery).
- Non-deceptive subject lines.
- A valid physical postal address in every message.

Some states have additional anti-spam rules; check before sending at scale.

## Out of scope (intentional)

- **State-registered advisers (<$100M AUM)** — they are not in this dataset.
  If you want to add them, see NASAA's regulator directory:
  https://www.nasaa.org/contact-your-regulator/
- **Exempt Reporting Advisers** — the SEC ships a separate `ia*-exempt.zip`.
  This pipeline ignores it; trivial to add if you want.
- **SEC IAPD scraping** (`adviserinfo.sec.gov`) — bulk data is already free,
  so do not scrape the UI.
- **Email-pattern guessing or SMTP verification** — not in v1.
- **LinkedIn scraping** — ToS issue.

## Tests

A couple of obvious sanity tests live in `tests/`:

```powershell
pip install pytest
pytest -q
```
