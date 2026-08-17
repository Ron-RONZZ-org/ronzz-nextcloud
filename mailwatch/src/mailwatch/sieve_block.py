"""Sieve auto-block for repeat-offender senders.

Pushes a server-side Sieve ``reject`` script via ManageSieve (RFC 5804)
so blocked domains are dropped before they reach any client.

Migration note (vs lighterbird): lighterbird's ``SpamManager.to_sieve()``
matches on the ``envelope`` extension, which community reports say is
unusable on Migadu (Postfix and Dovecot run on separate servers — see
issue #2 open questions).  mailwatch therefore generates **header-based**
rules using the core Sieve ``address`` test, which works on any
Sieve-capable server.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from mailwatch.config import AccountConfig, AutoBlockConfig
from mailwatch.email.filters.sieve import SieveManager

logger = logging.getLogger(__name__)


def _quote_reject_message(text: str) -> str:
    """Escape a string for embedding in a Sieve ``reject`` argument."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def generate_block_script(domains: list[str]) -> str:
    """Generate a Sieve script rejecting the given domains.

    Uses ``require ["reject", "address"]`` with the core ``address``
    test (RFC 5228 §2.7.3) — no ``envelope`` dependency.

    Args:
        domains: Domains to block (``example.com`` matches any
                 ``*@example.com``).

    Returns:
        Sieve script source (empty string if *domains* is empty).
    """
    if not domains:
        return ""

    parts: list[str] = ['require ["reject", "address"];', ""]

    for domain in sorted(set(domains)):
        parts.append(
            f'if address :domain :contains "from" "{domain}" {{\n'
            f'    reject "Blocked domain: {_quote_reject_message(domain)}";\n'
            f"}}"
        )

    return "\n".join(parts) + "\n"


class SieveAutoBlocker:
    """Pushes a combined block script for one account.

    The script is regenerated from the ``block_candidates`` table rows
    with ``blocked_at IS NOT NULL`` and re-uploaded/activated whenever
    a new block is added.
    """

    def __init__(self, db: Any, cfg: AutoBlockConfig) -> None:
        self._db = db
        self.cfg = cfg

    # ── DB access ─────────────────────────────────────────────────────

    def list_blocked(self, account_email: str) -> list[str]:
        """Return domains currently blocked for an account."""
        rows = self._db.execute(
            "SELECT sender_domain FROM block_candidates "
            "WHERE account_email = ? AND blocked_at IS NOT NULL",
            (account_email,),
        )
        return [row["sender_domain"] for row in rows]

    def register_hit(self, account_email: str, sender_domain: str) -> int:
        """Record a spam hit for a sender domain.

        Returns the updated hit count.
        """
        now = datetime.now(UTC).isoformat()
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT spam_hits, first_seen FROM block_candidates "
                "WHERE account_email = ? AND sender_domain = ?",
                (account_email, sender_domain),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO block_candidates "
                    "(account_email, sender_domain, spam_hits, first_seen, last_seen, blocked_at) "
                    "VALUES (?, ?, 1, ?, ?, NULL)",
                    (account_email, sender_domain, now, now),
                )
                return 1
            hits = int(row["spam_hits"]) + 1
            conn.execute(
                "UPDATE block_candidates SET spam_hits = ?, last_seen = ? "
                "WHERE account_email = ? AND sender_domain = ?",
                (hits, now, account_email, sender_domain),
            )
            return hits

    def record_block(self, account_email: str, sender_domain: str) -> None:
        """Mark a domain as blocked (blocked_at set)."""
        now = datetime.now(UTC).isoformat()
        self._db.execute(
            "UPDATE block_candidates SET blocked_at = ? "
            "WHERE account_email = ? AND sender_domain = ?",
            (now, account_email, sender_domain),
        )

    def count_recent_hits(
        self, account_email: str, sender_domain: str, window_days: int
    ) -> int:
        """Count spam hits within the last *window_days* days."""
        cutoff = (datetime.now(UTC) - timedelta(days=window_days)).isoformat()
        row = self._db.execute_one(
            "SELECT spam_hits FROM block_candidates "
            "WHERE account_email = ? AND sender_domain = ? AND last_seen >= ?",
            (account_email, sender_domain, cutoff),
        )
        return row["spam_hits"] if row else 0

    # ── Sieve push ────────────────────────────────────────────────────

    def push_block_script(
        self, account: AccountConfig, password: str, dry_run: bool = False
    ) -> bool:
        """Regenerate and push the block script for one account.

        Returns True on success (or when dry-run — nothing pushed).
        """
        domains = self.list_blocked(account.email)
        script = generate_block_script(domains)
        if not script.strip():
            # Nothing to block — skip (do not upload an empty script).
            return True

        if dry_run:
            logger.info(
                "[sieve] [dry-run] would push %s for %s (%d domain(s))",
                self.cfg.script_name,
                account.email,
                len(domains),
            )
            return True

        try:
            manager = SieveManager(
                account.sieve_host,
                account.sieve_port,
                account.sieve_use_tls,
            )
            manager.connect(account.imap_username, password)
            try:
                manager.put_script(self.cfg.script_name, script)
                manager.activate_script(self.cfg.script_name)
            finally:
                manager.disconnect()
            logger.info(
                "[sieve] Pushed %s for %s (%d domain(s))",
                self.cfg.script_name,
                account.email,
                len(domains),
            )
            return True
        except Exception as exc:
            logger.warning("[sieve] Push failed for %s: %s", account.email, exc)
            return False


__all__ = ["SieveAutoBlocker", "generate_block_script"]
