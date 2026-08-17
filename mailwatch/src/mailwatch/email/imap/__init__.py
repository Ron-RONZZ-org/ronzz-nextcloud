"""IMAP client package for mailwatch (vendored from lighterbird)."""

from mailwatch.email.imap.capabilities import IMAPCapabilities, detect_capabilities
from mailwatch.email.imap.client import IMAPClient, decode_imap_utf7
from mailwatch.email.imap.idle import IMAPIdleManager, IMAPIdleThread

__all__ = [
    "IMAPCapabilities",
    "IMAPClient",
    "IMAPIdleManager",
    "IMAPIdleThread",
    "decode_imap_utf7",
    "detect_capabilities",
]
