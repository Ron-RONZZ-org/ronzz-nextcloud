# ruff: noqa: B007, RUF001
"""Tests for spam similarity detection — MinHash, content hashes, repeated-spam guard."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from mailwatch.email.db import get_db
from mailwatch.email.filters.spam_similarity import (
    SpamSimilarityDetector,
    _content_hash,
    _extract_domain,
    _jaccard_from_bytes,
    _make_shingles,
    _minhash_signature,
    _normalize,
)


@pytest.fixture
def db(tmp_path: Path):
    return get_db(tmp_path / "email.db")


# mailwatch adaptation: the daemon has no accounts/folders/messages tables,
# and add_spam takes from_addr directly.  This registry lets tests pass the
# From header through the original _insert_msg call sites unchanged.
_INSERTED: dict[str, str] = {}


def _insert_msg(
    db,
    msg_uuid: str,
    subject: str = "Test",
    body: str = "Body",
    from_addr: str = "spam@example.com",
    account_email: str = "user@test.com",
):
    """Record the sender for add_spam's from_addr parameter."""
    _INSERTED[msg_uuid] = from_addr


# ── Unit tests for pure functions ─────────────────────────────────────────


class TestNormalize:
    def test_lowercase(self):
        assert _normalize("HELLO World") == "hello world"

    def test_collapse_whitespace(self):
        assert _normalize("hello   world\n\tfoo") == "hello world foo"

    def test_strip(self):
        assert _normalize("  hello world  ") == "hello world"


class TestContentHash:
    def test_deterministic(self):
        assert _content_hash("hello world") == _content_hash("hello world")

    def test_different_inputs(self):
        assert _content_hash("spam") != _content_hash("ham")

    def test_normalization(self):
        """Content hash should be invariant to whitespace/case."""
        assert _content_hash("Hello  World") == _content_hash("hello world")


class TestExtractDomain:
    def test_email(self):
        assert _extract_domain("user@example.com") == "example.com"

    def test_with_name(self):
        assert _extract_domain('"Spammer" <spam@evil.com>') == "evil.com"

    def test_url(self):
        # _extract_domain is for email From headers, not generic URLs
        # URL-like input without @ returns None
        assert _extract_domain("https://phishing.com/path") is None

    def test_none(self):
        assert _extract_domain("") is None

    def test_no_at(self):
        assert _extract_domain("not-an-email") is None


class TestMakeShingles:
    def test_basic_bigrams(self):
        shingles = _make_shingles(["a", "b", "c", "d"])
        assert shingles == ["a b", "b c", "c d"]

    def test_single_word(self):
        assert _make_shingles(["hello"]) == ["hello"]

    def test_empty(self):
        assert _make_shingles([]) == []

    def test_two_words(self):
        assert _make_shingles(["spam", "alert"]) == ["spam alert"]


class TestMinHashSignature:
    def test_signature_length(self):
        words = ["hello", "world"]
        sig = _minhash_signature(words, num_perm=128)
        assert len(sig) == 128 * 4  # 512 bytes

    def test_deterministic(self):
        sig1 = _minhash_signature(["hello", "world"])
        sig2 = _minhash_signature(["hello", "world"])
        assert sig1 == sig2

    def test_different_inputs_different_sig(self):
        sig1 = _minhash_signature(["hello", "world"])
        sig2 = _minhash_signature(["buy", "now", "cheap"])
        assert sig1 != sig2

    def test_empty_shingles(self):
        sig = _minhash_signature([])
        assert len(sig) == 128 * 4

    def test_zero_signature_not_all_zeros(self):
        sig = _minhash_signature(["hello", "world"])
        fmt = "<128I"
        import struct

        values = struct.unpack(fmt, sig)
        # At least some positions should be non-zero
        assert any(v != 0 for v in values)


class TestJaccardFromBytes:
    def test_identical(self):
        sig = _minhash_signature(["hello", "world"])
        assert _jaccard_from_bytes(sig, sig) == 1.0

    def test_different(self):
        sig_a = _minhash_signature(["hello", "world", "test"])
        sig_b = _minhash_signature(["buy", "now", "cheap", "offer"])
        # Different content should have low similarity
        sim = _jaccard_from_bytes(sig_a, sig_b)
        assert sim < 0.5

    def test_similar_text(self):
        """Near-duplicate text should have high Jaccard."""
        sig_a = _minhash_signature(["hello", "world", "this", "is", "spam"])
        sig_b = _minhash_signature(
            ["hello", "world", "this", "is", "spam", "buy", "now"]
        )
        sim = _jaccard_from_bytes(sig_a, sig_b)
        assert sim > 0.3  # significantly similar


# ── Integration tests for SpamSimilarityDetector ──────────────────────────


class TestSpamSimilarityDetector:
    def test_add_spam_creates_signature(self, db):
        """Adding a spam message should create a signature row."""
        msg_uuid = str(uuid.uuid4())
        _insert_msg(
            db,
            msg_uuid,
            subject="Buy cheap medicine now",
            body="Click here for cheap prices",
            from_addr="spam@evil.com",
        )

        detector = SpamSimilarityDetector(db)
        detector.add_spam(
            msg_uuid,
            "Buy cheap medicine now",
            "Click here for cheap prices",
            "user@test.com",
            from_addr=_INSERTED.get(msg_uuid, ""),
        )

        row = db.execute_one(
            "SELECT COUNT(*) AS cnt FROM spam_signatures",
        )
        assert row["cnt"] == 1

    def test_add_spam_content_hash(self, db):
        """Adding spam should also create a content hash entry."""
        msg_uuid = str(uuid.uuid4())
        _insert_msg(
            db,
            msg_uuid,
            subject="Win a prize!",
            body="You have won",
            from_addr="winner@scam.com",
        )

        detector = SpamSimilarityDetector(db)
        detector.add_spam(
            msg_uuid,
            "Win a prize!",
            "You have won",
            "user@test.com",
            from_addr=_INSERTED.get(msg_uuid, ""),
        )

        row = db.execute_one(
            "SELECT COUNT(*) AS cnt FROM spam_content_hashes",
        )
        assert row["cnt"] == 1

    def test_remove_message_deletes_signature(self, db):
        """Removing a message should delete its signature."""
        msg_uuid = str(uuid.uuid4())
        _insert_msg(db, msg_uuid)
        detector = SpamSimilarityDetector(db)
        detector.add_spam(
            msg_uuid,
            "Test",
            "Body",
            "user@test.com",
            from_addr=_INSERTED.get(msg_uuid, ""),
        )
        assert detector.count_signatures() == 1

        detector.remove_message(msg_uuid)
        assert detector.count_signatures() == 0

    def test_check_similarity_no_match(self, db):
        """A unique message should have low similarity to stored spam."""
        # Store one spam
        msg1 = str(uuid.uuid4())
        _insert_msg(
            db, msg1, subject="Buy cheap meds", body="Click here for cheap pills online"
        )
        detector = SpamSimilarityDetector(db)
        detector.add_spam(
            msg1,
            "Buy cheap meds",
            "Click here for cheap pills online",
            "user@test.com",
            from_addr=_INSERTED.get(msg1, ""),
        )

        # Check a completely different message
        result = detector.check_similarity(
            "Meeting tomorrow",
            "Let's discuss quarterly results",
            "user@test.com",
        )
        assert result["score"] < 0.3
        assert not result["is_similar"]
        assert not result["is_near_duplicate"]

    def test_check_similarity_near_duplicate(self, db):
        """A near-duplicate message should have high similarity."""
        msg1 = str(uuid.uuid4())
        _insert_msg(
            db,
            msg1,
            subject="Urgent! Your account",
            body="Click to verify your account details now",
        )
        detector = SpamSimilarityDetector(db)
        detector.add_spam(
            msg1,
            "Urgent! Your account",
            "Click to verify your account details now",
            "user@test.com",
            from_addr=_INSERTED.get(msg1, ""),
        )

        # Nearly identical message
        result = detector.check_similarity(
            "Urgent! Your account",
            "Click to verify your account details immediately",
            "user@test.com",
        )
        # Should detect some similarity (the texts are nearly the same)
        assert result["score"] > 0.3

    def test_check_similarity_different_account(self, db):
        """Signatures are per-account; other account should not match."""
        msg1 = str(uuid.uuid4())
        _insert_msg(db, msg1, account_email="alice@test.com")
        detector = SpamSimilarityDetector(db)
        detector.add_spam(
            msg1,
            "Spammy subject",
            "Spammy body text",
            "alice@test.com",
            from_addr=_INSERTED.get(msg1, ""),
        )

        # Check from a different account — should find nothing
        result = detector.check_similarity(
            "Spammy subject",
            "Spammy body text",
            "bob@test.com",
        )
        assert result["score"] == 0.0

    def test_check_repeated_same_sender(self, db):
        """Same content from same sender multiple times → repeated."""
        detector = SpamSimilarityDetector(db)
        content = "Buy cheap watches online"

        # Add same spam message 3 times
        for i in range(3):
            uid = str(uuid.uuid4())
            _insert_msg(
                db,
                uid,
                subject="Special offer",
                body=content,
                from_addr="spam@evil.com",
            )
            detector.add_spam(
                uid,
                "Special offer",
                content,
                "user@test.com",
                from_addr=_INSERTED.get(uid, ""),
            )

        # Check fourth occurrence
        result = detector.check_repeated(
            "Special offer",
            content,
            "spam@evil.com",
            "user@test.com",
        )
        assert result["is_repeated"]
        assert "3×" in result["reason"]

    def test_check_repeated_multiple_senders(self, db):
        """Same content from 2+ sender domains → campaign detection."""
        detector = SpamSimilarityDetector(db)
        content = "You won a free iPhone!"

        # Add from first domain
        uid1 = str(uuid.uuid4())
        _insert_msg(db, uid1, body=content, from_addr="scam1@evil1.com")
        detector.add_spam(
            uid1,
            "Congratulations!",
            content,
            "user@test.com",
            from_addr=_INSERTED.get(uid1, ""),
        )

        # Add from second domain
        uid2 = str(uuid.uuid4())
        _insert_msg(db, uid2, body=content, from_addr="scam2@evil2.com")
        detector.add_spam(
            uid2,
            "Congratulations!",
            content,
            "user@test.com",
            from_addr=_INSERTED.get(uid2, ""),
        )

        # Check a third occurrence from a third domain
        result = detector.check_repeated(
            "Congratulations!",
            content,
            "scam3@evil3.com",
            "user@test.com",
        )
        assert result["is_repeated"]
        assert "2 different senders" in result["reason"]

    def test_check_repeated_not_repeated(self, db):
        """A unique message should not flag as repeated."""
        result = detector = SpamSimilarityDetector(db)
        result = detector.check_repeated(
            "Hello",
            "Unique content here",
            "friend@good.com",
            "user@test.com",
        )
        assert not result["is_repeated"]
        assert result["occurrence_count"] == 0

    def test_get_stats(self, db):
        """Stats should reflect stored data."""
        msg1 = str(uuid.uuid4())
        msg2 = str(uuid.uuid4())
        _insert_msg(db, msg1)
        _insert_msg(db, msg2)

        detector = SpamSimilarityDetector(db)
        detector.add_spam(
            msg1,
            "Spam A",
            "Content A",
            "user@test.com",
            from_addr=_INSERTED.get(msg1, ""),
        )
        detector.add_spam(
            msg2,
            "Spam B",
            "Content B",
            "user@test.com",
            from_addr=_INSERTED.get(msg2, ""),
        )

        stats = detector.get_stats()
        assert stats["signatures"] == 2
        assert stats["content_hashes"] == 2

    def test_empty_text_handling(self, db):
        """Empty text should not crash and return neutral results."""
        detector = SpamSimilarityDetector(db)

        result = detector.check_similarity("", "", "user@test.com")
        assert result["score"] == 0.0

        result = detector.check_repeated("", "", "", "user@test.com")
        assert not result["is_repeated"]
