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

### Primary-contact pass (CFO cascade)

A second scrape that finds one decision-maker per firm and adds three
firm-level columns to the enriched master (`primary_contact_title`,
`primary_contact_name`, `primary_contact_email`):

```powershell
python -m src.scrape_primary_contact               # full run, updates CSV + xlsx
python -m src.scrape_primary_contact --limit 25    # first 25 firms
python -m src.scrape_primary_contact --cache-only  # no network, cached pages only
python -m src.scrape_primary_contact --dry-run     # print, write nothing
```

It walks a strict title cascade and stops at the first level that yields a
person: **CFO → COO → Director of Finance → Controller → Managing Partner →
any contact found** (with whatever title they have). Cached pages are scanned
first; leadership-likely paths (`/team`, `/leadership`, `/management`, ...)
are then fetched with the same politeness rules as the main scraper. Emails
are taken from the page when visible, otherwise matched from the firm's
already-scraped emails by name pattern (`jane.doe@`, `jdoe@`, ...).

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

## FDIC bank pipeline (`fdic_main.py`)

A parallel pipeline that targets **FDIC-insured banks** instead of RIAs. It
shares the same two scrapers and the same Zomma framework as the RIA pipeline —
only the data source and the scoring inputs differ.

Unlike Form ADV, the FDIC BankFind Suite is a **clean public JSON API**
(`api.fdic.gov/banks/institutions`) that returns the bank's website (`WEBADDR`)
directly, so there is nothing to download-and-unzip and no HTML to scrape for
the institution data — Stage 1 is a thin paginated API client. ~4,300 active
institutions, ~98% with a usable website.

Stages:

1. **Fetch + clean** (`src/fdic_fetch.py`) — page the institutions endpoint,
   write raw CSV + a clean parquet whose columns match the shared scraper schema
   (`cert_number`, `firm_legal_name`, `website`, `office_state`, `asset_total`,
   `deposits`, `offices`, ...). `ASSET`/`DEP` arrive in $thousands and are
   converted to dollars.
2. **Filter** (`src/fdic_filter.py`) — bank ICP (asset band, state, website
   required). `match_score` favours **small** banks so `--limit` scrapes the
   most on-thesis banks first.
3. **Scrape websites** — reuses `src.scrape_websites` verbatim (banks carry a
   `cert_number` key instead of `crd_number`).
4. **Primary-contact cascade** — reuses `src.scrape_primary_contact`.
5. **Zomma Priority** (`src/fdic_zomma.py`) — the RIA thesis ported to bank
   fields (see below).

```powershell
python .\fdic_main.py                      # full run (fetch -> filter -> scrape -> score)
python .\fdic_main.py --skip-fetch         # reuse the cached clean parquet
python .\fdic_main.py --skip-fetch --limit 25   # smoke test on 25 banks
python .\fdic_main.py --state TX           # one state
python .\fdic_main.py --skip-scrape        # stop after the ICP filter
```

Each stage also runs standalone (`python -m src.fdic_fetch`, `... fdic_filter`,
`... fdic_zomma <master.csv>`).

### Bank Zomma Priority

Same 1–5 percentile buckets, same weights, same Fit / Segment / RPA-capable
extras as the RIA scorer — with bank analogs for the two RIA signals FDIC data
lacks (there is **no employee or client count** per bank):

| RIA component (weight)        | FDIC analog                                                        |
|-------------------------------|--------------------------------------------------------------------|
| service complexity (0.30)     | **branch footprint** — more offices = more cores/portals to reconcile |
| size: smaller better (0.35)   | smallness of **total assets** (ICP band)                           |
| contact reachability (0.20)   | identical (scraped emails + named people)                          |
| ops intensity (0.15)          | **assets-per-branch thinness** — more manual overhead per dollar   |

Footprint and asset-size are anti-correlated for banks (~−0.73), so the
footprint weight is dialed **below** the size weight (the "Balanced" tuning) to
keep the focus on smaller, "too small for RPA" banks rather than the larger,
already-automatable end.

Weights and the saturation constants live at the top of `src/fdic_zomma.py` —
run a scrape, eyeball the printed 1–5 distribution, and tune.

### FDIC outputs

- `data/fdic/raw/institutions_YYYYMMDD.csv` — raw API pull
- `data/fdic/processed/banks_clean.parquet` — full clean dataset
- `data/fdic/processed/banks_targeted.csv` — ICP-filtered banks with `match_score`
- `data/fdic/enriched/fdic_targets_YYYYMMDD.csv` — final list, one row per
  (bank, contact), with primary-contact and Zomma columns

## NCUA credit-union pipeline (`ncua_main.py`)

Targets **NCUA credit unions**. Same scrapers and Zomma framework again — but the
data source is a **quarterly bulk download**, not an API, and it carries **no
website**, so there's one extra stage.

Stages:

1. **Fetch + parse** (`src/ncua_fetch.py`) — auto-detect and download the latest
   5300 Call Report quarter ZIP, read the profile (`FOICU.txt`) and the financial
   schedules (`FS220.txt` → total assets `ACCT_010` + members `ACCT_083`;
   `FS220A.txt` → employees `ACCT_564A/B`), count branches from the branch file,
   and emit a clean parquet (`cu_number`, `firm_legal_name`, `asset_total`,
   `members`, `employee_count`, `offices`, ...). Assets are already in dollars.
2. **Pull official Profiles** (`src/ncua_profile.py`) — the call-report bulk data
   has no URL, but NCUA's "Research a Credit Union" tool is backed by a public
   JSON API (`mapping.ncua.gov/api/CreditUnionDetails/GetCreditUnionDetails/{charter}`)
   that returns each CU's official **website**, **CEO/manager name**, and phone —
   for **~95%** of credit unions. Free and government-sourced; cached to
   `data/ncua/processed/ncua_profiles.csv`. The CEO name is reconciled onto the
   `primary_contact_*` columns after scraping and matched to a scraped email, so
   you get the official decision-maker plus their address. (A name-based
   domain-guesser, `src/ncua_discover_sites.py`, remains available standalone as
   a fallback for the ~5% the API leaves blank.)
3. **Filter** (`src/ncua_filter.py`) — CU ICP (asset band, state, website found).
4–5. **Scrape websites + primary contact** — the shared engines (CUs carry a
   `cu_number` key).
6. **Zomma Priority** (`src/ncua_zomma.py`) — the closest of the three sources to
   the RIA model, because CUs report both members and employees: the ops signal
   is genuine **members-per-employee density** (≙ RIA's clients-per-employee),
   not a proxy. Footprint ← branches, size ← asset smallness, contact ← reachability.

```powershell
python .\ncua_main.py --limit 50          # smoke test (download, discover, scrape 50)
python .\ncua_main.py                       # full run, latest quarter
python .\ncua_main.py --skip-fetch          # reuse cached clean parquet
python .\ncua_main.py --skip-discover       # reuse only already-cached sites
python .\ncua_main.py --state TX
```

### NCUA outputs

- `data/ncua/raw/call-report-data-YYYY-MM.zip` — the quarterly bulk download
- `data/ncua/processed/cus_clean.parquet` — full clean dataset
- `data/ncua/processed/ncua_profiles.csv` — NCUA Profile cache (website + CEO + phone)
- `data/ncua/processed/discovered_sites.csv` — domain-guess fallback cache
- `data/ncua/processed/cus_targeted.csv` — ICP-filtered CUs with `match_score`
- `data/ncua/enriched/ncua_targets_YYYYMMDD.csv` — final list with contacts + Zomma

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
