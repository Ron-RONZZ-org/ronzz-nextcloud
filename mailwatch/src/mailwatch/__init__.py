"""mailwatch — headless IMAP IDLE spam/idler daemon for Migadu-hosted mail.

Derived from lighterbird's email module (see VENDORED.md for provenance).
Watches configured accounts via RFC 2177 IDLE, classifies new messages
(Bayesian + phishing feeds + MinHash similarity), moves spam to the Junk
folder on the IMAP server, optionally pushes Sieve rules for repeat
offenders, and trains the Bayesian classifier from Junk-folder activity.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
