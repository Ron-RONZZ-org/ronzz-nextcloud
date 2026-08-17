"""Redirector — send-to-hesk folder → destination mailbox (e.g. Hesk inbox).

Watches a per-account source folder (default ``send-to-hesk``) and
redirects every message found there into a destination mailbox,
**verbatim**: the raw RFC822 bytes are appended unchanged, so headers
(From, Message-ID, Date, To) survive intact.  Downstream consumers —
e.g. Hesk's IMAP email intake watching ``hi@ronzz.org/INBOX`` — then
attribute the ticket to the *original* sender, not the forwarding user.

        The source message is deleted (or moved to ``after_move_folder``)
        only after the append succeeds; on failure it stays in place and
        is retried on the next pass, so a redirect never loses mail.

        The appended message is left **unread** (no ``\\Seen`` flag) —
        downstream consumers (Hesk's IMAP intake) fetch *unseen* mail
        only, so the redirected message must arrive as new/unread.

The redirector runs in two ways (both wired in ``watcher.py``):
  * IDLE callback: an IMAP IDLE thread on the source folder fires
    ``redirect_account()`` the moment a message appears (EXISTS).
  * Catch-up scan: the daemon's periodic scan re-runs it so a missed
    notification never leaves a message stuck in an empty-looking
    folder.
"""

from __future__ import annotations

import logging

from mailwatch.audit import AuditLog
from mailwatch.config import AccountConfig, RedirectConfig
from mailwatch.email.imap.client import IMAPClient
from mailwatch.keyring import get_password

logger = logging.getLogger(__name__)


class Redirector:
    """Redirect messages from a source folder into a target mailbox.

    Args:
        account: The source account (carries ``redirect`` config).
        audit: Audit log for JSON-lines trail.
        dry_run: If True, log intentions but never append/delete.
    """

    def __init__(
        self,
        account: AccountConfig,
        audit: AuditLog,
        *,
        dry_run: bool = False,
    ) -> None:
        self.account = account
        self.redirect: RedirectConfig = account.redirect
        self.audit = audit
        self.dry_run = dry_run

    # ── Public API ───────────────────────────────────────────────────

    def redirect_account(self) -> int:
        """Redirect all messages currently in the source folder.

        Returns:
            Number of messages successfully redirected.
        """
        target = self.redirect.target
        if target is None or not self.redirect.enabled:
            return 0

        src_pw = get_password(self.account.email)
        if not src_pw:
            logger.warning(
                "[redirect] No source password in keyring for %s — redirect skipped",
                self.account.email,
            )
            return 0
        dst_pw = get_password(target.email)
        if not dst_pw:
            logger.warning(
                "[redirect] No destination password in keyring for %s — redirect skipped",
                target.email,
            )
            return 0

        src_client = IMAPClient(
            self.account.imap_host, self.account.imap_port, self.account.imap_use_ssl
        )
        dst_client = IMAPClient(
            target.imap_host, target.imap_port, target.imap_use_ssl
        )
        try:
            src_client.connect(self.account.imap_username, src_pw)
            dst_client.connect(target.imap_username, dst_pw)
        except ConnectionError as exc:
            logger.warning("[redirect] Connection failed: %s", exc)
            return 0

        try:
            uids = self._list_source_uids(src_client)
            if not uids:
                return 0
            logger.info(
                "[redirect] %s/%s: %d message(s) to redirect",
                self.account.email,
                self.redirect.source_folder,
                len(uids),
            )

            ok_count = 0
            for uid in uids:
                raw = src_client.fetch_raw(uid, self.redirect.source_folder)
                if raw is None:
                    logger.warning("[redirect] fetch failed for UID %s", uid)
                    continue
                if self.dry_run:
                    self.audit.emit(
                        "redirect",
                        event_detail="dry-run",
                        account=self.account.email,
                        source_folder=self.redirect.source_folder,
                        target=target.email,
                        destination_folder=target.destination_folder,
                        uid=uid,
                        ok=False,
                    )
                    logger.info(
                        "[redirect] [dry-run] would redirect UID %s (%d bytes) to %s",
                        uid,
                        len(raw),
                        target.email,
                    )
                    continue

                appended = dst_client.append_message(
                    target.destination_folder, raw
                )
                if not appended:
                    logger.warning(
                        "[redirect] APPEND to %s/%s failed for UID %s — leaving in place",
                        target.email,
                        target.destination_folder,
                        uid,
                    )
                    self.audit.emit(
                        "redirect",
                        event_detail="append_failed",
                        account=self.account.email,
                        source_folder=self.redirect.source_folder,
                        target=target.email,
                        destination_folder=target.destination_folder,
                        uid=uid,
                        ok=False,
                    )
                    continue

                # Append OK — remove the source message.
                self._remove_source(src_client, uid)
                ok_count += 1
                self.audit.emit(
                    "redirect",
                    event_detail="ok",
                    account=self.account.email,
                    source_folder=self.redirect.source_folder,
                    target=target.email,
                    destination_folder=target.destination_folder,
                    uid=uid,
                    ok=True,
                )
                logger.info(
                    "[redirect] UID %s → %s/%s",
                    uid,
                    target.email,
                    target.destination_folder,
                )

            return ok_count
        finally:
            src_client.disconnect()
            dst_client.disconnect()

    # ── Helpers ──────────────────────────────────────────────────────

    def _list_source_uids(self, client: IMAPClient) -> list[int]:
        """Return all UIDs currently in the source folder (any flags)."""
        ok, _uidvalidity, _modseq = client.select_folder_ex(
            self.redirect.source_folder, readonly=True
        )
        if not ok:
            logger.warning(
                "[redirect] cannot select source folder %r for %s",
                self.redirect.source_folder,
                self.account.email,
            )
            return []
        try:
            typ, data = client.conn.uid("SEARCH", "ALL")
        except Exception as exc:
            logger.warning("[redirect] SEARCH ALL failed: %s", exc)
            return []
        if typ != "OK" or not data or not data[0]:
            return []
        return [int(b) for b in data[0].split()]

    def _remove_source(self, client: IMAPClient, uid: int) -> bool:
        """Delete or archive the source message after a successful append."""
        after = self.redirect.after_move_folder
        if after:
            return client.move_message(uid, self.redirect.source_folder, after)
        # Delete: mark \Deleted in the source folder and expunge.
        if not client.set_flags(uid, self.redirect.source_folder, add=["\\Deleted"]):
            return False
        try:
            client.conn.expunge()
            return True
        except Exception as exc:
            logger.warning("[redirect] expunge failed for UID %s: %s", uid, exc)
            return False


__all__ = ["Redirector"]
