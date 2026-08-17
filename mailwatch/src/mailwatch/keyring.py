"""Keyring wrapper for email account passwords.

Vendored from lighterbird's ``email/keyring.py``, inlined against the
``keyring`` package directly (lighterbird delegated to lighterllm).
"""

from __future__ import annotations

import keyring

_SERVICE_PREFIX = "mailwatch/email"


def get_password(account_email: str) -> str | None:
    """Retrieve account password from system keyring."""
    try:
        return keyring.get_password(f"{_SERVICE_PREFIX}/{account_email}", "password")
    except Exception:
        return None


def set_password(account_email: str, password: str) -> bool:
    """Store account password in system keyring."""
    try:
        keyring.set_password(f"{_SERVICE_PREFIX}/{account_email}", "password", password)
        return True
    except Exception:
        return False


def delete_password(account_email: str) -> bool:
    """Remove account password from system keyring."""
    try:
        keyring.delete_password(f"{_SERVICE_PREFIX}/{account_email}", "password")
        return True
    except Exception:
        return False


__all__ = ["delete_password", "get_password", "set_password"]
