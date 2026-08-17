"""Spam/phishing/similarity filters for mailwatch (vendored from lighterbird)."""

from mailwatch.email.filters.phishing import PhishingDetector, PhishingFeedUpdater
from mailwatch.email.filters.sieve import SieveManager, validate_sieve
from mailwatch.email.filters.spam_detect import SpamClassifier, SpamTrainer, _tokenize
from mailwatch.email.filters.spam_similarity import SpamSimilarityDetector

__all__ = [
    "PhishingDetector",
    "PhishingFeedUpdater",
    "SieveManager",
    "SpamClassifier",
    "SpamSimilarityDetector",
    "SpamTrainer",
    "_tokenize",
    "validate_sieve",
]
