"""Tests for the mailwatch classification pipeline.

Uses a fake IMAPClient so no network is involved.  The fake provides
a scripted UNSEEN set, parsed messages, and records move calls.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from mailwatch.audit import AuditLog
from mailwatch.config import AccountConfig, AutoBlockConfig, DaemonConfig
from mailwatch.db import get_db
from mailwatch.pipeline import MailwatchPipeline
from mailwatch.sieve_block import SieveAutoBlocker


def _make_message(
    uid: int,
    subject: str,
    body: str,
    from_addr: str = "sender@example.com",
    message_id: str | None = None,
) -> dict:
    return {
        "uuid": str(uuid.uuid4()),
        "account_email": "",
        "folder_name": "INBOX",
        "message_id": message_id or f"<mid-{uid}@example.com>",
        "in_reply_to": "",
        "imap_uid": uid,
        "from_addr": from_addr,
        "to_recipients": "[]",
        "cc_recipients": "[]",
        "subject": subject,
        "body": body,
        "html_body": None,
        "priority": 5,
        "is_read": 0,
        "is_starred": 0,
        "is_deleted": 0,
        "received_at": datetime.now(UTC).isoformat(),
        "created_at": datetime.now(UTC).isoformat(),
        "updated_at": datetime.now(UTC).isoformat(),
    }


class FakeIMAPClient:
    """Scripted IMAP client double."""

    def __init__(self, messages: list[dict], *, move_ok: bool = True):
        self.messages = messages
        self.move_ok = move_ok
        self.moves: list[tuple[int, str, str]] = []
        self.selected: list[str] = []
        self.connected = False

    def connect(self, username: str, password: str) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def select_folder_ex(
        self, folder: str, readonly: bool = True, condstore: bool = False
    ):
        self.selected.append(folder)
        return True, 1, 1

    @property
    def conn(self):
        messages = self.messages

        class _Conn:
            def uid(self, command, *args):
                if command.upper() == "SEARCH":
                    uids = [m["imap_uid"] for m in messages]
                    return ("OK", [(" ".join(str(u) for u in uids)).encode()])
                return ("OK", [])

        return _Conn()

    def fetch_uids(self, folder: str, uids: list[int], mark_seen: bool = False):
        return [m for m in self.messages if m["imap_uid"] in uids]

    def move_message(self, uid: int, from_folder: str, to_folder: str) -> bool:
        self.moves.append((uid, from_folder, to_folder))
        return self.move_ok


@pytest.fixture
def db(tmp_path):
    return get_db(tmp_path / "test.db")


@pytest.fixture
def audit(tmp_path):
    return AuditLog(tmp_path / "audit.jsonl")


def _pipeline(db, audit, *, dry_run=False, threshold=0.9, auto_block=True):
    cfg = DaemonConfig(
        dry_run=dry_run,
        spam_threshold=threshold,
        auto_block=AutoBlockConfig(enabled=auto_block),
    )
    blocker = SieveAutoBlocker(db, cfg.auto_block)
    return MailwatchPipeline(db, cfg, audit, blocker)


def _account(**kw) -> AccountConfig:
    defaults = dict(
        email="me@ronzz.org",
        imap_host="imap.migadu.com",
        sieve_host="managesieve.migadu.com",
    )
    defaults.update(kw)
    return AccountConfig(**defaults)


def test_no_unseen_messages(db, audit, monkeypatch):
    fake = FakeIMAPClient([])
    monkeypatch.setattr("mailwatch.pipeline.IMAPClient", lambda *a, **k: fake)
    monkeypatch.setattr("mailwatch.pipeline.get_password", lambda e: "secret")
    pl = _pipeline(db, audit)
    assert pl.process_account(_account()) == 0
    assert fake.moves == []


def test_spam_moved_to_junk(db, audit, monkeypatch):
    msg = _make_message(
        1,
        "URGENT: cheap pills 50% OFF",
        "buy now cheap pills pharmacy discount offer",
        from_addr="scammer <spam@evil.example>",
    )
    fake = FakeIMAPClient([msg])
    monkeypatch.setattr("mailwatch.pipeline.IMAPClient", lambda *a, **k: fake)
    monkeypatch.setattr("mailwatch.pipeline.get_password", lambda e: "secret")
    pl = _pipeline(db, audit)
    assert pl.process_account(_account()) == 1
    assert fake.moves == [(1, "INBOX", "Junk")]
    # daemon move recorded for training exclusion
    row = db.execute_one("SELECT * FROM daemon_moves WHERE imap_uid = 1")
    assert row is not None
    assert row["message_id"] == msg["message_id"]


def test_ham_not_moved(db, audit, monkeypatch):
    msg = _make_message(
        2,
        "Weekly team meeting",
        "please find attached the minutes of the weekly meeting",
    )
    fake = FakeIMAPClient([msg])
    monkeypatch.setattr("mailwatch.pipeline.IMAPClient", lambda *a, **k: fake)
    monkeypatch.setattr("mailwatch.pipeline.get_password", lambda e: "secret")
    pl = _pipeline(db, audit)
    assert pl.process_account(_account()) == 1
    assert fake.moves == []


def test_dry_run_no_move(db, audit, monkeypatch):
    msg = _make_message(
        3,
        "URGENT: pills discount OFFER",
        "cheap pills discount pharmacy offer now",
        from_addr="spam@evil.example",
    )
    fake = FakeIMAPClient([msg])
    monkeypatch.setattr("mailwatch.pipeline.IMAPClient", lambda *a, **k: fake)
    monkeypatch.setattr("mailwatch.pipeline.get_password", lambda e: "secret")
    pl = _pipeline(db, audit, dry_run=True)
    assert pl.process_account(_account()) == 1
    assert fake.moves == []  # dry-run must not move
    # Dry-run must not mutate DB state either.
    assert db.execute_one("SELECT 1 AS x FROM daemon_moves") is None
    assert db.execute_one("SELECT 1 AS x FROM block_candidates") is None


def test_phishing_flagged_and_moved(db, audit, monkeypatch):
    """A phishing-feed hit must be flagged and moved, logged distinctly."""
    # Seed a phishing domain directly (no network).
    db.execute(
        "INSERT OR REPLACE INTO phishing_feeds (domain, source, last_seen) "
        "VALUES ('phish.example.com', 'openphish', ?)",
        (datetime.now(UTC).isoformat(),),
    )
    msg = _make_message(
        4,
        "Your account was locked",
        "verify your account details immediately",
        from_addr='"Migadu Support" <support@phish.example.com>',
        message_id="<phish-1@example.com>",
    )
    msg["html_body"] = '<a href="https://phish.example.com/verify">Verify now</a>'
    fake = FakeIMAPClient([msg])
    monkeypatch.setattr("mailwatch.pipeline.IMAPClient", lambda *a, **k: fake)
    monkeypatch.setattr("mailwatch.pipeline.get_password", lambda e: "secret")
    pl = _pipeline(db, audit)
    assert pl.process_account(_account()) == 1
    assert fake.moves == [(4, "INBOX", "Junk")]

    lines = audit.path.read_text().strip().splitlines()
    classif = [l for l in lines if '"classification"' in l]
    assert classif, "expected a classification audit line"
    import json

    rec = json.loads(classif[0])
    assert rec["is_phishing"] is True
    assert rec["action"] == "move_junk_phishing"
    assert any("phishing feed" in r for r in rec["reasons"])


def test_repeat_offender_blocked(db, audit, monkeypatch):
    """3 spam hits from one domain → auto-block + Sieve push."""
    account = _account()
    pl = _pipeline(db, audit)
    # Pre-record 2 hits so the 3rd triggers the threshold.
    for _ in range(2):
        pl.blocker.register_hit(account.email, "evil.example")

    pushed = {"count": 0}

    class _BlockerStub:
        def push_block_script(self, acct, password, dry_run=False):
            pushed["count"] += 1
            return True

    pl.blocker.push_block_script = _BlockerStub().push_block_script  # type: ignore

    msg = _make_message(
        5,
        "URGENT: cheap pills 50% OFF",
        "buy now cheap pills pharmacy discount offer",
        from_addr="scam@evil.example",
    )
    fake = FakeIMAPClient([msg])
    monkeypatch.setattr("mailwatch.pipeline.IMAPClient", lambda *a, **k: fake)
    monkeypatch.setattr("mailwatch.pipeline.get_password", lambda e: "secret")
    pl.process_account(account)

    row = db.execute_one(
        "SELECT * FROM block_candidates WHERE sender_domain = 'evil.example'"
    )
    assert row is not None
    assert row["blocked_at"] is not None
    assert pushed["count"] == 1
    lines = audit.path.read_text()
    assert '"sieve_block"' in lines


def test_sender_domain_extraction():
    from mailwatch.email.filters.spam_similarity import _extract_domain

    assert _extract_domain("scammer <spam@evil.example>") == "evil.example"
    assert _extract_domain("noreply@sub.evil.example") == "sub.evil.example"
    assert _extract_domain("") is None
