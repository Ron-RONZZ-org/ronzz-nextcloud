"""IMAP helper functions (vendored from lighterbird @ 99baa8ef). for header decoding and email parsing.

Forked from A-lien's imap/helpers.py.
"""

from __future__ import annotations

from email.header import decode_header


def decode_mime_header(value: str) -> str:
    """Decode a MIME encoded header value to plain text."""
    if not value:
        return ""
    parts = decode_header(value)
    result: list[str] = []
    for part, encoding in parts:
        if isinstance(part, bytes):
            result.append(part.decode(encoding or "utf-8", errors="replace"))
        else:
            result.append(str(part))
    return " ".join(result)


def parse_email_address(value: str) -> str:
    """Extract email address from 'Name <addr@dom.ain>' form."""
    if not value:
        return ""
    if "<" in value and ">" in value:
        return value.split("<")[1].split(">")[0].strip()
    return value.strip()


def extract_sender_name(from_header: str) -> str:
    """Extract display name from 'Name <addr@dom.ain>' form."""
    if not from_header:
        return ""
    if "<" in from_header and ">" in from_header:
        return from_header.split("<")[0].strip().strip("\"'")
    return ""


def parse_address_list(value: str) -> list[str]:
    """Parse a list of email addresses from a header value."""
    if not value:
        return []
    results = []
    for part in value.split(","):
        addr = parse_email_address(part.strip())
        if addr:
            results.append(addr)
    return results
