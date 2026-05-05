"""Shared helpers: logging setup, simple URL/domain helpers."""
from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlparse

# Windows consoles default to cp1252 and choke on the unicode arrows / ≈ used
# throughout the pipeline. Force utf-8 once on import, best-effort.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

_LOGGERS_CONFIGURED: set[str] = set()


def get_logger(name: str, log_file: Path | None = None, level: int = logging.INFO) -> logging.Logger:
    """Get a logger that writes to console and (optionally) a rotating file.

    Idempotent: calling twice with the same name does not duplicate handlers.
    """
    logger = logging.getLogger(name)
    if name in _LOGGERS_CONFIGURED:
        return logger

    logger.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    logger.propagate = False
    _LOGGERS_CONFIGURED.add(name)
    return logger


_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def normalize_url(url) -> str | None:
    """Normalize a website value pulled from Form ADV. Returns None if unusable.

    Tolerant of pd.NA / NaN / None / non-strings — those become None.
    Rejects malformed URLs (embedded '@', whitespace, dotless host, control chars)
    rather than letting them flow downstream into the scraper.
    """
    if url is None:
        return None
    if not isinstance(url, str):
        try:
            import pandas as pd

            if pd.isna(url):
                return None
        except Exception:
            pass
        return None
    u = url.strip()
    if not u or u.lower() in {"n/a", "none", "na", "-"}:
        return None
    # Reject obvious junk before parsing
    if _CONTROL_CHARS.search(u):
        return None
    if " " in u or "\t" in u:
        return None
    if not re.match(r"^https?://", u, re.I):
        u = "http://" + u
    parsed = urlparse(u)
    netloc = parsed.netloc
    if not netloc:
        return None
    if "@" in netloc:
        # e.g. firms that fat-fingered "www.twitter@fi3advisors.com" into Form ADV.
        return None
    if any(c.isspace() for c in netloc):
        return None
    host = netloc.split(":")[0]  # ignore port for the dot check
    if "." not in host:
        return None
    return f"{parsed.scheme}://{netloc.lower()}"


def domain_of(url: str) -> str | None:
    """Return the registrable hostname (lowercased, no port, no leading 'www.')."""
    if not url:
        return None
    if not re.match(r"^https?://", url, re.I):
        url = "http://" + url
    host = urlparse(url).netloc.lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host or None


def safe_filename(name: str) -> str:
    """Sanitize a string for use as a filename component."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:120]
