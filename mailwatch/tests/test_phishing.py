"""Tests for phishing detection — feed lookup, display-name spoof, watchlist."""

from __future__ import annotations

from pathlib import Path

import pytest

from mailwatch.email.db import get_db
from mailwatch.email.filters.phishing import (
    PhishingDetector,
    PhishingFeedUpdater,
    _extract_domain,
    _extract_urls,
    _parse_from_header,
)


@pytest.fixture
def db(tmp_path: Path):
    return get_db(tmp_path / "email.db")


class TestExtractDomain:
    def test_email_address(self):
        assert _extract_domain("user@gmail.com") == "gmail.com"

    def test_email_with_name(self):
        assert _extract_domain("Name <user@paypal.com>") == "paypal.com"

    def test_url(self):
        assert _extract_domain("https://evil.com/phish") == "evil.com"

    def test_url_with_path(self):
        assert _extract_domain("http://192.168.1.1/login.php") == "192.168.1.1"

    def test_invalid(self):
        assert _extract_domain("") is None


class TestExtractUrls:
    def test_basic_href(self):
        urls = _extract_urls('<a href="http://evil.com">click</a>')
        assert urls == ["http://evil.com"]

    def test_multiple_links(self):
        html = '<a href="http://a.com">A</a> <a href="https://b.com">B</a>'
        urls = _extract_urls(html)
        assert len(urls) == 2

    def test_empty_html(self):
        assert _extract_urls(None) == []
        assert _extract_urls("") == []


class TestParseFromHeader:
    def test_name_and_email(self):
        name, email = _parse_from_header('"PayPal" <service@paypal.com>')
        assert name == "PayPal"
        assert email == "service@paypal.com"

    def test_bare_email(self):
        name, email = _parse_from_header("spammer@evil.com")
        assert name == ""
        assert email == "spammer@evil.com"

    def test_name_without_quotes(self):
        name, email = _parse_from_header("Support <support@example.com>")
        assert name == "Support"
        assert email == "support@example.com"


class TestPhishingDetector:
    def test_display_name_spoof_detected(self, db):
        """Display name says PayPal but domain is not paypal.com."""
        detector = PhishingDetector(db)
        result = detector._check_display_name_spoof(
            '"PayPal Support" <phisher@gmail.com>'
        )
        assert result is not None
        assert result["claimed_brand"] == "paypal"
        assert result["actual_domain"] == "gmail.com"

    def test_display_name_spoof_legitimate(self, db):
        """Display name says PayPal and domain IS paypal.com."""
        detector = PhishingDetector(db)
        result = detector._check_display_name_spoof('"PayPal" <service@paypal.com>')
        assert result is None

    def test_analyze_spoofed_display_name(self, db):
        """Full analysis should flag spoofed display name.

        Display name says "PayPal" but domain is gmail.com.
        With a matching phishing feed URL, the score crosses threshold.
        """
        # Seed a phishing feed entry
        from datetime import UTC, datetime

        db.execute(
            "INSERT INTO phishing_feeds (domain, source, last_seen) VALUES (?, ?, ?)",
            ("phish-paypal.com", "openphish", datetime.now(UTC).isoformat()),
        )

        detector = PhishingDetector(db)
        result = detector.analyze(
            from_addr='"PayPal Security" <phisher@gmail.com>',
            subject="Your PayPal account is limited",
            html_body='<a href="http://phish-paypal.com/login">Login here</a>',
            body="Please verify your PayPal account information",
            account_email="user@test.com",
        )
        # Display name spoof: 0.4 + Feed URL match: 0.9 → capped at 1.0
        assert result["is_phishing"] is True
        assert result["score"] >= 0.9

    def test_analyze_legitimate_email(self, db):
        """Legitimate email should not be flagged."""
        detector = PhishingDetector(db)
        result = detector.analyze(
            from_addr='"John" <john@company.com>',
            subject="Meeting tomorrow",
            html_body=None,
            body="Let's sync up",
            account_email="user@test.com",
        )
        assert result["is_phishing"] is False

    def test_analyze_with_phishing_url(self, db):
        """URL matching a phishing feed domain should be flagged."""
        # Seed a phishing feed entry
        from datetime import UTC, datetime

        db.execute(
            "INSERT INTO phishing_feeds (domain, source, last_seen) VALUES (?, ?, ?)",
            ("evil-phish.com", "openphish", datetime.now(UTC).isoformat()),
        )
        detector = PhishingDetector(db)
        result = detector.analyze(
            from_addr='"Service" <service@evil-phish.com>',
            subject="Your account",
            html_body='<a href="http://evil-phish.com/login">Login</a>',
            body=None,
            account_email="user@test.com",
        )
        assert result["is_phishing"] is True
        assert result["score"] >= 0.9  # feed match is very strong

    def test_watchlist_detected(self, db):
        """Domain on user watchlist should be flagged."""
        from datetime import UTC, datetime

        db.execute(
            "INSERT INTO phishing_domains (domain, brand, reason, reported_at, account_email) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "known-phisher.com",
                None,
                "user-reported",
                datetime.now(UTC).isoformat(),
                "user@test.com",
            ),
        )
        detector = PhishingDetector(db)
        result = detector.analyze(
            from_addr='"Someone" <spam@known-phisher.com>',
            subject="Hello",
            html_body=None,
            body="Hi there",
            account_email="user@test.com",
        )
        assert result["is_phishing"] is True
        assert result["score"] == 1.0
        assert "Previously reported" in str(result["reasons"])

    def test_watchlist_different_account(self, db):
        """Watchlist is per-account — different account should NOT match."""
        from datetime import UTC, datetime

        db.execute(
            "INSERT INTO phishing_domains (domain, brand, reason, reported_at, account_email) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "phisher.com",
                None,
                "user-reported",
                datetime.now(UTC).isoformat(),
                "alice@test.com",
            ),
        )
        detector = PhishingDetector(db)
        result = detector.analyze(
            from_addr='"Spam" <spam@phisher.com>',
            subject="Hello",
            html_body=None,
            body="Hi",
            account_email="bob@test.com",  # different account
        )
        assert result["is_phishing"] is False

    def test_report_fraudulent_adds_to_watchlist(self, db):
        """report_fraudulent should add domain to watchlist."""
        detector = PhishingDetector(db)
        detector.report_fraudulent(
            from_addr='"Scammer" <scam@evil.com>',
            subject="You won!",
            body="Claim your prize",
            account_email="user@test.com",
            message_uuid=None,  # skip feedback log (no FK to messages needed)
        )
        row = db.execute_one(
            "SELECT * FROM phishing_domains WHERE domain = ? AND account_email = ?",
            ("evil.com", "user@test.com"),
        )
        assert row is not None

    def test_report_fraudulent_logs_feedback(self, db):
        """report_fraudulent should log feedback."""
        import uuid

        # mailwatch adaptation: no accounts/folders/messages tables; the
        # feedback insert takes the message UUID directly.
        msg_uuid = str(uuid.uuid4())
        detector = PhishingDetector(db)
        detector.report_fraudulent(
            from_addr='"Scam" <scam@evil.com>',
            subject="You won!",
            body="Claim prize",
            account_email="user@test.com",
            message_uuid=msg_uuid,
        )
        row = db.execute_one(
            "SELECT * FROM spam_feedback WHERE message_uuid = ?",
            (msg_uuid,),
        )
        assert row is not None
        assert row["feedback"] == "fraud"


class TestPhishingFeedUpdater:
    def test_process_plain_feed(self, db):
        """Test parsing a plain URL feed."""
        updater = PhishingFeedUpdater(db)
        count = updater._process_feed(
            "test-feed",
            "http://evil.com/phish\nhttps://bad.net/crime\n",
            "plain",
        )
        assert count == 2
        assert (
            db.execute_one(
                "SELECT * FROM phishing_feeds WHERE domain = ?", ("evil.com",)
            )
            is not None
        )
        assert (
            db.execute_one(
                "SELECT * FROM phishing_feeds WHERE domain = ?", ("bad.net",)
            )
            is not None
        )

    def test_process_csv_feed(self, db):
        """Test parsing a CSV feed (PhishTank format)."""
        updater = PhishingFeedUpdater(db)
        csv_data = (
            "id,url,phish_detail_url,submission_time,verified,verification_time,online,target\n"
            "1,http://phish.com/paypal,http://details.com/,2024-01-01,yes,2024-01-01,yes,PayPal\n"
            "2,http://fake.amazon.com/login,http://details2.com/,2024-01-02,yes,2024-01-02,yes,Amazon\n"
        )
        count = updater._process_feed("phishtank-test", csv_data, "csv")
        assert count == 2
        assert (
            db.execute_one(
                "SELECT * FROM phishing_feeds WHERE domain = ?", ("phish.com",)
            )
            is not None
        )
        assert (
            db.execute_one(
                "SELECT * FROM phishing_feeds WHERE domain = ?", ("fake.amazon.com",)
            )
            is not None
        )

    def test_process_empty_feed(self, db):
        """Empty feed should not error."""
        updater = PhishingFeedUpdater(db)
        count = updater._process_feed("test", "", "plain")
        assert count == 0

    def test_process_json_feed(self, db):
        """PhishStats JSON array format (api.phishstats.info, 2026)."""
        updater = PhishingFeedUpdater(db)
        json_data = (
            '[{"id":1,"url":"https://evil.example.com/login"},'
            '{"id":2,"url":"http://fake.bank.example.net/phish",'
            '"redirect_url":"http://fake.bank.example.net/"},'
            '{"id":3,"url":""}]'
        )
        count = updater._process_feed("phishstats", json_data, "json")
        assert count == 2
        assert db.execute_one(
            "SELECT * FROM phishing_feeds WHERE domain = 'evil.example.com'"
        ) is not None
        assert db.execute_one(
            "SELECT * FROM phishing_feeds WHERE domain = 'fake.bank.example.net'"
        ) is not None

    def test_process_json_feed_invalid(self, db):
        """Malformed JSON should yield 0 without raising."""
        updater = PhishingFeedUpdater(db)
        assert updater._process_feed("phishstats", "not json", "json") == 0
