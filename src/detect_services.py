"""Service-offering detection from already-cached firm pages (no network).

For each targeted firm we read the HTML we cached during the scrape (homepage +
about/team/contact pages), strip it to visible text, and look for evidence of a
set of service offerings. Output per service:
    1     = positive evidence found in the firm's pages
    0     = pages were read but no evidence found
    blank = no readable page on file (can't tell)

Keyword/phrase matching is deliberately conservative (multi-word phrases where a
bare word would be too noisy, e.g. 'tax planning' not 'tax'). It is a heuristic:
homepages/about pages, not dedicated /services pages, so treat 0 as "no evidence
here" rather than a hard "they definitely don't." A second sweep tallies other
recurring service terms so we can decide which to promote to real categories.
"""
from __future__ import annotations

import re
from collections import Counter

import pandas as pd
from selectolax.parser import HTMLParser

import config
from src.utils import domain_of, safe_filename
from src.scrape_websites import _read_redirect_meta

# --- The four requested categories -------------------------------------------
# Patterns run against lowercased visible text. Any one match -> 1.
SERVICE_PATTERNS: dict[str, list[re.Pattern]] = {
    "brokerage": [re.compile(p) for p in (
        r"\bbroker[- ]?dealer\b",
        r"\bbrokerage\b",
        r"\bregistered representative",
        r"\bmember finra\b", r"\bfinra member\b",
        r"securities offered through",
    )],
    "insurance": [re.compile(p) for p in (
        r"\binsurance\b",
        r"\bannuit(?:y|ies)\b",
        r"long[- ]term care",
    )],
    "funeral": [re.compile(p) for p in (
        r"\bfuneral\b", r"\bburial\b", r"\bfinal expense\b",
        r"\bpre[- ]?need\b", r"\bcremation\b",
    )],
    "tax": [re.compile(p) for p in (
        r"\btax planning\b", r"\btax preparation\b", r"\btax services\b",
        r"\btax advice\b", r"\btax strateg", r"\btax return",
        r"\btax minimi", r"\bincome tax\b", r"\btax compliance\b",
        r"\btax management\b", r"\btax consulting\b",
    )],
    # --- added per user selection (bundles split into distinct services) -----
    "estate_planning": [re.compile(r"estate planning")],
    "retirement_planning": [re.compile(r"retirement planning")],
    "401k": [re.compile(p) for p in (
        r"401\(?k\)?", r"403\(?b\)?",
        r"retirement plan (?:consulting|services|design|administration)",
        r"pension plan", r"\bplan sponsor",
    )],
    "accounting": [re.compile(r"\baccounting\b|bookkeeping")],
    "family_office": [re.compile(r"family office|\bconcierge\b")],
    "alternatives": [re.compile(
        r"alternative investment|private equity|hedge fund|private credit"
    )],
}

# --- Discovery sweep: other recurring services to consider adding ------------
OTHER_SERVICE_CANDIDATES: dict[str, re.Pattern] = {label: re.compile(p) for label, p in {
    "financial planning":            r"financial planning",
    "wealth management":             r"wealth management",
    "investment management":         r"investment management",
    "trust services":                r"\btrust (?:services|administration|company)\b",
    "charitable / philanthropic":    r"philanthrop|charitable (?:giving|planning)",
    "education / college planning":  r"college planning|education (?:planning|funding)",
    "business succession / exit":    r"succession planning|exit planning|business planning",
    "real estate":                   r"real estate",
    "mortgage / lending":            r"\bmortgage\b|\blending\b",
    "banking / cash management":     r"\bbanking\b|cash management",
    "legal / attorney":              r"legal services|\battorney\b|\blaw firm\b",
    "divorce / QDRO":                r"\bdivorce\b|\bqdro\b",
    "medicare / social security":    r"\bmedicare\b|social security",
}.items()}

SVC_COLS = [f"svc_{c}" for c in SERVICE_PATTERNS]


def _cache_dirs_for(website) -> list:
    if not isinstance(website, str):
        return []
    host = domain_of(website)
    if not host:
        return []
    dirs = []
    d = config.SCRAPE_CACHE_DIR / safe_filename(host)
    if d.is_dir():
        dirs.append(d)
    final = _read_redirect_meta(host)
    if final:
        fd = config.SCRAPE_CACHE_DIR / safe_filename(final)
        if fd.is_dir() and fd not in dirs:
            dirs.append(fd)
    return dirs


def _firm_text(website) -> str | None:
    """Concatenated, lowercased visible text of a firm's cached pages, or None."""
    texts: list[str] = []
    for d in _cache_dirs_for(website):
        for html_file in d.glob("*.html"):
            try:
                html = html_file.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            tree = HTMLParser(html)
            for node in tree.css("script, style, noscript"):
                try:
                    node.decompose()
                except Exception:
                    pass
            root = tree.body or tree
            txt = root.text(separator=" ", strip=True) if root else ""
            if txt:
                texts.append(txt)
    if not texts:
        return None
    return re.sub(r"\s+", " ", " ".join(texts)).lower()


def _detect(text: str) -> dict[str, int]:
    return {c: (1 if any(p.search(text) for p in pats) else 0)
            for c, pats in SERVICE_PATTERNS.items()}


def run(master_path=None) -> None:
    master_path = master_path or config.latest_ria_master()
    firms = pd.read_csv(config.TARGETED_CSV)

    flag_rows: list[dict] = []
    other_counts: Counter = Counter()
    other_examples: dict[str, list[str]] = {}
    no_text = 0

    for _, r in firms.iterrows():
        text = _firm_text(r["website"])
        rec = {"crd_number": r["crd_number"]}
        if text is None:
            no_text += 1
            for c in SERVICE_PATTERNS:
                rec[f"svc_{c}"] = pd.NA
        else:
            for c, v in _detect(text).items():
                rec[f"svc_{c}"] = v
            for label, pat in OTHER_SERVICE_CANDIDATES.items():
                if pat.search(text):
                    other_counts[label] += 1
                    if len(other_examples.setdefault(label, [])) < 3:
                        other_examples[label].append(str(r["firm_legal_name"]))
        flag_rows.append(rec)

    flags = pd.DataFrame(flag_rows)

    # --- merge onto the master (firm-level flags repeat across contact rows) ---
    master = pd.read_csv(master_path)
    master = master.drop(columns=[c for c in SVC_COLS if c in master.columns])
    master = master.merge(flags, on="crd_number", how="left")
    for c in SVC_COLS:
        master[c] = master[c].astype("Int64")
    # place the service columns just before match_score
    cols = [c for c in master.columns if c not in SVC_COLS]
    insert_at = cols.index("match_score") if "match_score" in cols else len(cols)
    cols = cols[:insert_at] + SVC_COLS + cols[insert_at:]
    master = master[cols]
    master.to_csv(master_path, index=False)

    # --- report ---------------------------------------------------------------
    n = len(firms)
    print(f"\n=== Service detection over {n:,} targeted firms "
          f"({n - no_text:,} with readable pages, {no_text:,} blank) ===\n")
    print(f"{'service':<12}{'offer (1)':>10}{'no e/o (0)':>12}{'blank':>8}{'% of read':>11}")
    for c in SERVICE_PATTERNS:
        col = flags[f"svc_{c}"]
        ones = int((col == 1).sum())
        zeros = int((col == 0).sum())
        blanks = int(col.isna().sum())
        pct = 100 * ones / (ones + zeros) if (ones + zeros) else 0
        print(f"{c:<12}{ones:>10,}{zeros:>12,}{blanks:>8,}{pct:>10.1f}%")

    print("\n=== Other recurring services (candidates to add) — firm counts ===")
    read = n - no_text
    for label, cnt in other_counts.most_common():
        ex = ", ".join(other_examples.get(label, [])[:2])
        print(f"  {label:<32}{cnt:>6,}  ({100*cnt/read:4.1f}%)   e.g. {ex}")

    print(f"\nWrote {master_path} (added columns: {', '.join(SVC_COLS)})")


if __name__ == "__main__":
    run()
