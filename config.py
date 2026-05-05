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
