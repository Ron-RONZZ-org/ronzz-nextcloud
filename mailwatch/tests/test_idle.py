"""Tests for the RFC 2177 IDLE loop.

imaplib has no IDLE support (``_command("IDLE")`` raises KeyError and
``IMAP4`` has no ``fileno()``), so the loop sends the command manually
and polls ``conn.sock``.  These tests drive ``_run_idle_loop`` with a
fake client + scripted ``select`` to verify entry, notification
delivery, and exit.
"""

from __future__ import annotations

import threading
import time

from mailwatch.email.imap.capabilities import IMAPCapabilities
from mailwatch.email.imap.idle import IMAPIdleThread

_IDLE_TIMEOUT = 29 * 60  # module constant; overridden via fake time below


class FakeConn:
    def __init__(self, lines: list[bytes]):
        self.sent: list[bytes] = []
        self.lines = lines

    def _new_tag(self) -> str:
        return "T00001"

    def send(self, data: bytes) -> None:
        self.sent.append(data)

    def _get_line(self) -> bytes:
        return self.lines.pop(0) if self.lines else b"* 1 EXISTS\r\n"

    def _command_complete(self, tag: str, name: str):
        # No untagged responses accumulated at DONE — the inner-loop
        # notification path is what the test exercises.
        return ("OK", [])

    sock = object()  # select() target — replaced by the monkeypatched select


class FakeIMAPClient:
    def __init__(self, host: str, port: int, use_ssl: bool):
        self.conn = FakeConn([b"+ idling\r\n", b"* 4 EXISTS\r\n"])
        self.capabilities = IMAPCapabilities(has_idle=True)
        self.disconnected = False

    def connect(self, username: str, password: str) -> None:
        pass

    def disconnect(self) -> None:
        self.disconnected = True

    def select_folder_ex(
        self, folder: str, readonly: bool = True, condstore: bool = False
    ):
        return (True, 1, 1)


def _make_thread() -> IMAPIdleThread:
    return IMAPIdleThread(
        account_email="me@ronzz.org",
        host="imap.migadu.com",
        port=993,
        use_ssl=True,
        username="me@ronzz.org",
        password="x",
        folder="INBOX",
        on_notification=lambda account, folder, event: notifications.append(
            (account, folder, event)
        ),
    )


notifications: list[tuple[str, str, str]] = []


def test_idle_loop_enters_and_fires_notification(monkeypatch):
    """Manual IDLE entry + EXISTS notification via the callback."""
    notifications.clear()
    thread = _make_thread()
    fake = FakeIMAPClient("imap.migadu.com", 993, True)
    monkeypatch.setattr(
        "mailwatch.email.imap.client.IMAPClient",
        lambda host, port, ssl: fake,
    )

    select_results = [
        ([fake.conn.sock], [], []),  # read "+ idling"
        ([fake.conn.sock], [], []),  # read "* 4 EXISTS" → notify
    ]

    def fake_select(rlist, wlist, xlist, timeout=None):
        if select_results:
            return select_results.pop(0)
        # Timed out — loop will re-check stop_event
        return ([], [], [])

    monkeypatch.setattr("mailwatch.email.imap.idle.select.select", fake_select)

    # Stop the loop shortly after the two scripted reads.
    def _stop():
        time.sleep(0.2)
        thread._stop_event.set()

    threading.Thread(target=_stop, daemon=True).start()
    thread._run_idle_loop()

    # IDLE entered with a fresh tag, DONE sent on exit.
    sent = b"".join(fake.conn.sent)
    assert b"T00001 IDLE\r\n" in sent
    assert b"DONE\r\n" in sent
    assert notifications == [("me@ronzz.org", "INBOX", "exists")]
    assert fake.disconnected is True


def test_idle_loop_skips_without_capability(monkeypatch):
    """Server without IDLE capability → no IDLE command sent."""
    notifications.clear()
    thread = _make_thread()
    fake = FakeIMAPClient("imap.migadu.com", 993, True)
    fake.capabilities = IMAPCapabilities(has_idle=False)
    monkeypatch.setattr(
        "mailwatch.email.imap.client.IMAPClient",
        lambda host, port, ssl: fake,
    )
    thread._run_idle_loop()
    assert b"IDLE" not in b"".join(fake.conn.sent)
    assert notifications == []


def test_idle_loop_survives_bad_select(monkeypatch):
    """TypeError from select (SSL edge cases) must not crash the loop."""
    notifications.clear()
    thread = _make_thread()
    fake = FakeIMAPClient("imap.migadu.com", 993, True)

    def boom(*a, **k):
        raise TypeError("select() on SSL socket")

    monkeypatch.setattr("mailwatch.email.imap.idle.select.select", boom)
    monkeypatch.setattr(
        "mailwatch.email.imap.client.IMAPClient",
        lambda host, port, ssl: fake,
    )

    # With select raising, the inner loop breaks, IDLE is exited via DONE,
    # and the outer while re-enters IDLE — stop after one full iteration.
    def _stop():
        time.sleep(0.2)
        thread._stop_event.set()

    threading.Thread(target=_stop, daemon=True).start()
    thread._run_idle_loop()
    sent = b"".join(fake.conn.sent)
    assert b"T00001 IDLE\r\n" in sent  # at least one entry attempt
    assert b"DONE\r\n" in sent
