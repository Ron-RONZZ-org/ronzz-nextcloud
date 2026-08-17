"""The mailwatch classification pipeline.

Wired by ``watcher.py``: on an IDLE notification (or a catch-up scan),
``MailwatchPipeline.process_account()`` connects to the account's IMAP,
finds new (UNSEEN) messages, runs the three detectors, decides, and
acts:

1. **Bayesian** (seed corpus + per-user tokens) — ``SpamClassifier``
2. **Phishing** (OpenPhish/PhishTank/PhishStats feeds + brand spoof) —
   ``PhishingDetector``
3. **Similarity** (MinHash near-duplicates + content hashes) —
   ``SpamSimilarityDetector``

Decision mirrors lighterbird's ``_classify_new_messages``: the combined
score is the max of Bayesian and similarity; phishing is orthogonal and
forces the spam action.  Spam is MOVE'd to the Junk folder via UID MOVE
(RFC 6851, visible to all clients).  Repeat-offender domains feed the
optional Sieve auto-blocker.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from typing import Any

from mailwatch.audit import AuditLog
from mailwatch.config import AccountConfig, DaemonConfig
from mailwatch.db import MailwatchDB
from mailwatch.email.filters import (
    PhishingDetector,
    SpamClassifier,
    SpamSimilarityDetector,
)
from mailwatch.email.filters.spam_similarity import _extract_domain
from mailwatch.email.imap.client import IMAPClient
from mailwatch.keyring import get_password
from mailwatch.sieve_block import SieveAutoBlocker

logger = logging.getLogger(__name__)


class AccountLock:
    """Per-account lock preventing concurrent IMAP operations."""

    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def acquire(self, account_email: str, timeout: float = 30.0) -> bool:
        with self._guard:
            lock = self._locks.setdefault(account_email, threading.Lock())
        if not lock.acquire(timeout=timeout):
            logger.warning(
                "[lock] Could not acquire IMAP lock for %s within %.1fs",
                account_email,
                timeout,
            )
            return False
        return True

    def release(self, account_email: str) -> None:
        with self._guard:
            lock = self._locks.get(account_email)
        if lock:
            try:
                lock.release()
            except RuntimeError:
                pass


class ClassificationResult:
    """Result of classifying a single message."""

    __slots__ = (
        "bayesian_score",
        "combined_score",
        "from_addr",
        "is_phishing",
        "is_spam",
        "message_id",
        "phishing_score",
        "reasons",
        "similarity_score",
        "subject",
        "uid",
    )

    def __init__(
        self,
        uid: int,
        message_id: str,
        from_addr: str,
        subject: str,
        bayesian_score: float,
        phishing_score: float,
        similarity_score: float,
        combined_score: float,
        is_spam: bool,
        is_phishing: bool,
        reasons: list[str],
    ) -> None:
        self.uid = uid
        self.message_id = message_id
        self.from_addr = from_addr
        self.subject = subject
        self.bayesian_score = bayesian_score
        self.phishing_score = phishing_score
        self.similarity_score = similarity_score
        self.combined_score = combined_score
        self.is_spam = is_spam
        self.is_phishing = is_phishing
        self.reasons = reasons

    @property
    def scores(self) -> dict[str, float]:
        return {
            "bayesian": round(self.bayesian_score, 4),
            "phishing": round(self.phishing_score, 4),
            "similarity": round(self.similarity_score, 4),
            "combined": round(self.combined_score, 4),
        }


class MailwatchPipeline:
    """Classify + act on new messages for a single account."""

    def __init__(
        self,
        db: MailwatchDB,
        cfg: DaemonConfig,
        audit: AuditLog,
        blocker: SieveAutoBlocker,
    ) -> None:
        self._db = db
        self.cfg = cfg
        self.audit = audit
        self.blocker = blocker
        self.classifier = SpamClassifier()
        self.phishing = PhishingDetector(db)
        self.similarity = SpamSimilarityDetector(db)

    # ── Public entry ──────────────────────────────────────────────────

    def process_account(
        self,
        account: AccountConfig,
        folder: str = "INBOX",
        password: str | None = None,
    ) -> int:
        """Classify and act on new messages for one account+folder.

        Args:
            account: Account configuration.
            folder: Folder to scan (normally INBOX).
            password: IMAP password.  If None, read from the keyring.

        Returns:
            Number of messages processed.
        """
        pw = password if password is not None else get_password(account.email)
        if not pw:
            logger.warning(
                "[pipeline] No password for %s — set it in the keyring "
                "(mailwatch/email/%s)",
                account.email,
                account.email,
            )
            return 0

        client = IMAPClient(account.imap_host, account.imap_port, account.imap_use_ssl)
        try:
            client.connect(account.imap_username, pw)
        except ConnectionError as exc:
            logger.warning("[pipeline] Cannot connect %s: %s", account.email, exc)
            return 0

        try:
            return self._scan_unseen(client, account, folder)
        finally:
            client.disconnect()

    # ── Internals ─────────────────────────────────────────────────────

    def _scan_unseen(
        self, client: IMAPClient, account: AccountConfig, folder: str
    ) -> int:
        """Find UNSEEN messages in *folder*, classify and act on each."""
        ok, _uidvalidity, _modseq = client.select_folder_ex(folder, readonly=True)
        if not ok:
            logger.warning("[pipeline] Cannot SELECT %s for %s", folder, account.email)
            return 0

        unseen = self._search_unseen(client)
        if not unseen:
            return 0
        logger.info(
            "[pipeline] %d new message(s) for %s/%s", len(unseen), account.email, folder
        )

        messages = client.fetch_uids(folder, unseen)
        processed = 0
        for msg in messages:
            msg["account_email"] = account.email
            msg["folder_name"] = folder
            try:
                result = self._classify(msg)
            except Exception as exc:
                logger.warning(
                    "[pipeline] Classification failed for UID %s: %s",
                    msg.get("imap_uid"),
                    exc,
                )
                continue

            action = self._act(client, account, msg, result)
            self.audit.classification(
                account=account.email,
                folder=folder,
                uid=result.uid,
                message_id=result.message_id,
                from_addr=result.from_addr,
                subject=result.subject,
                scores=result.scores,
                is_spam=result.is_spam,
                is_phishing=result.is_phishing,
                reasons=result.reasons,
                action=action,
            )
            processed += 1
        return processed

    def _search_unseen(self, client: IMAPClient) -> list[int]:
        """Return UIDs of UNSEEN messages in the currently selected folder."""
        try:
            typ, data = client.conn.uid("SEARCH", "UNSEEN")
        except Exception as exc:
            logger.warning("[pipeline] UID SEARCH UNSEEN failed: %s", exc)
            return []
        if typ != "OK" or not data or not data[0]:
            return []
        return [int(uid) for uid in data[0].split()]

    def _classify(self, msg: dict[str, Any]) -> ClassificationResult:
        """Run the three detectors on a parsed message."""
        uid = int(msg.get("imap_uid", 0))
        message_id = msg.get("message_id", "") or str(uid)
        from_addr = msg.get("from_addr", "") or ""
        subject = msg.get("subject", "") or ""
        body = msg.get("body", "") or ""
        html_body = msg.get("html_body") or None
        account_email = msg.get("account_email") or ""

        bayesian = self.classifier.classify(subject, body, account_email)
        phish = self.phishing.analyze(
            from_addr,
            subject,
            html_body,
            body,
            account_email,
        )
        sim = self.similarity.check_similarity(subject, body, account_email)

        combined = max(
            bayesian.get("score", 0.0),
            sim.get("score", 0.0),
        )
        is_spam = combined >= self.cfg.spam_threshold or bool(
            phish.get("is_phishing", False)
        )

        return ClassificationResult(
            uid=uid,
            message_id=message_id,
            from_addr=from_addr,
            subject=subject,
            bayesian_score=float(bayesian.get("score", 0.0)),
            phishing_score=float(phish.get("score", 0.0)),
            similarity_score=float(sim.get("score", 0.0)),
            combined_score=float(combined),
            is_spam=is_spam,
            is_phishing=bool(phish.get("is_phishing", False)),
            reasons=list(phish.get("reasons", []) or []),
        )

    def _act(
        self,
        client: IMAPClient,
        account: AccountConfig,
        msg: dict[str, Any],
        result: ClassificationResult,
    ) -> str:
        """Take the configured action for a classified message.

        Returns a short action string for the audit log:
        ``"move_junk"``, ``"move_junk_phishing"``, ``"none"``.
        """
        if not result.is_spam:
            return "none"

        is_phishing = result.is_phishing
        junk_folder = account.resolved_junk_folder
        source_folder = msg.get("folder_name") or "INBOX"

        if self.cfg.dry_run:
            logger.info(
                "[pipeline] [dry-run] would move UID %s (%s) from %s to %s "
                "(spam=%.3f phishing=%s)",
                result.uid,
                result.message_id,
                source_folder,
                junk_folder,
                result.combined_score,
                is_phishing,
            )
            # No DB mutation in dry-run: no daemon-move record, no
            # offender hits, no Sieve push.
            return "dry_run"

        # Record the move so the training idler can exclude it (bias guard).
        self._record_daemon_move(account.email, result.uid, result.message_id)

        moved = client.move_message(result.uid, source_folder, junk_folder)
        if not moved:
            logger.warning(
                "[pipeline] MOVE failed for UID %s (%s) to %s",
                result.uid,
                result.message_id,
                junk_folder,
            )
            return "move_failed"

        logger.info(
            "[pipeline] Moved UID %s (%s) to %s (spam=%.3f phishing=%s)",
            result.uid,
            result.message_id,
            junk_folder,
            result.combined_score,
            is_phishing,
        )
        self.audit.emit(
            "move",
            account=account.email,
            uid=result.uid,
            message_id=result.message_id,
            from_folder=source_folder,
            to_folder=junk_folder,
            is_phishing=is_phishing,
            combined_score=round(result.combined_score, 4),
        )

        self._maybe_register_offender(account, result, apply=True)
        return "move_junk_phishing" if is_phishing else "move_junk"

    # ── Repeat-offender tracking ──────────────────────────────────────

    def _sender_domain(self, result: ClassificationResult) -> str | None:
        return _extract_domain(result.from_addr)

    def _maybe_register_offender(
        self, account: AccountConfig, result: ClassificationResult, apply: bool
    ) -> None:
        """Track spam hits per sender domain; auto-block at the threshold.

        Only runs when ``daemon.auto_block.enabled`` is true and the
        account has ManageSieve configured.  *apply* is False in
        dry-run mode (register the hit, never push).
        """
        ab = self.cfg.auto_block
        if not ab.enabled:
            return
        domain = self._sender_domain(result)
        if not domain or not account.has_sieve:
            return

        hits = self.blocker.register_hit(account.email, domain)
        if hits < ab.hits_threshold:
            return

        # Threshold reached — check window before blocking.
        recent = self.blocker.count_recent_hits(account.email, domain, ab.window_days)
        if recent < ab.hits_threshold:
            return

        already = any(d == domain for d in self.blocker.list_blocked(account.email))
        if already:
            return

        self.blocker.record_block(account.email, domain)
        self.audit.emit(
            "sieve_block",
            account=account.email,
            sender_domain=domain,
            hits=recent,
            dry_run=self.cfg.dry_run,
        )

        if apply:
            pw = get_password(account.email)
            if pw:
                pushed = self.blocker.push_block_script(
                    account, pw, dry_run=self.cfg.dry_run
                )
                if pushed:
                    self.audit.emit(
                        "sieve_block_pushed",
                        account=account.email,
                        sender_domain=domain,
                    )

    # ── Training exclusion bookkeeping ────────────────────────────────

    def _record_daemon_move(
        self, account_email: str, uid: int, message_id: str
    ) -> None:
        """Persist a daemon-initiated Junk move for the training idler."""
        now = datetime.now(UTC).isoformat()
        self._db.execute(
            "INSERT OR REPLACE INTO daemon_moves "
            "(account_email, imap_uid, message_id, moved_at) VALUES (?, ?, ?, ?)",
            (account_email, uid, message_id, now),
        )

    # ── Feed refresh ──────────────────────────────────────────────────

    def refresh_phishing_feeds(self) -> dict[str, int]:
        """Download and upsert the phishing feeds.

        Returns a dict mapping feed name → new domain count.
        """
        from mailwatch.email.filters import PhishingFeedUpdater

        updater = PhishingFeedUpdater(self._db)
        totals = updater.update_all()
        self.audit.emit("feed_update", totals=totals)
        return totals
