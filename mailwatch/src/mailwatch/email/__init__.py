"""Email module for mailwatch — vendored from lighterbird (see VENDORED.md)."""

from mailwatch.email.filters import (
    PhishingDetector,
    PhishingFeedUpdater,
    SieveManager,
    SpamClassifier,
    SpamSimilarityDetector,
    SpamTrainer,
    validate_sieve,
)
from mailwatch.email.imap import (
    IMAPCapabilities,
    IMAPClient,
    IMAPIdleManager,
    IMAPIdleThread,
)

__all__ = [
    "IMAPCapabilities",
    "IMAPClient",
    "IMAPIdleManager",
    "IMAPIdleThread",
    "PhishingDetector",
    "PhishingFeedUpdater",
    "SieveManager",
    "SpamClassifier",
    "SpamSimilarityDetector",
    "SpamTrainer",
    "validate_sieve",
]
