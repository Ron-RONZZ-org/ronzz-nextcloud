"""SQLite database for mailwatch.

Schema subset of lighterbird's ``email/db.py`` (phishing feeds,
spam feedback, MinHash similarity state) plus mailwatch-specific
tables (daemon moves, block candidates).  No accounts/folders/messages
tables — accounts live in the TOML config, messages are classified on
the fly and never persisted.

The wrapper mirrors the small surface the vendored filters need:
``execute``, ``execute_one``, ``transaction``.  Connections are cached
per-thread (each mailwatch thread gets its own connection), WAL mode
is enabled, and writes are serialized with a cross-thread lock so
detectors and training never race.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

# ── Schema (adapted from lighterbird email/db.py) ───────────────────────

_CREATE_PHISHING_FEEDS = """
CREATE TABLE IF NOT EXISTS phishing_feeds (
    domain      TEXT NOT NULL,
    source      TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    PRIMARY KEY (domain, source)
);
"""

_CREATE_PHISHING_DOMAINS = """
CREATE TABLE IF NOT EXISTS phishing_domains (
    domain          TEXT NOT NULL,
    brand           TEXT,
    reason          TEXT NOT NULL DEFAULT '',
    reported_at     TEXT NOT NULL,
    account_email   TEXT NOT NULL,
    PRIMARY KEY (domain, account_email)
);
"""

# message_uuid stores the raw Message-ID (no messages table in mailwatch).
_CREATE_SPAM_FEEDBACK = """
CREATE TABLE IF NOT EXISTS spam_feedback (
    uuid            TEXT PRIMARY KEY,
    message_uuid    TEXT NOT NULL,
    account_email   TEXT NOT NULL,
    feedback        TEXT NOT NULL CHECK(feedback IN ('spam', 'ham', 'fraud')),
    source          TEXT NOT NULL DEFAULT 'manual',
    created_at      TEXT NOT NULL
);
"""

_CREATE_SPAM_SIGNATURES = """
CREATE TABLE IF NOT EXISTS spam_signatures (
    message_uuid    TEXT NOT NULL,
    account_email   TEXT NOT NULL,
    signature       BLOB NOT NULL,
    content_hash    TEXT NOT NULL,
    num_tokens      INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (message_uuid)
);
"""

_CREATE_SPAM_CONTENT_HASHES = """
CREATE TABLE IF NOT EXISTS spam_content_hashes (
    content_hash    TEXT NOT NULL,
    sender_domain   TEXT NOT NULL,
    account_email   TEXT NOT NULL,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    occurrence_count INTEGER NOT NULL DEFAULT 1,
    subject_snippet TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (content_hash, sender_domain, account_email)
);
"""

# ── mailwatch-specific tables ───────────────────────────────────────────

# Messages the daemon moved to Junk (training exclusion + audit).
_CREATE_DAEMON_MOVES = """
CREATE TABLE IF NOT EXISTS daemon_moves (
    account_email   TEXT NOT NULL,
    imap_uid        INTEGER NOT NULL,
    message_id      TEXT NOT NULL DEFAULT '',
    moved_at        TEXT NOT NULL,
    PRIMARY KEY (account_email, imap_uid)
);
"""

# Repeat-offender senders tracked for the optional Sieve auto-block.
_CREATE_BLOCK_CANDIDATES = """
CREATE TABLE IF NOT EXISTS block_candidates (
    account_email   TEXT NOT NULL,
    sender_domain   TEXT NOT NULL,
    spam_hits       INTEGER NOT NULL DEFAULT 1,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    blocked_at      TEXT,
    PRIMARY KEY (account_email, sender_domain)
);
"""

# Per-account last-seen IMAP state for the training idler (folder
# reconciliation).  Keyed by (account_email, folder).
_CREATE_FOLDER_STATE = """
CREATE TABLE IF NOT EXISTS folder_state (
    account_email   TEXT NOT NULL,
    folder          TEXT NOT NULL,
    known_uids      TEXT NOT NULL DEFAULT '[]',   -- JSON list of UIDs seen
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (account_email, folder)
);
"""

_SCHEMA = [
    _CREATE_PHISHING_FEEDS,
    _CREATE_PHISHING_DOMAINS,
    _CREATE_SPAM_FEEDBACK,
    _CREATE_SPAM_SIGNATURES,
    _CREATE_SPAM_CONTENT_HASHES,
    _CREATE_DAEMON_MOVES,
    _CREATE_BLOCK_CANDIDATES,
    _CREATE_FOLDER_STATE,
]

_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_phishing_feeds_domain ON phishing_feeds(domain);",
    "CREATE INDEX IF NOT EXISTS idx_spam_signatures_acct ON spam_signatures(account_email);",
    "CREATE INDEX IF NOT EXISTS idx_spam_content_hashes_acct ON spam_content_hashes(account_email);",
    "CREATE INDEX IF NOT EXISTS idx_spam_feedback_msg ON spam_feedback(message_uuid);",
]


class MailwatchDB:
    """Thread-safe SQLite wrapper with WAL mode and per-thread connections."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self._init_schema()

    # ── Connection management ──────────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    def _init_schema(self) -> None:
        conn = self._conn()
        with conn:
            for statement in _SCHEMA:
                conn.execute(statement)
            for statement in _INDEXES:
                conn.execute(statement)

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
            self._local.conn = None

    # ── Query API (used by vendored filters) ────────────────────────────

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """Execute a query and return all rows as dicts (empty if none).

        DML statements (INSERT/UPDATE/DELETE) are auto-committed so
        implicit transactions never linger on a per-thread connection
        (which would break explicit ``BEGIN`` inside ``transaction()``).
        """
        conn = self._conn()
        try:
            cur = conn.execute(sql, params)
        except sqlite3.OperationalError as exc:
            # WAL write contention or busy database — retry once.
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                conn.execute("PRAGMA busy_timeout=60000")
                cur = conn.execute(sql, params)
            else:
                raise
        rows = cur.fetchall()
        if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            conn.commit()
        return [dict(row) for row in rows]

    def execute_one(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> dict[str, Any] | None:
        """Execute a query and return the first row as dict, or None."""
        conn = self._conn()
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def transaction(self) -> MailwatchDB._Transaction:
        """Context manager for a write transaction (cross-thread serialized)."""
        return self._Transaction(self)

    class _Transaction:
        def __init__(self, db: MailwatchDB) -> None:
            self._db = db
            self._lock: threading.Lock | None = None

        def __enter__(self) -> sqlite3.Connection:
            # Serialize writes across threads; per-thread connections would
            # otherwise deadlock on SQLite's database lock.
            self._lock = self._db._write_lock
            self._lock.acquire()
            conn = self._db._conn()
            conn.execute("BEGIN IMMEDIATE")
            return conn

        def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
            conn = self._db._conn()
            try:
                if exc_type is None:
                    conn.execute("COMMIT")
                else:
                    conn.execute("ROLLBACK")
            finally:
                if self._lock is not None:
                    self._lock.release()


def get_db(path: str | Path | None = None) -> MailwatchDB:
    """Create (or reuse the process-wide) MailwatchDB at *path*.

    Defaults to ``data_dir()/mailwatch.db``.
    """
    from mailwatch.paths import data_dir

    if path is None:
        path = data_dir() / "mailwatch.db"
    return MailwatchDB(path)


__all__ = ["MailwatchDB", "get_db"]
