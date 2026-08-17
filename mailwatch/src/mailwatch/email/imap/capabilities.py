"""IMAP capability detection (vendored from lighterbird @ 99baa8ef). — parses server CAPABILITY response."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class IMAPCapabilities:
    """Parsed IMAP server capabilities.

    Detected at connect time via the CAPABILITY command.  Each field
    corresponds to an IMAP extension RFC.
    """

    has_condstore: bool = False  # RFC 4551
    has_qresync: bool = False  # RFC 5162
    has_idle: bool = False  # RFC 2177
    has_move: bool = False  # RFC 6851
    has_uidplus: bool = False  # RFC 4315


def detect_capabilities(conn: Any) -> IMAPCapabilities:
    """Parse server capabilities from an IMAP connection.

    Sends the CAPABILITY command and parses the response.
    Uses ``conn.capability()`` if available, else falls back to
    ``CAPABILITY`` command via ``conn._simple_command()``.

    Args:
        conn: An ``imaplib.IMAP4`` connection instance.

    Returns:
        An :class:`IMAPCapabilities` dataclass with detected features.
    """
    caps = IMAPCapabilities()

    try:
        # imaplib provides capability() which returns a list of bytes
        typ, data = conn.capability()
        if typ != "OK" or not data:
            logger.warning(
                "[capabilities] CAPABILITY command returned %s / %s", typ, data
            )
            return caps

        # data may be a single bytes object or a list
        if isinstance(data, list):
            raw = b" ".join(d for d in data if isinstance(d, bytes))
        elif isinstance(data, bytes):
            raw = data
        else:
            raw = b""

        cap_str = raw.decode("ascii", errors="ignore").upper()

        caps.has_condstore = "CONDSTORE" in cap_str
        caps.has_qresync = "QRESYNC" in cap_str
        caps.has_idle = "IDLE" in cap_str
        caps.has_move = "MOVE" in cap_str or "UID MOVE" in cap_str
        caps.has_uidplus = "UIDPLUS" in cap_str

        logger.debug(
            "[capabilities] Detected: CONDSTORE=%s QRESYNC=%s IDLE=%s MOVE=%s UIDPLUS=%s",
            caps.has_condstore,
            caps.has_qresync,
            caps.has_idle,
            caps.has_move,
            caps.has_uidplus,
        )
    except Exception as exc:
        logger.warning("[capabilities] Failed to detect capabilities: %s", exc)

    return caps


__all__ = ["IMAPCapabilities", "detect_capabilities"]
