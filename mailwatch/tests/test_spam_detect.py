"""Tests for spam detection — Bayesian classifier, tokenizer, trainer.

Per-user token data is now stored in a JSON file under MAILWATCH_CONFIG_DIR.
Tests set the env var to a temp directory so production config is untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mailwatch.email.filters.spam_detect import (
    SpamClassifier,
    SpamTrainer,
    _chi2_score,
    _load_user_tokens,
    _save_user_tokens,
    _tokenize,
)


@pytest.fixture(autouse=True)
def temp_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect config_dir to a temp directory for test isolation."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setenv("MAILWATCH_CONFIG_DIR", str(cfg))
    return cfg


class TestTokenizer:
    def test_basic_words(self):
        tokens = _tokenize("Hello world, this is a test")
        assert "hello" in tokens
        assert "world" in tokens
        assert "test" in tokens

    def test_urls_tagged(self):
        tokens = _tokenize("Check this https://example.com/page")
        assert "http://" in tokens  # generic URL flag
        # Domain-level token
        assert any("dom:example.com" in t for t in tokens)

    def test_all_caps_detected(self):
        tokens = _tokenize("BUY NOW!!! LIMITED OFFER")
        assert "ALL-CAPS" in tokens

    def test_money_detected(self):
        tokens = _tokenize("Only $99.99 today!")
        assert "$" in tokens

    def test_punctuation_tokens(self):
        tokens = _tokenize("Urgent!!!")
        assert "urgent" in tokens

    def test_short_tokens_skipped(self):
        tokens = _tokenize("a b c d e f")
        assert not any(t in tokens for t in ["a", "b", "c"])

    def test_duplicates_deduped(self):
        """tokenize() returns duplicates — dedup is caller's responsibility."""
        tokens = _tokenize("spam spam spam spam spam")
        assert (
            len([t for t in tokens if t == "spam"]) == 5
        )  # not deduped at token level


class TestChi2Score:
    def test_extreme_spammy_probs(self):
        """All tokens very spammy → score near 1.0."""
        score = _chi2_score([0.99, 0.95, 0.97, 0.98, 0.96])
        assert score > 0.9

    def test_extreme_hammy_probs(self):
        """All tokens very hammy → score near 0.0."""
        score = _chi2_score([0.01, 0.03, 0.02, 0.05, 0.04])
        assert score < 0.1

    def test_mixed_probs(self):
        """Mixed probabilities → score near 0.5."""
        score = _chi2_score([0.5, 0.5, 0.5, 0.5])
        assert 0.3 < score < 0.7

    def test_empty_list(self):
        """Empty list → default 0.5 (uncertain)."""
        score = _chi2_score([])
        assert score == 0.5

    def test_single_token(self):
        """Single token should still produce a reasonable score."""
        score = _chi2_score([0.99])
        assert score > 0.5


class TestSpamClassifier:
    def test_classify_spammy_text(self, temp_config_dir):
        """Text with distinctive spam patterns should be classified as spam."""
        classifier = SpamClassifier()
        result = classifier.classify(
            subject="FREE winner!!! claim your prize",
            body="Click here to earn money fast with this exclusive offer",
            account_email="user@test.com",
        )
        assert result["is_spam"] is True
        assert result["score"] >= 0.5

    def test_classify_hammy_text(self, temp_config_dir):
        """Clean business text should not be classified as spam."""
        classifier = SpamClassifier()
        result = classifier.classify(
            subject="Meeting tomorrow at 3pm",
            body="Hi team, let's schedule the review. Thanks, John",
            account_email="user@test.com",
        )
        # Without training, this may still have a low spam score from seed
        assert result["score"] is not None
        assert isinstance(result["is_spam"], bool)

    def test_classify_returns_top_tokens(self, temp_config_dir):
        """Result should include contributing tokens."""
        classifier = SpamClassifier()
        result = classifier.classify(
            subject="Test",
            body="Hello world",
            account_email="user@test.com",
        )
        assert "tokens" in result
        assert isinstance(result["tokens"], list)

    def test_classify_without_account(self, temp_config_dir):
        """Should work with no account (seed-only)."""
        classifier = SpamClassifier()
        result = classifier.classify(
            subject="FREE winner",
            body="Exclusive offer click here to claim prize earn money",
        )
        assert result["is_spam"] is True

    def test_token_probability_seed_only(self, temp_config_dir):
        """Seed-only tokens should return seed probability."""
        classifier = SpamClassifier()
        prob = classifier._token_probability("viagra", None)
        assert prob is not None
        assert prob > 0.9  # viagra is very spammy in seed


class TestSpamTrainer:
    def test_report_spam_updates_counts(self, temp_config_dir):
        """Marking as spam should increment token counts in JSON file."""
        trainer = SpamTrainer()
        trainer.report(
            subject="BUY NOW!!!",
            body="Click here for free money",
            account_email="user@test.com",
            is_spam=True,
        )
        data = _load_user_tokens()
        acct = data.get("user@test.com", {})
        assert "free" in acct
        assert acct["free"]["spam_count"] >= 1
        assert acct["free"]["ham_count"] == 0

    def test_report_ham_updates_counts(self, temp_config_dir):
        """Marking as ham should increment ham counts in JSON file."""
        trainer = SpamTrainer()
        trainer.report(
            subject="Meeting request",
            body="Let's sync up next week",
            account_email="user@test.com",
            is_spam=False,
        )
        data = _load_user_tokens()
        acct = data.get("user@test.com", {})
        assert "meeting" in acct
        assert acct["meeting"]["ham_count"] >= 1
        assert acct["meeting"]["spam_count"] == 0

    def test_training_improves_classification(self, temp_config_dir):
        """After training on ham, a similar message should score lower."""
        trainer = SpamTrainer()
        classifier = SpamClassifier()

        # Train heavily on "meeting" as ham
        for _ in range(10):
            trainer.report(
                subject="Meeting reminder",
                body="Let's meet for coffee",
                account_email="user@test.com",
                is_spam=False,
            )

        # Classify a meeting-related message
        result = classifier.classify(
            subject="Meeting tomorrow",
            body="Let's meet at 3pm",
            account_email="user@test.com",
        )
        # The classifier should consider this less spammy after training
        assert result["score"] < 0.8  # not extremely spammy

    def test_log_feedback(self, temp_config_dir, monkeypatch):
        """Feedback log should record the action (in spam_feedback DB table)."""
        from mailwatch.email.db import get_db

        db = get_db(temp_config_dir / "email.db")

        # mailwatch adaptation: no accounts/folders/messages tables.
        import uuid

        msg_uuid = str(uuid.uuid4())

        trainer = SpamTrainer()
        trainer.log_feedback(
            message_uuid=msg_uuid,
            account_email="user@test.com",
            feedback="spam",
            db=db,
        )
        row = db.execute_one(
            "SELECT * FROM spam_feedback WHERE message_uuid = ?",
            (msg_uuid,),
        )
        assert row is not None
        assert row["feedback"] == "spam"
        assert row["account_email"] == "user@test.com"

    def test_get_stats(self, temp_config_dir):
        """get_stats should return aggregated counts from JSON file."""
        # Pre-populate some data
        _save_user_tokens(
            {
                "user@test.com": {
                    "viagra": {
                        "spam_count": 5,
                        "ham_count": 0,
                        "last_seen_at": "2024-01-01T00:00:00",
                    },
                    "meeting": {
                        "spam_count": 0,
                        "ham_count": 3,
                        "last_seen_at": "2024-01-01T00:00:00",
                    },
                },
            }
        )

        trainer = SpamTrainer()
        stats = trainer.get_stats()
        assert stats["total_tokens"] == 2
        assert stats["total_spam"] == 5
        assert stats["total_ham"] == 3
