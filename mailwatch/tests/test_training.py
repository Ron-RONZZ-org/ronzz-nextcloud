"""Tests for the two-signal training idler.

The fake IMAP client provides scripted Junk/INBOX contents so the
reconciliation logic is exercised without a network.
"""

from __future__ import annotations

import pytest

from mailwatch.audit import AuditLog
from mailwatch.config import AccountConfig, TrainingConfig
from mailwatch.db import get_db
from mailwatch.training import TrainingIdler


class FakeTrainingClient:
    """Scripted client: Junk/INBOX message-id maps + message bodies."""

    def __init__(
        self,
        junk: dict[int, str],
        inbox: dict[int, str],
        bodies: dict[tuple[str, int], tuple[str, str]],
    ):
        self.junk = junk
        self.inbox = inbox
        self.bodies = bodies

    def connect(self, username: str, password: str) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def fetch_message_ids(self, folder: str) -> dict[int, str]:
        return dict(self.junk if folder == "Junk" else self.inbox)

    def fetch_uids(self, folder: str, uids: list[int], mark_seen: bool = False):
        out = []
        for uid in uids:
            key = (folder, uid)
            if key in self.bodies:
                subject, body = self.bodies[key]
                out.append(
                    {
                        "imap_uid": uid,
                        "message_id": f"<{uid}@example.com>",
                        "subject": subject,
                        "body": body,
                    }
                )
        return out


@pytest.fixture
def db(tmp_path):
    return get_db(tmp_path / "train.db")


@pytest.fixture
def audit(tmp_path):
    return AuditLog(tmp_path / "audit.jsonl")


@pytest.fixture
def account() -> AccountConfig:
    return AccountConfig(email="me@ronzz.org")


class RecordingTrainer:
    """SpamTrainer stand-in that records report() calls."""

    def __init__(self):
        self.calls: list[tuple[str, str, str, bool]] = []

    def report(self, subject: str, body: str, account_email: str, is_spam: bool):
        self.calls.append((subject, body, account_email, is_spam))


def _idler(db, audit, trainer, **cfg_kw):
    cfg = TrainingConfig(**cfg_kw)
    return TrainingIdler(db, cfg, audit, trainer=trainer)


def test_new_junk_arrival_trains_spam(db, audit, account):
    trainer = RecordingTrainer()
    idler = _idler(db, audit, trainer)
    client = FakeTrainingClient(
        junk={10: "mid-10@example.com"},  # new arrival
        inbox={},
        bodies={("Junk", 10): ("Get rich quick", "send money now")},
    )
    summary = idler._reconcile(client, account)
    assert summary["spam"] == 1
    assert summary["ham"] == 0
    assert trainer.calls == [("Get rich quick", "send money now", "me@ronzz.org", True)]
    # Feedback recorded
    row = db.execute_one(
        "SELECT * FROM spam_feedback WHERE message_uuid = 'mid-10@example.com'"
    )
    assert row is not None and row["feedback"] == "spam"
    assert row["source"] == "junk_idler"


def test_daemon_move_excluded_from_training(db, audit, account):
    """Messages the daemon moved must NOT train the classifier (bias guard)."""
    trainer = RecordingTrainer()
    idler = _idler(db, audit, trainer)
    db.execute(
        "INSERT INTO daemon_moves (account_email, imap_uid, message_id, moved_at) "
        "VALUES ('me@ronzz.org', 5, 'mid-5@example.com', '2026-01-01T00:00:00+00:00')"
    )
    client = FakeTrainingClient(
        junk={5: "mid-5@example.com"},  # daemon moved this
        inbox={},
        bodies={("Junk", 5): ("spammy", "spam")},
    )
    summary = idler._reconcile(client, account)
    assert summary["spam"] == 0
    assert summary["skipped_moved"] == 1
    assert trainer.calls == []


def test_daemon_move_matched_by_message_id_not_uid(db, audit, account):
    """After MOVE the UID changes — match must work by Message-ID."""
    trainer = RecordingTrainer()
    idler = _idler(db, audit, trainer)
    # daemon recorded UID 5 in INBOX; the message now sits in Junk as UID 99.
    db.execute(
        "INSERT INTO daemon_moves (account_email, imap_uid, message_id, moved_at) "
        "VALUES ('me@ronzz.org', 5, 'mid-5@example.com', '2026-01-01T00:00:00+00:00')"
    )
    client = FakeTrainingClient(
        junk={99: "mid-5@example.com"},
        inbox={},
        bodies={("Junk", 99): ("spammy", "spam")},
    )
    summary = idler._reconcile(client, account)
    assert summary["skipped_moved"] == 1
    assert trainer.calls == []


def test_junk_to_inbox_trains_ham(db, audit, account):
    """A message leaving Junk and appearing in INBOX is a ham signal."""
    trainer = RecordingTrainer()
    idler = _idler(db, audit, trainer)
    # First scan: message in Junk only.
    client1 = FakeTrainingClient(
        junk={7: "mid-7@example.com"},
        inbox={},
        bodies={},
    )
    idler._reconcile(client1, account)

    # Second scan: user moved it to INBOX.  UIDs change on MOVE (7 in
    # Junk → 99 in INBOX) — the ham fetch must resolve the current
    # INBOX UID by Message-ID.
    client2 = FakeTrainingClient(
        junk={},
        inbox={99: "mid-7@example.com"},
        bodies={("INBOX", 99): ("Important", "please read this carefully")},
    )
    summary = idler._reconcile(client2, account)
    assert summary["ham"] == 1
    assert trainer.calls == [
        ("Important", "please read this carefully", "me@ronzz.org", False)
    ]
    row = db.execute_one(
        "SELECT * FROM spam_feedback WHERE message_uuid = 'mid-7@example.com'"
    )
    assert row is not None and row["feedback"] == "ham"
    assert row["source"] == "junk_to_inbox"


def test_no_double_training(db, audit, account):
    """Repeated scans must not train the same message twice."""
    trainer = RecordingTrainer()
    idler = _idler(db, audit, trainer)
    client = FakeTrainingClient(
        junk={10: "mid-10@example.com"},
        inbox={},
        bodies={("Junk", 10): ("Get rich quick", "send money now")},
    )
    idler._reconcile(client, account)
    idler._reconcile(client, account)  # same state again
    assert len(trainer.calls) == 1


def test_training_disabled(db, audit, account):
    trainer = RecordingTrainer()
    idler = _idler(db, audit, trainer, enabled=False)
    # scan_account short-circuits on enabled=False before any IMAP access.
    summary = idler.scan_account(account)
    assert summary["error"] == "disabled"
    assert trainer.calls == []
