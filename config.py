"""Central config for the RIA pipeline.

Tweak ICP values, paths, and scraper rate limits here. Keep secrets out of this file.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

USER_AGENT = "Mozilla/5.0 (compatible; RIA-Research/0.1; +mailto:aubrey3993@gmail.com)"

# --- Paths --------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
ENRICHED_DIR = DATA_DIR / "enriched"
LOG_DIR = PROJECT_ROOT / "logs"
SCRAPE_CACHE_DIR = RAW_DIR / "scrape_cache"

CLEAN_PARQUET = PROCESSED_DIR / "firms_clean.parquet"
TARGETED_CSV = PROCESSED_DIR / "firms_targeted.csv"
COLUMN_MAP_LOG = LOG_DIR / "column_mapping.log"
SCRAPE_LOG = LOG_DIR / "scrape.log"

# --- SEC source ---------------------------------------------------------------
SEC_INDEX_URL = (
    "https://www.sec.gov/data-research/sec-markets-data/"
    "information-about-registered-investment-advisers-exempt-reporting-advisers"
)

# --- FDIC source (BankFind Suite API) -----------------------------------------
# Clean public JSON API — no HTML scraping for the institution data. The
# institutions endpoint hands back the bank's website (WEBADDR) directly, which
# feeds straight into the shared website/primary-contact scrapers.
#   docs: https://api.fdic.gov/banks/docs   (banks.data.fdic.gov redirects here)
FDIC_API_BASE = "https://api.fdic.gov/banks"

FDIC_DATA_DIR = DATA_DIR / "fdic"
FDIC_RAW_DIR = FDIC_DATA_DIR / "raw"
FDIC_PROCESSED_DIR = FDIC_DATA_DIR / "processed"
FDIC_ENRICHED_DIR = FDIC_DATA_DIR / "enriched"
FDIC_CLEAN_PARQUET = FDIC_PROCESSED_DIR / "banks_clean.parquet"
FDIC_TARGETED_CSV = FDIC_PROCESSED_DIR / "banks_targeted.csv"

# Fields requested from the institutions endpoint. ASSET/DEP are in $THOUSANDS
# (we convert to dollars on parse). OFFICES = branch count. There is no
# employee count in the public data — the bank size/ops signal comes from
# ASSET, DEP, and OFFICES instead.
FDIC_INSTITUTION_FIELDS = [
    "NAME", "CERT", "ADDRESS", "CITY", "STALP", "STNAME", "ZIP",
    "ASSET", "DEP", "OFFICES", "WEBADDR", "BKCLASS", "ACTIVE",
    "ESTYMD", "ROA", "ROE", "NETINC",
]


# --- Bank ICP (FDIC) ----------------------------------------------------------
@dataclass
class BankICP:
    """Ideal Customer Profile filters for FDIC-insured banks. Asset values are
    in DOLLARS (parse converts the API's $-thousands up front)."""

    asset_min: int = 50_000_000          # $50M — drop the very smallest shells
    asset_max: int = 10_000_000_000      # $10B — community/small-bank ceiling
    states: list[str] | None = None      # None means all US states + territories
    exclude_no_website: bool = True


DEFAULT_BANK_ICP = BankICP()


# --- NCUA source (5300 Call Report quarterly bulk data) -----------------------
# No live API: a quarterly ZIP of comma-delimited text files keyed on CU_NUMBER.
# Profile (name/address) is in FOICU.txt; financials are ACCT_* codes spread
# across FS220*.txt schedules. There is NO website or email field anywhere in
# the data, so websites must be discovered (src/ncua_discover_sites.py) before
# the shared contact scrapers can run.
#   page: https://ncua.gov/analysis/credit-union-corporate-call-report-data/quarterly-data
NCUA_QUARTERLY_URL = "https://www.ncua.gov/files/publications/analysis/call-report-data-{ym}.zip"

NCUA_DATA_DIR = DATA_DIR / "ncua"
NCUA_RAW_DIR = NCUA_DATA_DIR / "raw"
NCUA_PROCESSED_DIR = NCUA_DATA_DIR / "processed"
NCUA_ENRICHED_DIR = NCUA_DATA_DIR / "enriched"
NCUA_CLEAN_PARQUET = NCUA_PROCESSED_DIR / "cus_clean.parquet"
NCUA_TARGETED_CSV = NCUA_PROCESSED_DIR / "cus_targeted.csv"
NCUA_SITE_CACHE = NCUA_PROCESSED_DIR / "discovered_sites.csv"

# Account codes (confirmed against AcctDesc.txt). Assets are in actual DOLLARS
# (no $-thousands scaling, unlike FDIC).
NCUA_ACCT_ASSETS = "ACCT_010"      # FS220.txt  — Total Assets
NCUA_ACCT_MEMBERS = "ACCT_083"     # FS220.txt  — Number of current members
NCUA_ACCT_FT_EMP = "ACCT_564A"     # FS220A.txt — Full-time employees
NCUA_ACCT_PT_EMP = "ACCT_564B"     # FS220A.txt — Part-time employees


# --- Credit-union ICP (NCUA) --------------------------------------------------
@dataclass
class CreditUnionICP:
    """Ideal Customer Profile filters for NCUA credit unions. Asset values are
    in DOLLARS. The website gate applies AFTER site discovery."""

    asset_min: int = 50_000_000          # $50M floor
    asset_max: int = 5_000_000_000       # $5B ceiling (keeps it community-focused)
    states: list[str] | None = None
    exclude_no_website: bool = True       # drop CUs with no discoverable site


DEFAULT_CU_ICP = CreditUnionICP()

# Firms sometimes file a social-media URL in lieu of a website. These never
# yield advisor emails and may violate the platform's ToS to scrape — drop
# them at filter time and as a scraper safety net.
SOCIAL_URL_BLOCKLIST = [
    "facebook.com",
    "linkedin.com",
    "twitter.com",
    "x.com",
    "instagram.com",
    "youtube.com",
    "tiktok.com",
]


# --- ICP ----------------------------------------------------------------------
@dataclass
class ICP:
    """Ideal Customer Profile filters. Override fields when constructing."""

    aum_min: int = 250_000_000
    aum_max: int = 5_000_000_000
    states: list[str] | None = None  # None means all US states + territories
    min_hnw_aum: int = 100_000_000      # $100M in HNW AUM
    min_hnw_clients: int = 10           # at least 10 HNW client relationships
    min_employees: int = 5
    max_employees: int = 200
    exclude_no_website: bool = True


DEFAULT_ICP = ICP()


# --- Scraper ------------------------------------------------------------------
@dataclass
class ScraperConfig:
    max_concurrent_domains: int = 10
    per_domain_delay_seconds: float = 2.0
    request_timeout_seconds: float = 10.0
    max_retries: int = 3
    backoff_base_seconds: float = 2.0
    cache_pages: bool = True
    team_paths: list[str] = field(
        default_factory=lambda: [
            "/",
            "/team",
            "/about",
            "/about-us",
            "/our-team",
            "/people",
            "/advisors",
            "/our-advisors",
            "/staff",
            "/contact",
            "/leadership",
        ]
    )
    junk_email_local_parts: set[str] = field(
        default_factory=lambda: {
            "noreply",
            "no-reply",
            "donotreply",
            "do-not-reply",
            "postmaster",
            "mailer-daemon",
            "abuse",
            "webmaster",
        }
    )
    # Local parts kept only when nothing better is found.
    low_value_local_parts: set[str] = field(
        default_factory=lambda: {"info", "support", "hello", "contact", "admin", "office"}
    )
    # Domains that show up via embedded marketing/tracking pixels — never advisor inboxes.
    junk_email_domains: set[str] = field(
        default_factory=lambda: {
            "sentry.io",
            "sentry-cdn.com",
            "sendgrid.net",
            "sendgrid.com",
            "mailchimp.com",
            "list-manage.com",
            "cloudflare.com",
            "cloudflareinsights.com",
            "googletagmanager.com",
            "google-analytics.com",
            "googleapis.com",
            "doubleclick.net",
            "facebook.com",
            "fbcdn.net",
            "twitter.com",
            "linkedin.com",
            "youtube.com",
            "vimeo.com",
            "wistia.com",
            "wixpress.com",
            "wix.com",
            "squarespace.com",
            "wordpress.com",
            "hubspot.com",
            "hs-scripts.com",
            "hsforms.com",
            "intercom.io",
            "zendesk.com",
            "salesforce.com",
            "force.com",
            "amazonaws.com",
            "akamaihd.net",
            "akamai.net",
            "fontawesome.com",
            "jsdelivr.net",
            "bootstrapcdn.com",
            "gstatic.com",
            "example.com",
            "domain.com",
            "yourdomain.com",
            "sentry.wixpress.com",
        }
    )


DEFAULT_SCRAPER = ScraperConfig()


def ensure_dirs() -> None:
    """Create all expected dirs if missing. Safe to call repeatedly."""
    for d in (RAW_DIR, PROCESSED_DIR, ENRICHED_DIR, LOG_DIR, SCRAPE_CACHE_DIR):
        d.mkdir(parents=True, exist_ok=True)


def ensure_fdic_dirs() -> None:
    """Create FDIC pipeline dirs if missing. Safe to call repeatedly.

    The scrape cache (SCRAPE_CACHE_DIR) is shared with the RIA pipeline — it is
    keyed by domain, so banks and advisers never collide.
    """
    for d in (FDIC_RAW_DIR, FDIC_PROCESSED_DIR, FDIC_ENRICHED_DIR, LOG_DIR, SCRAPE_CACHE_DIR):
        d.mkdir(parents=True, exist_ok=True)


def ensure_ncua_dirs() -> None:
    """Create NCUA pipeline dirs if missing. Safe to call repeatedly. The scrape
    cache (keyed by domain) is shared with the other pipelines."""
    for d in (NCUA_RAW_DIR, NCUA_PROCESSED_DIR, NCUA_ENRICHED_DIR, LOG_DIR, SCRAPE_CACHE_DIR):
        d.mkdir(parents=True, exist_ok=True)
