"""Continuous training — the two-signal feedback loop.

Design (issue #2 open question, approved):

* **Spam signal** — new arrivals in the Junk folder that the daemon did
  NOT move itself (Migadu gateway verdicts, manual user moves).  These
  are independent observations, not self-confirmation.

* **Ham signal (bias corrector)** — messages that disappear from Junk
  and reappear in INBOX (the user said "not spam").  Without this the
  classifier would self-confirm; with it the loop is balanced.

The daemon's own moves are excluded via the ``daemon_moves`` table
(see ``pipeline._record_daemon_move``), so auto-moved spam is never
trained on.  Each message trains at most once per label+source — repeated
scans cannot double-count.

Junk is watched with the same RFC 2177 IDLE manager (fast spam
training), plus a periodic reconciliation scan (``scan_interval_seconds``)
that detects Junk→INBOX moves (a removal from Junk is not an IDLE event).
"""

from __future__ import annotations

import json
import logging
import uuid as uuid_mod
from datetime import UTC, datetime
from typing import Any

from mailwatch.audit import AuditLog
from mailwatch.config import AccountConfig, TrainingConfig
from mailwatch.db import MailwatchDB
from mailwatch.email.filters import SpamTrainer
from mailwatch.email.imap.client import IMAPClient

logger = logging.getLogger(__name__)


class TrainingIdler:
    """Two-signal continuous training on the Junk folder."""

    def __init__(
        self,
        db: MailwatchDB,
        cfg: TrainingConfig,
        audit: AuditLog,
        trainer: SpamTrainer | None = None,
    ) -> None:
        self._db = db
        self.cfg = cfg
        self.audit = audit
        self.trainer = trainer or SpamTrainer()

    def scan_account(
        self, account: AccountConfig, password: str | None = None
    ) -> dict[str, Any]:
        """Run one reconciliation scan for a single account.

        Returns a summary dict with trained counts.
        """
        from mailwatch.keyring import get_password

        if not self.cfg.enabled:
            return {"spam": 0, "ham": 0, "skipped_moved": 0, "error": "disabled"}

        pw = password if password is not None else get_password(account.email)
        if not pw:
            logger.warning("[training] No password for %s", account.email)
            return {"spam": 0, "ham": 0, "skipped_moved": 0, "error": "no-password"}

        client = IMAPClient(account.imap_host, account.imap_port, account.imap_use_ssl)
        try:
            client.connect(account.imap_username, pw)
        except ConnectionError as exc:
            logger.warning("[training] Cannot connect %s: %s", account.email, exc)
            return {"spam": 0, "ham": 0, "skipped_moved": 0, "error": str(exc)}

        try:
            return self._reconcile(client, account)
        finally:
            client.disconnect()

    def _reconcile(self, client: IMAPClient, account: AccountConfig) -> dict[str, Any]:
        """Compare current Junk/INBOX contents against stored state."""
        junk_folder = account.resolved_junk_folder
        current_junk = client.fetch_message_ids(junk_folder)
        current_inbox = client.fetch_message_ids("INBOX")

        prev = self._load_state(account.email, junk_folder)

        summary = {"spam": 0, "ham": 0, "skipped_moved": 0, "error": None}

        # ── Spam signal: new Junk arrivals not moved by us ────────────
        for uid, message_id in current_junk.items():
            if uid in prev:
                continue  # already seen
            if self._is_daemon_move(account.email, uid, message_id):
                summary["skipped_moved"] += 1
                continue
            if self._train_spam(client, account, uid, message_id):
                summary["spam"] += 1

        # ── Ham signal: message left Junk AND is now in INBOX ─────────
        # UIDs change on MOVE, so resolve the message's *current* INBOX
        # UID by Message-ID before fetching.
        inbox_uids: dict[str, int] = {}
        for inbox_uid, message_id in current_inbox.items():
            if message_id:
                inbox_uids[message_id] = inbox_uid

        for uid, message_id in prev.items():
            if uid in current_junk:
                continue  # still in Junk
            inbox_uid = inbox_uids.get(message_id)
            if inbox_uid is not None and self._train_ham(
                client, account, inbox_uid, message_id
            ):
                summary["ham"] += 1

        self._save_state(account.email, junk_folder, current_junk)
        return summary

    # ── Training ──────────────────────────────────────────────────────

    def _train_spam(
        self, client: IMAPClient, account: AccountConfig, uid: int, message_id: str
    ) -> bool:
        """Train as spam: fetch the message body and feed the trainer."""
        if not self.cfg.train_spam_on_junk_arrival:
            return False
        subject, body = self._fetch_text(
            client, account, account.resolved_junk_folder, uid
        )
        if subject is None:
            logger.warning(
                "[training] Could not fetch Junk message %s/%s", account.email, uid
            )
            return False
        self.trainer.report(subject, body, account.email, is_spam=True)
        self._log_feedback(
            account.email, message_id or str(uid), "spam", source="junk_idler"
        )
        logger.info(
            "[training] Trained spam: %s uid=%s msg=%s",
            account.email,
            uid,
            message_id or "?",
        )
        self.audit.emit(
            "train",
            account=account.email,
            uid=uid,
            message_id=message_id,
            label="spam",
            source="junk_idler",
        )
        return True

    def _train_ham(
        self, client: IMAPClient, account: AccountConfig, uid: int, message_id: str
    ) -> bool:
        """Train as ham: fetch the message body and feed the trainer."""
        if not self.cfg.train_ham_on_junk_to_inbox:
            return False
        subject, body = self._fetch_text(client, account, "INBOX", uid)
        if subject is None:
            logger.warning(
                "[training] Could not fetch INBOX message %s/%s", account.email, uid
            )
            return False
        self.trainer.report(subject, body, account.email, is_spam=False)
        self._log_feedback(
            account.email, message_id or str(uid), "ham", source="junk_to_inbox"
        )
        logger.info(
            "[training] Trained ham: %s uid=%s msg=%s",
            account.email,
            uid,
            message_id or "?",
        )
        self.audit.emit(
            "train",
            account=account.email,
            uid=uid,
            message_id=message_id,
            label="ham",
            source="junk_to_inbox",
        )
        return True

    def _fetch_text(
        self, client: IMAPClient, account: AccountConfig, folder: str, uid: int
    ) -> tuple[str | None, str]:
        """Fetch one message's subject + plaintext body on the scan's
        existing connection.

        Returns ``(subject, body)`` or ``(None, "")`` on failure.
        """
        try:
            messages = client.fetch_uids(folder, [uid])
        except Exception as exc:
            logger.warning(
                "[training] fetch failed %s/%s uid=%s: %s",
                account.email,
                folder,
                uid,
                exc,
            )
            return None, ""
        if not messages:
            return None, ""
        msg = messages[0]
        return (msg.get("subject") or "", msg.get("body") or "")

    def _log_feedback(
        self, account_email: str, message_id: str, feedback: str, source: str
    ) -> None:
        """Record a training event in ``spam_feedback`` (dedup per label+source)."""
        now = datetime.now(UTC).isoformat()
        with self._db.transaction() as conn:
            existing = conn.execute(
                "SELECT feedback FROM spam_feedback "
                "WHERE account_email = ? AND message_uuid = ? AND source = ?",
                (account_email, message_id, source),
            ).fetchone()
            if existing:
                return  # already trained this label+source — no double count
            conn.execute(
                "INSERT INTO spam_feedback "
                "(uuid, message_uuid, account_email, feedback, source, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    str(uuid_mod.uuid4()),
                    message_id,
                    account_email,
                    feedback,
                    source,
                    now,
                ),
            )

    # ── State persistence (folder UID snapshots) ──────────────────────

    def _load_state(self, account_email: str, folder: str) -> dict[int, str]:
        row = self._db.execute_one(
            "SELECT known_uids FROM folder_state "
            "WHERE account_email = ? AND folder = ?",
            (account_email, folder),
        )
        if not row:
            return {}
        try:
            return {int(k): v for k, v in json.loads(row["known_uids"]).items()}
        except (ValueError, TypeError):
            return {}

    def _save_state(
        self, account_email: str, folder: str, uids: dict[int, str]
    ) -> None:
        now = datetime.now(UTC).isoformat()
        payload = json.dumps({str(k): v for k, v in uids.items()})
        self._db.execute(
            "INSERT OR REPLACE INTO folder_state "
            "(account_email, folder, known_uids, updated_at) VALUES (?, ?, ?, ?)",
            (account_email, folder, payload, now),
        )

    def _is_daemon_move(self, account_email: str, uid: int, message_id: str) -> bool:
        """Check whether a Junk message was moved there by the daemon.

        Matches by Message-ID first — after an IMAP MOVE the message
        gets a *new* UID in the destination folder, so UID equality
        alone cannot identify daemon moves.  UID matching is kept as a
        fallback for messages without a Message-ID header.
        """
        if message_id:
            row = self._db.execute_one(
                "SELECT 1 AS x FROM daemon_moves "
                "WHERE account_email = ? AND message_id = ?",
                (account_email, message_id),
            )
            if row is not None:
                return True
        row = self._db.execute_one(
            "SELECT 1 AS x FROM daemon_moves WHERE account_email = ? AND imap_uid = ?",
            (account_email, uid),
        )
        return row is not None


__all__ = ["TrainingIdler"]
