"""Centralised mailwatch error types.

mailwatch talks to remote IMAP/Sieve servers.  Failures fall into two
classes with very different handling:

* **Transient** — DNS, TCP, TLS, or socket timeouts.  Safe to retry,
  ideally with exponential backoff (:class:`IMAPConnectionError`).
* **Definitive authentication** — the server rejected the credentials.
  Retrying cannot fix a stale password, and a tight retry loop against
  a provider that rate-limits authentication (Migadu tarpits abusive
  source IPs) turns one stale password into a self-inflicted outage
  (:class:`IMAPAuthError`).

:class:`IMAPAuthError` subclasses :class:`IMAPConnectionError` so generic
handlers still treat it as a connection problem; specific handlers check
the auth subclass first.
"""

from __future__ import annotations

# Exit status used when the daemon stops because authentication failed.
# The systemd unit sets ``RestartPreventExitStatus`` to this value so the
# daemon stays down (and shows as failed) instead of restart-looping.
EXIT_AUTH_FAILURE = 3


class MailwatchError(Exception):
    """Base class for mailwatch domain errors."""


class IMAPConnectionError(MailwatchError):
    """Transient IMAP failure (network, TLS, or timeout). Retryable."""


class IMAPAuthError(IMAPConnectionError):
    """Definitive IMAP authentication failure. Retrying cannot fix it."""


__all__ = [
    "EXIT_AUTH_FAILURE",
    "IMAPAuthError",
    "IMAPConnectionError",
    "MailwatchError",
]
