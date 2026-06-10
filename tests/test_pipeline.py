"""Two sanity tests covering the parts most likely to silently break:

1. Column-mapping fallback: a column with shifted casing/punctuation
   ("5F.(2).(c)" instead of "5F(2)(c)") still maps to aum_total.
2. Robots.txt blocking: a Disallow: / robots actually skips the firm.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.parse_adv import _build_column_map, parse_adv, PartialSnapshotError  # noqa: E402
from src.scrape_websites import _scrape_one_firm  # noqa: E402
from src.utils import normalize_url  # noqa: E402
import config  # noqa: E402


def test_normalize_url_rejects_malformed():
    """Real Form-ADV junk like 'www.twitter@fi3advisors.com' must not reach the scraper."""
    assert normalize_url("http://www.twitter@fi3advisors.com") is None
    assert normalize_url("www.twitter@fi3advisors.com") is None
    # Other shapes of junk this hardener should also reject
    assert normalize_url("http://localhost") is None       # no dot in host
    assert normalize_url("http://my site.com") is None     # whitespace
    assert normalize_url("http://example\x00.com") is None # control char
    # Sanity: a clean URL still passes through
    assert normalize_url("www.acmewealth.com") == "http://www.acmewealth.com"


def test_column_mapping_tolerates_punctuation_and_case(tmp_path: Path):
    """Headers in the wild come with stray punctuation and shifted case.
    The parser's regex-based fallback should still find every key field."""
    headers = [
        "1A",                   # legal name
        "1B-1 (Primary Business Name)",
        "1D",                   # SEC number
        "1E-1",                 # CRD
        "1F1-Street 1",
        "1F1-City",
        "1F1-State",
        "1F1-Postal Code",
        "1I-1 - Website",
        "5A",
        "5D.(b).(1)",           # HNW client count — punctuation variant
        "5D.(b).(3)",           # HNW AUM dollars — punctuation variant
        "5F.(2).(c)",           # punctuation variant — must still map to aum_total
        "9A.1",
    ]
    cmap = _build_column_map(headers)
    m = cmap.mapping
    assert m["firm_legal_name"] == "1A"
    assert m["crd_number"] == "1E-1"
    assert m["aum_total"] == "5F.(2).(c)", f"got {m.get('aum_total')!r}"
    assert m["hnw_clients"] == "5D.(b).(1)"
    assert m["hnw_aum_dollars"] == "5D.(b).(3)"
    assert m["has_custody"] == "9A.1"
    # 1F1-Street 1 should map to office_street, not office_street2
    assert m["office_street"] == "1F1-Street 1"
    # cco fields and the old percent fields are no longer in the schema
    assert "cco_email" not in m
    assert "cco_first_name" not in m
    assert "pct_hnw_clients" not in m
    assert "pct_institutional" not in m


def test_parse_adv_end_to_end_smoke(tmp_path: Path, monkeypatch):
    """A tiny synthetic xlsx round-trips through parse_adv without crashing
    and produces typed columns we expect."""
    df_in = pd.DataFrame(
        {
            "1A": ["Acme Wealth LLC", "Beta Capital"],
            "1E-1": ["123456", "789012"],
            "1F1-State": ["ny", "ca"],
            "1I-1": ["www.acmewealth.com", None],
            "5A": [25, 8],
            # Item 5D HNW: (b)(1) = client count, (b)(3) = AUM dollars
            "5D-(b)-(1)": ["42", "3"],
            "5D-(b)-(3)": ["   384,347,184.00", "   12,500,000.00"],
            "5D-(a)-(1)": ["563", "120"],
            "5D-(a)-(3)": ["   164,144,017.00", "   45,000,000.00"],
            "5F-(2)-(c)": ["1,200,000,000", "$80,000,000"],
            "9A": ["No", "Yes"],
        }
    )
    xlsx = tmp_path / "tiny.xlsx"
    df_in.to_excel(xlsx, index=False)

    # Redirect outputs into tmp_path so we don't pollute data/processed.
    monkeypatch.setattr(config, "PROCESSED_DIR", tmp_path)
    monkeypatch.setattr(config, "CLEAN_PARQUET", tmp_path / "firms_clean.parquet")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "COLUMN_MAP_LOG", tmp_path / "logs" / "column_mapping.log")

    out = parse_adv(xlsx)
    assert list(out["firm_legal_name"]) == ["Acme Wealth LLC", "Beta Capital"]
    assert out.loc[0, "office_state"] == "NY"
    assert out.loc[0, "website"] == "http://www.acmewealth.com"
    assert "cco_email" not in out.columns
    assert out.loc[0, "aum_total"] == 1_200_000_000
    # New HNW columns: count is Int64-nullable, AUM is float dollars
    assert int(out.loc[0, "hnw_clients"]) == 42
    assert out.loc[0, "hnw_aum_dollars"] == 384_347_184.0
    assert int(out.loc[0, "individual_clients"]) == 563
    assert out.loc[0, "individual_aum_dollars"] == 164_144_017.0
    assert out.loc[0, "has_custody"] is False or out.loc[0, "has_custody"] == False  # noqa: E712


def test_parse_adv_rejects_partial_snapshot(tmp_path: Path, monkeypatch):
    """A reduced month-start file (identity columns but no Item-5 financials)
    must raise PartialSnapshotError and write NOTHING, so it can't clobber the
    good processed outputs."""
    # Identity/address/website present; aum/hnw/employee columns absent.
    df_in = pd.DataFrame(
        {
            "Legal Name": ["Acme Wealth LLC", "Beta Capital"],
            "Organization CRD#": ["123456", "789012"],
            "Main Office State": ["NY", "CA"],
            "Website Address": ["www.acmewealth.com", "www.betacap.com"],
        }
    )
    xlsx = tmp_path / "partial.xlsx"
    df_in.to_excel(xlsx, index=False)

    out_parquet = tmp_path / "firms_clean.parquet"
    monkeypatch.setattr(config, "PROCESSED_DIR", tmp_path)
    monkeypatch.setattr(config, "CLEAN_PARQUET", out_parquet)
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "COLUMN_MAP_LOG", tmp_path / "logs" / "column_mapping.log")

    with pytest.raises(PartialSnapshotError) as ei:
        parse_adv(xlsx)
    # Message names a missing required column and does not write the parquet.
    assert "aum_total" in str(ei.value)
    assert not out_parquet.exists(), "partial snapshot must not write firms_clean.parquet"


class _StubResponse:
    def __init__(self, status: int, text: str):
        self.status_code = status
        self.text = text


class _StubClient:
    """Pretends to be httpx.AsyncClient. Returns a Disallow-all robots.txt."""

    def __init__(self):
        self.calls: list[str] = []

    async def get(self, url: str, **_kw):
        self.calls.append(url)
        if url.endswith("/robots.txt"):
            return _StubResponse(200, "User-agent: *\nDisallow: /\n")
        # We should never get here in this test — robots blocks every path.
        return _StubResponse(200, "<html><body>oops</body></html>")


class _StubResponseRedirect:
    def __init__(self, status: int, text: str, url: str):
        self.status_code = status
        self.text = text
        self.url = url


class _RedirectingClient:
    """Pretends to be httpx.AsyncClient.

    - Allows everything in robots.txt for both old and new domains.
    - For any non-robots GET on acme-old.com, returns 200 but reports the
      response URL as if redirected to acme-new.com — same as httpx would
      with follow_redirects=True after a 302.
    """

    def __init__(self, html: str):
        self.html = html
        self.calls: list[str] = []

    async def get(self, url: str, **_kw):
        self.calls.append(url)
        if url.endswith("/robots.txt"):
            return _StubResponseRedirect(200, "User-agent: *\nAllow: /\n", url)
        # Pretend any acme-old request landed on acme-new.com after a 302.
        if "acme-old.com" in url:
            final_url = url.replace("acme-old.com", "acme-new.com")
            return _StubResponseRedirect(200, self.html, final_url)
        return _StubResponseRedirect(200, self.html, url)


def test_redirect_caches_and_validates_against_final_host(tmp_path, monkeypatch):
    """The big bug we just fixed:
       - Cache must land under the final host (acme-new.com)
       - Emails on the final host pass; emails on unrelated domains fail
    """
    # Isolate scrape cache + redirect meta to tmp.
    monkeypatch.setattr(config, "SCRAPE_CACHE_DIR", tmp_path / "cache")

    # Page hosts:
    #   - one personal address on the FINAL host (must be kept)
    #   - one personal address on an UNRELATED host (must be rejected)
    #   - one mailto on the unrelated host (must be rejected too)
    #   - text-regex address on yet another unrelated host (must be rejected)
    html = """
    <html><body>
      <a href="mailto:alice@acme-new.com">Alice</a>
      <a href="mailto:info@unrelated.com">Spam</a>
      <p>Reach Bob at bob@somewhere-else.com</p>
    </body></html>
    """
    client = _RedirectingClient(html)
    sem = asyncio.Semaphore(1)
    cfg = config.ScraperConfig(
        per_domain_delay_seconds=0.0,
        request_timeout_seconds=1.0,
        max_retries=1,
        cache_pages=True,
    )
    firm = {"website": "https://acme-old.com", "crd_number": "555"}
    res = asyncio.run(_scrape_one_firm(firm, client, sem, cfg))

    # Cache landed under acme-new.com, not acme-old.com
    cache_root = tmp_path / "cache"
    new_dir = cache_root / "acme-new.com"
    old_dir = cache_root / "acme-old.com"
    assert new_dir.exists() and any(new_dir.glob("*.html")), \
        f"expected cache under acme-new.com, got: {list(cache_root.iterdir())}"
    # The acme-old.com dir may exist only to hold the _redirect.json sidecar;
    # if so, it must NOT contain any cached HTML pages.
    if old_dir.exists():
        assert not any(old_dir.glob("*.html")), \
            f"cache should NOT be written under original host: {list(old_dir.iterdir())}"

    # Sidecar redirect map written
    assert (old_dir / "_redirect.json").exists()

    # Emails: acme-new.com kept, others rejected
    emails = {c.email for c in res.contacts}
    assert "alice@acme-new.com" in emails, f"got {emails}"
    assert "info@unrelated.com" not in emails
    assert "bob@somewhere-else.com" not in emails
    # final_domain tracked
    assert res.final_domain == "acme-new.com"


def test_robots_disallow_blocks_all_paths():
    """If robots.txt forbids everything for our UA, no content URL is fetched."""
    client = _StubClient()
    sem = asyncio.Semaphore(1)
    cfg = config.ScraperConfig(
        per_domain_delay_seconds=0.0,
        request_timeout_seconds=1.0,
        max_retries=1,
        cache_pages=False,
    )
    firm = {"website": "http://example-ria.test", "crd_number": "999999"}
    res = asyncio.run(_scrape_one_firm(firm, client, sem, cfg))

    # The only URL fetched should be robots.txt; no team pages attempted.
    content_calls = [c for c in client.calls if not c.endswith("/robots.txt")]
    assert content_calls == [], f"unexpected fetches: {content_calls}"
    assert res.contacts == []
    assert res.fetched_paths == []


# ----- _fetch_robots: redirect / 404 / 403-then-200 / disallow-all regression


from src.scrape_websites import (  # noqa: E402
    _fetch_robots,
    _clean_email_candidate,
    _is_valid_email,
    _looks_like_person_name,
    _looks_like_title,
    extract_contacts_from_html,
)


class _RobotsResponse:
    def __init__(self, status: int, text: str = ""):
        self.status_code = status
        self.text = text


class _RobotsScriptedClient:
    """Async-client stub: maps {(scheme, domain): _RobotsResponse|exception class}."""

    def __init__(self, mapping):
        self.mapping = mapping  # url -> response or exception class
        self.calls: list[str] = []

    async def get(self, url, **_kw):
        self.calls.append(url)
        item = self.mapping.get(url)
        if item is None:
            return _RobotsResponse(404, "")
        if isinstance(item, type) and issubclass(item, BaseException):
            raise item("stub")
        return item


def test_fetch_robots_404_allows_all():
    """No robots.txt anywhere → can_fetch True for any path (regression for
    the unparsed-RobotFileParser bug)."""
    client = _RobotsScriptedClient({
        "https://example.com/robots.txt": _RobotsResponse(404),
        "http://example.com/robots.txt":  _RobotsResponse(404),
    })
    rp = asyncio.run(_fetch_robots(client, "example.com"))
    assert rp.can_fetch(config.USER_AGENT, "https://example.com/team")
    assert rp.can_fetch(config.USER_AGENT, "https://example.com/about-us")
    assert rp.can_fetch(config.USER_AGENT, "https://example.com/")


def test_fetch_robots_redirect_honored():
    """Sites commonly 301 bare → www on /robots.txt. With follow_redirects=True
    the client lands on the final 200 response and we honor those rules."""
    # Stub returns a permissive Squarespace-style robots.txt — the kind that
    # was being incorrectly flagged as "robots disallow" in the prior run.
    body = (
        "User-agent: GPTBot\n"
        "Disallow: /\n"
        "User-agent: *\n"
        "Disallow: /api/\n"
    )
    client = _RobotsScriptedClient({
        "https://example.com/robots.txt": _RobotsResponse(200, body),
    })
    rp = asyncio.run(_fetch_robots(client, "example.com"))
    assert rp.can_fetch(config.USER_AGENT, "https://example.com/team")
    assert rp.can_fetch(config.USER_AGENT, "https://example.com/contact")
    assert not rp.can_fetch(config.USER_AGENT, "https://example.com/api/foo")


def test_fetch_robots_https_403_falls_back_to_http_200():
    """An https 403 must NOT short-circuit the http fallback."""
    permissive = "User-agent: *\nDisallow:\n"
    client = _RobotsScriptedClient({
        "https://example.com/robots.txt": _RobotsResponse(403),
        "http://example.com/robots.txt":  _RobotsResponse(200, permissive),
    })
    rp = asyncio.run(_fetch_robots(client, "example.com"))
    assert rp.can_fetch(config.USER_AGENT, "https://example.com/team")
    assert rp.can_fetch(config.USER_AGENT, "https://example.com/")


def test_fetch_robots_genuine_disallow_all_blocks():
    """Regression: User-agent: * Disallow: / must still block."""
    body = "User-agent: *\nDisallow: /\n"
    client = _RobotsScriptedClient({
        "https://example.com/robots.txt": _RobotsResponse(200, body),
    })
    rp = asyncio.run(_fetch_robots(client, "example.com"))
    assert not rp.can_fetch(config.USER_AGENT, "https://example.com/team")
    assert not rp.can_fetch(config.USER_AGENT, "https://example.com/")


def test_fetch_robots_double_403_treated_as_block():
    """If BOTH schemes return 401/403, conservatively treat as Disallow: /."""
    client = _RobotsScriptedClient({
        "https://example.com/robots.txt": _RobotsResponse(403),
        "http://example.com/robots.txt":  _RobotsResponse(401),
    })
    rp = asyncio.run(_fetch_robots(client, "example.com"))
    assert not rp.can_fetch(config.USER_AGENT, "https://example.com/team")


def test_fetch_robots_transport_errors_both_schemes_allow_all():
    """Both schemes raising → no information → allow all (don't blanket-block)."""
    client = _RobotsScriptedClient({
        "https://example.com/robots.txt": ConnectionError,
        "http://example.com/robots.txt":  ConnectionError,
    })
    rp = asyncio.run(_fetch_robots(client, "example.com"))
    assert rp.can_fetch(config.USER_AGENT, "https://example.com/team")


# ----- Email hygiene


def test_clean_email_strips_url_encoded_space():
    assert _clean_email_candidate("%20jeff@example.com") == "jeff@example.com"
    assert _clean_email_candidate("  alice@example.com  ") == "alice@example.com"
    assert _clean_email_candidate("MAILTO:alice@EXAMPLE.com".split(":", 1)[1]) == "alice@example.com"


def test_is_valid_email_rejects_pixel_locals():
    assert not _is_valid_email("img1234@cdn.example.com")
    assert not _is_valid_email("track.open@example.com")
    assert not _is_valid_email("pixel@example.com")
    assert not _is_valid_email("wf-clickthrough@example.com")
    assert not _is_valid_email("mailbox+noreply@example.com")
    assert not _is_valid_email("mailbox+bounce@example.com")
    # Negatives: leading punctuation / percent / no host dot
    assert not _is_valid_email("%20jane@example.com")
    assert not _is_valid_email("jane@nodot")
    # Positives: ordinary addresses
    assert _is_valid_email("jane.doe@example.com")
    assert _is_valid_email("jdoe+legit@example.co.uk")


def test_extract_contacts_drops_url_encoded_prefix():
    """End-to-end: a mailto with %20 prefix should still produce a clean email."""
    html = '<a href="mailto:%20jane@firm.com">Jane</a>'
    contacts = extract_contacts_from_html(html, "firm.com", "test", firm_legal_name="Firm LLC")
    assert len(contacts) == 1
    assert contacts[0].email == "jane@firm.com"


# ----- Name / title quality filter


def test_name_filter_accepts_real_names():
    # The spec is strict: at least one token must contain ADJACENT vowels (or
    # the string must use a "Last, First" comma form). Some real names that
    # lack both signals (e.g. "Hayden Porter") will be rejected — accepted
    # collateral, since "null is better than garbage".
    assert _looks_like_person_name("Bruce Reeder")        # Reeder: ee
    assert _looks_like_person_name("Joseph Mansoor")      # Mansoor: oo
    assert _looks_like_person_name("Connor Augusta")      # Augusta: au
    assert _looks_like_person_name("Brooke Gais")         # Brooke: oo
    assert _looks_like_person_name("Smith, John")         # comma form


def test_name_filter_rejects_nav_chrome():
    # Page chrome / CTAs / address fragments observed in the prior run
    assert not _looks_like_person_name("Connect With")
    assert not _looks_like_person_name("Quick Links")
    assert not _looks_like_person_name("Our Locations")
    assert not _looks_like_person_name("Schedule Your")
    assert not _looks_like_person_name("Customer Service")
    assert not _looks_like_person_name("Norristown Road")
    assert not _looks_like_person_name("Cityplace Drive")
    assert not _looks_like_person_name("Old Kingston")  # nav stoplist + no vowel pair
    assert not _looks_like_person_name("Contact Us")
    # Token-count bounds
    assert not _looks_like_person_name("OnlyOneToken")
    assert not _looks_like_person_name("Way Too Many Tokens In This Phrase Probably")
    # Empty / non-name
    assert not _looks_like_person_name("")
    assert not _looks_like_person_name(None)


def test_name_filter_rejects_firm_name_echo():
    assert not _looks_like_person_name("Ironvine Capital", firm_legal_name="IRONVINE CAPITAL PARTNERS, LLC")
    assert not _looks_like_person_name(
        "Obermeyer Wealth", firm_legal_name="OBERMEYER WOOD INVESTMENT COUNSEL, LLLP"
    )
    # But a real name at the same firm should still pass
    assert _looks_like_person_name("Brooke Gais", firm_legal_name="OBERMEYER WOOD INVESTMENT COUNSEL, LLLP")


def test_title_filter():
    assert _looks_like_title("Chief Compliance Officer")
    assert _looks_like_title("Senior Wealth Advisor")
    assert _looks_like_title("Founder, Principal")
    assert _looks_like_title("Vice President | Wealth Advisory")
    assert not _looks_like_title("Connect With Us")
    assert not _looks_like_title("Norristown Road")
    assert not _looks_like_title("206.533.0525 info@…")
    assert not _looks_like_title("")
    assert not _looks_like_title(None)


# ----- Combined output: every targeted firm is kept (blank email + reason)


from src.scrape_websites import (  # noqa: E402
    _flatten_results,
    _missing_email_status,
    Contact,
    FirmScrapeResult,
)


def test_missing_email_status_codes():
    # No result at all (e.g. firm sliced out by --limit) → not_scraped
    assert _missing_email_status(None) == "not_scraped"
    # An explicit skip reason is surfaced verbatim
    assert _missing_email_status(FirmScrapeResult(crd_number="1", domain=None,
                                                  skipped_reason="no_website")) == "no_website"
    assert _missing_email_status(FirmScrapeResult(crd_number="2", domain="x.com",
                                                  skipped_reason="robots")) == "robots"
    # Pages fetched but nothing usable found → no_email_found
    res = FirmScrapeResult(crd_number="3", domain="x.com", fetched_paths=["/team"])
    assert _missing_email_status(res) == "no_email_found"


def test_flatten_keeps_every_firm_with_blank_email_and_reason():
    """The combined file must contain a row for EVERY targeted firm:
    contacts where we found them, otherwise a blank email + reason code."""
    firms = pd.DataFrame(
        [
            {"_key": "111", "firm_legal_name": "Has Contact LLC", "crd_number": 111,
             "office_state": "NY", "aum_total": 5e8, "website": "http://a.com", "match_score": 0.9},
            {"_key": "222", "firm_legal_name": "No Email LLC", "crd_number": 222,
             "office_state": "CA", "aum_total": 4e8, "website": "http://b.com", "match_score": 0.8},
            {"_key": "333", "firm_legal_name": "No Website LLC", "crd_number": 333,
             "office_state": "TX", "aum_total": 3e8, "website": "http://c.com", "match_score": 0.7},
        ]
    )
    results = {
        "111": FirmScrapeResult(
            crd_number="111", domain="a.com",
            contacts=[Contact(name="Jane Doe", title="Founder",
                              email="jane@a.com", source="scraped_mailto")],
        ),
        # 222: pages fetched, no usable email
        "222": FirmScrapeResult(crd_number="222", domain="b.com", fetched_paths=["/team"]),
        # 333: an explicit skip reason
        "333": FirmScrapeResult(crd_number="333", domain="c.com", skipped_reason="all_failed"),
    }

    rows = _flatten_results(firms, results)
    by_crd = {r["crd_number"]: r for r in rows}

    # Every firm is present exactly once (none have multiple contacts here)
    assert len(rows) == 3
    assert set(by_crd) == {111, 222, 333}

    # Firm with a contact: real email + scraped source
    assert by_crd[111]["contact_email"] == "jane@a.com"
    assert by_crd[111]["email_source"] == "scraped_mailto"
    assert by_crd[111]["contact_name"] == "Jane Doe"

    # Every firm column is carried through (wide, self-contained sheet) and the
    # internal _key is dropped.
    assert by_crd[111]["office_state"] == "NY"
    assert by_crd[111]["aum_total"] == 5e8
    assert by_crd[111]["match_score"] == 0.9
    assert "_key" not in by_crd[111]

    # Firms without a contact: blank email (None, not a sentinel) + reason code
    assert by_crd[222]["contact_email"] is None
    assert by_crd[222]["email_source"] == "no_email_found"
    assert by_crd[222]["contact_name"] is None
    assert by_crd[333]["contact_email"] is None
    assert by_crd[333]["email_source"] == "all_failed"


# ----- Primary-contact cascade (CFO -> COO -> ... -> any contact)


from src.scrape_primary_contact import (  # noqa: E402
    candidates_from_html,
    match_email_by_name,
    _best,
    _fallback_candidate,
    _title_level,
    CASCADE,
)


def test_title_cascade_order_and_patterns():
    assert _title_level("Chief Financial Officer") == 0
    assert _title_level("Partner & CFO") == 0
    assert _title_level("Chief Operating Officer") == 1
    assert _title_level("COO") == 1
    assert _title_level("Director of Finance") == 2
    assert _title_level("Finance Director") == 2
    assert _title_level("Controller") == 3
    assert _title_level("Comptroller") == 3
    assert _title_level("Managing Partner") == 4
    # Non-cascade titles don't match at all
    assert _title_level("Chief Investment Officer") is None
    assert _title_level("Senior Wealth Advisor") is None
    # GDPR boilerplate must not look like a Controller
    assert _title_level("acts as the data controller for this site") is None
    # lowercase 'coo'/'cfo' in prose must not match the acronym patterns
    assert _title_level("babies coo softly") is None


def test_candidates_from_html_finds_cfo_with_email():
    html = """
    <html><body>
      <div class="team-card">
        <h3>Maureen Brooks</h3>
        <p class="role">Chief Financial Officer</p>
        <a href="mailto:maureen.brooks@acmewealth.com">Email</a>
      </div>
      <div class="team-card">
        <h3>Doug Peterson</h3>
        <p class="role">Chief Operating Officer</p>
        <a href="mailto:doug.peterson@acmewealth.com">Email</a>
      </div>
    </body></html>
    """
    cands = candidates_from_html(html, "acmewealth.com", "/team", "ACME WEALTH LLC")
    best = _best(cands)
    assert best is not None
    assert best.level == 0  # CFO outranks COO on the same page
    assert best.name == "Maureen Brooks"
    assert best.email == "maureen.brooks@acmewealth.com"
    assert "Chief Financial Officer" in best.title


def test_candidates_cascade_falls_to_coo_when_no_cfo():
    html = """
    <html><body>
      <div><h3>Doug Peterson</h3><p>Chief Operating Officer</p></div>
      <div><h3>Sarah Woods</h3><p>Managing Partner</p></div>
    </body></html>
    """
    cands = candidates_from_html(html, "acmewealth.com", "/team", "ACME WEALTH LLC")
    best = _best(cands)
    assert best is not None
    assert best.level == 1
    assert best.name == "Doug Peterson"


def test_match_email_by_name_patterns():
    emails = ["maureen.brooks@firm.com", "dpeterson@firm.com", "info@firm.com"]
    assert match_email_by_name("Maureen Brooks", emails) == "maureen.brooks@firm.com"
    assert match_email_by_name("Doug Peterson", emails) == "dpeterson@firm.com"
    # O'Brien-style punctuation is normalized away
    assert match_email_by_name("Pat O'Toole", ["potoole@firm.com"]) == "potoole@firm.com"
    # No plausible match -> None (info@ is never matched by name)
    assert match_email_by_name("Jane Quill", emails) is None
    assert match_email_by_name(None, emails) is None


def test_fallback_prefers_personal_email_with_name():
    existing = [
        {"contact_name": None, "contact_title": None, "contact_email": "info@firm.com"},
        {"contact_name": "Bruce Reeder",
         "contact_title": "Bruce Reeder Senior Wealth Advisor View Bio",
         "contact_email": "bruce.reeder@firm.com"},
    ]
    c = _fallback_candidate(existing, harvested=[])
    assert c is not None
    assert c.level == len(CASCADE)  # fallback level
    assert c.name == "Bruce Reeder"
    assert c.email == "bruce.reeder@firm.com"
    # Title cleaned: leading name + trailing "View Bio" stripped
    assert c.title == "Senior Wealth Advisor"


def test_fallback_uses_low_value_email_as_last_resort():
    existing = [{"contact_name": None, "contact_title": None, "contact_email": "info@firm.com"}]
    c = _fallback_candidate(existing, harvested=[])
    assert c is not None
    assert c.email == "info@firm.com"
