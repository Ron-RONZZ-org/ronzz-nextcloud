"""Tests for the redirector: fetch raw → append verbatim → remove source."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from mailwatch.audit import AuditLog
from mailwatch.config import AccountConfig, RedirectConfig, RedirectTarget
from mailwatch.redirector import Redirector


def _account_with_redirect() -> AccountConfig:
    return AccountConfig(
        email="ron@ronzz.org",
        redirect=RedirectConfig(
            enabled=True,
            source_folder="send-to-hesk",
            target=RedirectTarget(
                email="hi@ronzz.org",
                destination_folder="INBOX",
            ),
        ),
    )


def test_redirect_disabled_returns_zero():
    account = AccountConfig(email="ron@ronzz.org")  # no redirect config
    audit = AuditLog("/tmp/audit-redirector-test.jsonl", dry_run=True)
    r = Redirector(account, audit, dry_run=True)
    assert r.redirect_account() == 0


def test_redirect_missing_target_returns_zero():
    account = AccountConfig(
        email="ron@ronzz.org",
        redirect=RedirectConfig(enabled=True),  # no target
    )
    audit = AuditLog("/tmp/audit-redirector-test.jsonl", dry_run=True)
    r = Redirector(account, audit, dry_run=True)
    assert r.redirect_account() == 0


def test_redirect_dry_run_no_append_or_delete(monkeypatch, tmp_path):
    account = _account_with_redirect()
    audit = AuditLog(tmp_path / "audit.jsonl", dry_run=True)
    r = Redirector(account, audit, dry_run=True)

    monkeypatch.setattr(
        "mailwatch.redirector.get_password",
        lambda email: f"pw-{email}",
    )

    src_client = MagicMock()
    dst_client = MagicMock()

    # _list_source_uids finds 2 messages
    src_client.select_folder_ex.return_value = (True, None, None)
    src_client.conn.uid.return_value = ("OK", [b"1 2"])
    src_client.fetch_raw.return_value = b"From: x\r\nSubject: test\r\n\r\nbody"

    # Both source and destination default to imap.migadu.com — return the
    # mocks in call order (source client first, destination second).
    monkeypatch.setattr(
        "mailwatch.redirector.IMAPClient",
        MagicMock(side_effect=[src_client, dst_client]),
    )

    n = r.redirect_account()

    assert n == 0  # dry-run: nothing appended
    dst_client.append_message.assert_not_called()
    src_client.set_flags.assert_not_called()
    src_client.disconnect.assert_called_once()
    dst_client.disconnect.assert_called_once()
    # audit records the dry-run lines
    lines = tmp_path.joinpath("audit.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2
    assert all("dry-run" in line for line in lines)


def test_redirect_appends_then_deletes(monkeypatch, tmp_path):
    account = _account_with_redirect()
    audit = AuditLog(tmp_path / "audit.jsonl", dry_run=False)
    r = Redirector(account, audit, dry_run=False)

    monkeypatch.setattr(
        "mailwatch.redirector.get_password",
        lambda email: f"pw-{email}",
    )

    src_client = MagicMock()
    dst_client = MagicMock()
    src_client.select_folder_ex.return_value = (True, None, None)
    src_client.conn.uid.return_value = ("OK", [b"42"])
    raw = b"From: alice@example.com\r\nSubject: help\r\n\r\nbody"
    src_client.fetch_raw.return_value = raw
    dst_client.append_message.return_value = True
    src_client.set_flags.return_value = True

    # Both source and destination default to imap.migadu.com — return the
    # mocks in call order (source client first, destination second).
    monkeypatch.setattr(
        "mailwatch.redirector.IMAPClient",
        MagicMock(side_effect=[src_client, dst_client]),
    )

    n = r.redirect_account()

    assert n == 1
    # Appended WITHOUT flags — must arrive unread so Hesk's unseen-only
    # IMAP intake picks it up.
    dst_client.append_message.assert_called_once_with("INBOX", raw)
    src_client.set_flags.assert_called_once_with(
        42, "send-to-hesk", add=["\\Deleted"]
    )
    src_client.conn.expunge.assert_called_once()
    lines = tmp_path.joinpath("audit.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    assert "ok" in lines[0]


def test_redirect_append_failure_leaves_source(monkeypatch, tmp_path):
    account = _account_with_redirect()
    audit = AuditLog(tmp_path / "audit.jsonl", dry_run=False)
    r = Redirector(account, audit, dry_run=False)

    monkeypatch.setattr(
        "mailwatch.redirector.get_password",
        lambda email: f"pw-{email}",
    )

    src_client = MagicMock()
    dst_client = MagicMock()
    src_client.select_folder_ex.return_value = (True, None, None)
    src_client.conn.uid.return_value = ("OK", [b"7"])
    src_client.fetch_raw.return_value = b"From: x\r\n\r\nbody"
    dst_client.append_message.return_value = False  # append fails

    # Both source and destination default to imap.migadu.com — return the
    # mocks in call order (source client first, destination second).
    monkeypatch.setattr(
        "mailwatch.redirector.IMAPClient",
        MagicMock(side_effect=[src_client, dst_client]),
    )

    n = r.redirect_account()

    assert n == 0
    # Source NOT deleted on failure
    src_client.set_flags.assert_not_called()
    src_client.conn.expunge.assert_not_called()
    lines = tmp_path.joinpath("audit.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    assert "append_failed" in lines[0]


def test_redirect_after_move_folder_uses_move(monkeypatch, tmp_path):
    account = AccountConfig(
        email="ron@ronzz.org",
        redirect=RedirectConfig(
            enabled=True,
            source_folder="send-to-hesk",
            after_move_folder="send-to-hesk-done",
            target=RedirectTarget(email="hi@ronzz.org"),
        ),
    )
    audit = AuditLog(tmp_path / "audit.jsonl", dry_run=False)
    r = Redirector(account, audit, dry_run=False)

    monkeypatch.setattr(
        "mailwatch.redirector.get_password",
        lambda email: f"pw-{email}",
    )

    src_client = MagicMock()
    dst_client = MagicMock()
    src_client.select_folder_ex.return_value = (True, None, None)
    src_client.conn.uid.return_value = ("OK", [b"3"])
    src_client.fetch_raw.return_value = b"From: x\r\n\r\nbody"
    dst_client.append_message.return_value = True
    src_client.move_message.return_value = True

    # Both source and destination default to imap.migadu.com — return the
    # mocks in call order (source client first, destination second).
    monkeypatch.setattr(
        "mailwatch.redirector.IMAPClient",
        MagicMock(side_effect=[src_client, dst_client]),
    )

    n = r.redirect_account()

    assert n == 1
    src_client.move_message.assert_called_once_with(
        3, "send-to-hesk", "send-to-hesk-done"
    )
    src_client.set_flags.assert_not_called()
