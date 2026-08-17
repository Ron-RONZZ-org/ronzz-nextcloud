# ruff: noqa: RUF002, SIM102, B007, S110
"""Phishing detection — (vendored from lighterbird @ 99baa8ef). feed-based URL lookup + display name spoof check.

Phishing is detected by two orthogonal mechanisms:

1. **Feed-based**: Check all URLs in the email against known phishing
   feeds (OpenPhish, PhishTank, PhishStats).  These are the same feeds
   used by SpamAssassin's ``Phishing`` plugin.  URLs are extracted from
   ``<a href="...">`` tags in HTML bodies and matched against a local
   feed database that is refreshed periodically.

2. **Display-name spoof**: Detect ``From: "Brand" <suspicious@domain>``
   patterns where the display name claims to be a well-known brand but
   the sending domain doesn't match.  Uses a shipped brand table.

3. **User watchlist**: When the user marks an email as fraudulent, the
   sender domain + any detected brand are recorded.  Future emails from
   that domain (or claiming to be that brand from a different domain)
   are flagged immediately with high confidence.

This module does NOT use a Bayesian classifier — phishing detection is
structural/rule-based, not token-based, to avoid polluting the spam
Bayesian model with legitimate brand names.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Shipped brand table — the most-phished brands and their legitimate domains.
# These are used for display-name spoof detection only; feed URLs handle the
# actual phishing URL matching.
_BRAND_TABLE_PATH = Path(__file__).resolve().parent / "phishing_brands.json"

# Regex for extracting <a href> from HTML
_RE_HREF = re.compile(r'<a\s+href="([^"]+)"', re.I)

# Confidence thresholds
_WATCHLIST_THRESHOLD = 0.9
_HEURISTIC_THRESHOLD = 0.5


def _load_brands() -> dict[str, list[str]]:
    """Load the brand domain table.

    Returns:
        Dict mapping brand name (lowercase) → list of legitimate domains.
    """
    defaults: dict[str, list[str]] = {
        "paypal": ["paypal.com", "paypal.de", "paypal.co.uk", "paypal.fr"],
        "amazon": [
            "amazon.com",
            "amazon.co.uk",
            "amazon.de",
            "amazon.fr",
            "amazon.ca",
            "amazon.co.jp",
        ],
        "apple": ["apple.com", "icloud.com"],
        "netflix": ["netflix.com"],
        "microsoft": ["microsoft.com", "outlook.com", "hotmail.com", "live.com"],
        "google": ["google.com", "gmail.com"],
        "facebook": ["facebook.com", "fb.com", "meta.com"],
        "linkedin": ["linkedin.com"],
        "github": ["github.com"],
        "chase": ["chase.com"],
        "wellsfargo": ["wellsfargo.com"],
        "bankofamerica": ["bankofamerica.com"],
        "amex": ["americanexpress.com"],
        "visa": ["visa.com"],
        "mastercard": ["mastercard.com"],
    }
    if _BRAND_TABLE_PATH.exists():
        try:
            with open(_BRAND_TABLE_PATH) as f:
                overrides = json.load(f)
                defaults.update(overrides)
        except Exception as exc:
            logger.warning("Failed to load phishing brands: %s", exc)
    return defaults


def _extract_domain(email_or_url: str) -> str | None:
    """Extract domain from an email address or URL.

    Args:
        email_or_url: ``"user@domain.com"`` or ``"https://domain.com/path"``.

    Returns:
        Domain part (lowercase), or None if unparseable.
    """
    email_or_url = email_or_url.strip()
    if "@" in email_or_url and "://" not in email_or_url:
        # Email address
        return email_or_url.split("@")[-1].lower().strip("> \t")
    # URL
    try:
        parsed = urlparse(email_or_url)
        host = parsed.hostname
        return host.lower() if host else None
    except Exception:
        return None


def _extract_urls(html_body: str | None) -> list[str]:
    """Extract all ``href`` URLs from an HTML body.

    Args:
        html_body: The HTML body of the email.

    Returns:
        List of extracted URLs.
    """
    if not html_body:
        return []
    return _RE_HREF.findall(html_body)


def _parse_from_header(from_addr: str) -> tuple[str, str]:
    """Parse a From header into display name and email address.

    Args:
        from_addr: Raw From header (e.g. ``"Name" <name@dom.com>``).

    Returns:
        Tuple of ``(display_name, email_addr)``.
    """
    from_addr = from_addr.strip()
    # Quoted display name: "Name" <email> or Name <email>
    m = re.match(r'^"([^"]*)"\s*<([^>]+)>$', from_addr)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    # Unquoted display name: Name <email>
    m = re.match(r"^([^<]+)\s*<([^>]+)>$", from_addr)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    # Bare email
    if "@" in from_addr:
        return "", from_addr
    return "", from_addr


class PhishingDetector:
    """Phishing detection — feed lookup + structural heuristics.

    Args:
        db: Database connection for feed and watchlist queries.
    """

    def __init__(self, db: Any) -> None:
        self._db = db
        self._brands: dict[str, list[str]] = _load_brands()

    # ── Public API ───────────────────────────────────────────────────────

    def analyze(
        self,
        from_addr: str,
        subject: str,
        html_body: str | None,
        body: str | None,
        account_email: str | None = None,
    ) -> dict[str, Any]:
        """Analyze a message for phishing indicators.

        Runs feed URL lookup + display-name spoof check + watchlist check.

        Args:
            from_addr: The ``From`` header value.
            subject: Email subject.
            html_body: HTML body (may be None for plain-text only).
            body: Plaintext body (fallback if HTML unavailable).
            account_email: Account email for watchlist lookup.

        Returns:
            Dict with ``is_phishing`` (bool), ``score`` (float 0.0–1.0),
            ``reasons`` (list of str), and ``severity`` (str).
        """
        reasons: list[str] = []
        score = 0.0

        # ── Phase 2: Watchlist check (overrides heuristics) ──────────
        watchlist = self._check_watchlist(from_addr, account_email)
        if watchlist["is_phishing"]:
            return watchlist

        # ── Phase 1: Structural heuristics ───────────────────────────
        # Display-name spoof check
        spoof = self._check_display_name_spoof(from_addr)
        if spoof:
            reasons.append(
                f"Display name says '{spoof['claimed_brand']}' "
                f"but sent from {spoof['actual_domain']}"
            )
            score += 0.4

        # Brand mismatch (brand mentioned in body but sender is different)
        brand = self._check_brand_mismatch(from_addr, subject, body or "")
        if brand and not spoof:
            reasons.append(
                f"Mentions '{brand['claimed_brand']}' "
                f"but sent from {brand['actual_domain']}"
            )
            score += 0.3

        # Feed URL match
        urls = _extract_urls(html_body)
        feed_matches = self._check_feeds(urls)
        if feed_matches:
            n = len(feed_matches)
            reasons.append(
                f"URL matches known phishing feed ({n} link{'s' if n > 1 else ''})"
            )
            score += 0.9  # Feed match is very strong signal

        score = min(score, 1.0)

        return {
            "is_phishing": score >= _HEURISTIC_THRESHOLD,
            "score": round(score, 4),
            "reasons": reasons,
            "severity": self._classify_severity(score),
        }

    def report_fraudulent(
        self,
        from_addr: str,
        subject: str,
        body: str | None,
        account_email: str,
        message_uuid: str | None = None,
    ) -> None:
        """Record user's fraudulent mark in the watchlist.

        Adds the sender domain to the phishing watchlist.  If the email
        mentions a known brand, the brand-domain pair is stored for
        future cross-checks.

        Args:
            from_addr: The ``From`` header value.
            subject: Email subject.
            body: Plaintext body.
            account_email: Account email.
            message_uuid: Message UUID for feedback log.
        """
        sender_domain = _extract_domain(from_addr)
        if not sender_domain:
            return

        now = datetime.now(UTC).isoformat()

        # Add to phishing domains watchlist
        self._db.execute(
            "INSERT OR IGNORE INTO phishing_domains "
            "(domain, brand, reason, reported_at, account_email) "
            "VALUES (?, ?, ?, ?, ?)",
            (sender_domain, None, "user-reported", now, account_email),
        )

        # Detect brand impersonation and store the pair
        spoof = self._check_display_name_spoof(from_addr)
        if spoof:
            self._db.execute(
                "UPDATE phishing_domains SET brand = ? "
                "WHERE domain = ? AND account_email = ?",
                (spoof["claimed_brand"], sender_domain, account_email),
            )

        # Log feedback
        if message_uuid:
            import uuid as _uuid

            self._db.execute(
                "INSERT INTO spam_feedback (uuid, message_uuid, account_email, feedback, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (str(_uuid.uuid4()), message_uuid, account_email, "fraud", now),
            )

    # ── Phase 1: Structural heuristics ─────────────────────────────────

    def _check_display_name_spoof(self, from_addr: str) -> dict | None:
        """Check if the display name claims a brand different from sender domain.

        Returns:
            Dict with ``claimed_brand`` and ``actual_domain``, or None.
        """
        display_name, email_addr = _parse_from_header(from_addr)
        if not display_name or not email_addr:
            return None

        sender_domain = _extract_domain(email_addr)
        if not sender_domain:
            return None

        for brand, domains in self._brands.items():
            if brand.lower() in display_name.lower():
                if not any(self._domain_matches(sender_domain, d) for d in domains):
                    return {
                        "claimed_brand": brand,
                        "actual_domain": sender_domain,
                    }
        return None

    def _check_brand_mismatch(
        self, from_addr: str, subject: str, body: str
    ) -> dict | None:
        """Check if the email body/subject mentions a brand from non-brand sender.

        Returns:
            Dict with ``claimed_brand`` and ``actual_domain``, or None.
        """
        sender_domain = _extract_domain(from_addr)
        if not sender_domain:
            return None

        # Skip if sender domain IS a known brand domain
        for domains in self._brands.values():
            if any(self._domain_matches(sender_domain, d) for d in domains):
                return None

        text = f"{subject} {body}".lower()
        for brand, domains in self._brands.items():
            if brand.lower() in text:
                return {
                    "claimed_brand": brand,
                    "actual_domain": sender_domain,
                }
        return None

    def _check_feeds(self, urls: list[str]) -> list[dict]:
        """Check extracted URLs against the phishing feeds database.

        Args:
            urls: URLs to check.

        Returns:
            List of matching feed entries (url + source).
        """
        if not urls:
            return []

        matches: list[dict] = []
        for url in urls:
            domain = _extract_domain(url)
            if not domain:
                continue
            row = self._db.execute_one(
                "SELECT source FROM phishing_feeds WHERE domain = ?",
                (domain,),
            )
            if row:
                matches.append({"url": url, "source": row["source"]})
        return matches

    def _domain_matches(self, actual: str, canonical: str) -> bool:
        """Check if a domain matches a canonical brand domain (or subdomain)."""
        return actual == canonical or actual.endswith("." + canonical)

    # ── Phase 2: User watchlist ───────────────────────────────────────

    def _check_watchlist(
        self, from_addr: str, account_email: str | None
    ) -> dict[str, Any]:
        """Check the sender domain against the user watchlist.

        Returns:
            Full result dict (is_phishing=True if watchlisted).
        """
        sender_domain = _extract_domain(from_addr)
        if not sender_domain or not account_email:
            return {
                "is_phishing": False,
                "score": 0.0,
                "reasons": [],
                "severity": "none",
            }

        # Direct domain match
        row = self._db.execute_one(
            "SELECT brand FROM phishing_domains WHERE domain = ? AND account_email = ?",
            (sender_domain, account_email),
        )
        if row:
            brand_info = f" impersonating '{row['brand']}'" if row["brand"] else ""
            return {
                "is_phishing": True,
                "score": 1.0,
                "reasons": [
                    f"Previously reported phishing domain{sender_domain}{brand_info}"
                ],
                "severity": "critical",
            }

        return {"is_phishing": False, "score": 0.0, "reasons": [], "severity": "none"}

    # ── Utility ────────────────────────────────────────────────────────

    @staticmethod
    def _classify_severity(score: float) -> str:
        """Map a numeric score to a severity level."""
        if score >= 0.9:
            return "critical"
        if score >= 0.7:
            return "high"
        if score >= 0.5:
            return "medium"
        if score >= 0.3:
            return "low"
        return "none"


# ── Feed update utility ─────────────────────────────────────────────────


class PhishingFeedUpdater:
    """Downloads and refreshes phishing feeds from the internet.

    Downloads feeds from OpenPhish, PhishTank, PhishStats, etc. and
    upserts domains into the ``phishing_feeds`` table.
    """

    FEEDS: list[dict[str, str]] = [
        {
            "name": "openphish",
            "url": "https://openphish.com/feed.txt",
            "format": "plain",
        },
        {
            "name": "phishtank",
            "url": "http://data.phishtank.com/data/online-valid.csv",
            "format": "csv",
        },
        {
            "name": "phishstats",
            "url": "https://phishstats.info/phish_score.csv",
            "format": "csv",
        },
    ]

    def __init__(self, db: Any) -> None:
        self._db = db

    def update_all(self) -> dict[str, int]:
        """Download and process all feeds.

        Returns:
            Dict mapping feed name → number of new domains loaded.
        """
        import httpx

        totals: dict[str, int] = {}
        for feed in self.FEEDS:
            try:
                resp = httpx.get(feed["url"], timeout=30)
                resp.raise_for_status()
                count = self._process_feed(feed["name"], resp.text, feed["format"])
                totals[feed["name"]] = count
                logger.info("[phishing] Feed %s: %d domains", feed["name"], count)
            except Exception as exc:
                logger.warning(
                    "[phishing] Feed %s update failed: %s", feed["name"], exc
                )
                totals[feed["name"]] = 0
        return totals

    def _process_feed(self, name: str, text: str, fmt: str) -> int:
        """Parse feed text and upsert domains into the database.

        Args:
            name: Feed source name.
            text: Raw feed content.
            fmt: ``"plain"`` (one URL per line) or ``"csv"``.

        Returns:
            Number of domains inserted/updated.
        """
        import csv
        from io import StringIO

        domains: set[str] = set()

        if fmt == "csv":
            reader = csv.reader(StringIO(text))
            for row in reader:
                if row and len(row) >= 2:
                    url = row[1].strip()
                    if url and not url.startswith("#"):
                        domain = _extract_domain(url)
                        if domain:
                            domains.add(domain)
        else:
            for line in text.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    domain = _extract_domain(line)
                    if domain:
                        domains.add(domain)

        now = datetime.now(UTC).isoformat()
        count = 0
        for domain in domains:
            try:
                self._db.execute(
                    "INSERT OR REPLACE INTO phishing_feeds (domain, source, last_seen) "
                    "VALUES (?, ?, ?)",
                    (domain, name, now),
                )
                count += 1
            except Exception:
                pass
        return count


__all__ = [
    "PhishingDetector",
    "PhishingFeedUpdater",
    "_extract_domain",
    "_extract_urls",
]
