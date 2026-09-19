"""Tests for IMAPClient.connect error classification.

A definitive credential rejection must raise ``IMAPAuthError`` (so the
daemon stops instead of retrying); a network/TLS/timeout failure must
raise the plain ``IMAPConnectionError`` (retryable with backoff).
"""

from __future__ import annotations

import imaplib

import pytest

from mailwatch.email.imap.client import IMAPClient
from mailwatch.errors import IMAPAuthError, IMAPConnectionError


class _RejectingIMAP4:
    def __init__(self, *args, **kwargs): ...

    def login(self, username: str, password: str) -> None:
        raise imaplib.IMAP4.error("Authentication failed")


class _TimeoutIMAP4:
    def __init__(self, *args, **kwargs): ...

    def login(self, username: str, password: str) -> None:
        raise TimeoutError("The read operation timed out")


def _client(monkeypatch, factory) -> IMAPClient:
    monkeypatch.setattr(
        "mailwatch.email.imap.client.imaplib.IMAP4_SSL", factory
    )
    return IMAPClient("imap.example.com", 993, True)


def test_login_rejection_raises_auth_error(monkeypatch):
    client = _client(monkeypatch, _RejectingIMAP4)
    with pytest.raises(IMAPAuthError):
        client.connect("me@example.com", "wrong")


def test_timeout_raises_plain_connection_error(monkeypatch):
    client = _client(monkeypatch, _TimeoutIMAP4)
    with pytest.raises(IMAPConnectionError) as excinfo:
        client.connect("me@example.com", "pw")
    # Must NOT be the auth subclass — a timeout is retryable.
    assert not isinstance(excinfo.value, IMAPAuthError)


def test_socket_failure_raises_plain_connection_error(monkeypatch):
    def _boom(*args, **kwargs):
        raise OSError("Network is unreachable")

    client = _client(monkeypatch, _boom)
    with pytest.raises(IMAPConnectionError) as excinfo:
        client.connect("me@example.com", "pw")
    assert not isinstance(excinfo.value, IMAPAuthError)
