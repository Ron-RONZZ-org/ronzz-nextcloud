#!/usr/bin/env python3
"""mailwatch live end-to-end test.

Exercises the daemon's four behaviors against a **real** IMAP/SMTP
account (requires the daemon running + the mailbox password in the
keyring):

  1. Spam filtering + MOVE  — a synthetic spam message must be
     classified (score >= threshold) and UID-MOVE'd to Junk.
  2. Ham filtering          — a synthetic neutral message must stay in
     INBOX (audit action ``none``).
  3. Spam training          — a message moved to Junk *not by the daemon*
     (simulated gateway/user junk mark) trains the Bayesian classifier
     (``spam_feedback`` source ``junk_idler``).
  4. Ham training           — the same message moved back to INBOX
     ("not spam") trains ham (``spam_feedback`` source ``junk_to_inbox``).
  5. Bias guard             — the daemon-moved message from step 1 must
     NOT appear in ``spam_feedback`` (no self-confirmation).

All synthetic messages carry the ``mailwatch-e2e-*`` Message-ID marker
and are deleted from the mailbox in the cleanup step; their
``spam_feedback`` / ``daemon_moves`` rows are purged too.  No other
mailbox content is touched.

Safety: refuses to run unless ``MAILWATCH_E2E=1`` is set, and requires
an explicit ``--account``.

Usage (on the server, as the mailwatch user):

    sudo -u mailwatch HOME=/var/lib/mailwatch MAILWATCH_E2E=1 \\
        /opt/mailwatch/venv/bin/python /opt/mailwatch/e2e/live_e2e.py \\
        --account ron@ronzz.org

Exit code: 0 = all steps passed, 1 = a step failed, 2 = usage/safety error.
"""

from __future__ import annotations

import argparse
import json
import os
import smtplib
import sqlite3
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from mailwatch.email.imap.client import IMAPClient
from mailwatch.keyring import get_password

MARKER = "mailwatch-e2e"
DEFAULT_TIMEOUT = 180  # seconds per wait step
DEFAULT_SCAN_INTERVAL = 120  # daemon daemon.training.scan_interval_seconds


class StepFailed(Exception):
    """A single E2E step failed (message included)."""


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def log(msg: str) -> None:
    print(f"[{datetime.now(UTC).strftime('%H:%M:%S')}] {msg}", flush=True)


# ── IMAP helpers ─────────────────────────────────────────────────────────


def _client(account: str, imap_host: str, imap_port: int, pw: str) -> IMAPClient:
    c = IMAPClient(imap_host, imap_port, use_ssl=True)
    c.connect(account, pw)
    return c


def _search_msg(c: IMAPClient, folder: str, message_id: str) -> list[bytes]:
    return c.search_by_header(folder, "Message-ID", message_id)


def _msg_in_folder(c: IMAPClient, folder: str, message_id: str) -> bool:
    return bool(_search_msg(c, folder, message_id))


def _delete_by_message_id(c: IMAPClient, folder: str, message_id: str) -> int:
    """Delete messages in *folder* matching *message_id*; returns count."""
    uids = _search_msg(c, folder, message_id)
    for uid in uids:
        c.conn.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
    if uids:
        c.conn.expunge()
    return len(uids)


def _move_by_message_id(c: IMAPClient, src: str, dst: str, message_id: str) -> bool:
    """Move the first message matching *message_id* between folders."""
    uids = _search_msg(c, src, message_id)
    if not uids:
        return False
    c.move_message(int(uids[0]), src, dst)
    return True


def _send(
    account: str,
    smtp_host: str,
    smtp_port: int,
    pw: str,
    message_id: str,
    subject: str,
    body: str,
) -> None:
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = account
    msg["To"] = account
    msg["Message-ID"] = message_id
    s = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30)
    try:
        s.login(account, pw)
        s.send_message(msg)
    finally:
        s.quit()


# ── Wait helpers ─────────────────────────────────────────────────────────


def poll(desc: str, timeout: float, interval: float, fn: Callable[[], Any]) -> Any:
    """Call *fn* until truthy or *timeout* seconds elapse."""
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    raise StepFailed(
        f"timed out after {timeout:.0f}s waiting for {desc} (last result: {last!r})"
    )


def _audit_events(audit_path: Path, message_id: str) -> list[dict[str, Any]]:
    """Return audit events mentioning *message_id*."""
    if not audit_path.exists():
        return []
    events = []
    try:
        with open(audit_path, encoding="utf-8") as f:
            for line in f:
                if message_id not in line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return events


# ── Database helpers ─────────────────────────────────────────────────────


def _db_rows(db_path: Path, table: str, message_id: str) -> list[dict[str, Any]]:
    """Select *message_id* rows from spam_feedback or daemon_moves."""
    if not db_path.exists():
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        col = "message_uuid" if table == "spam_feedback" else "message_id"
        rows = conn.execute(
            f"SELECT * FROM {table} WHERE {col} = ?",
            (message_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _purge_rows(db_path: Path, message_id: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "DELETE FROM spam_feedback WHERE message_uuid = ?",
            (message_id,),
        )
        conn.execute(
            "DELETE FROM daemon_moves WHERE message_id = ?",
            (message_id,),
        )
        conn.commit()
    finally:
        conn.close()


# ── The steps ────────────────────────────────────────────────────────────


def run(args: argparse.Namespace) -> None:
    account = args.account
    pw = get_password(account)
    if not pw:
        raise StepFailed(
            f"no password in keyring for {account} — run "
            f"'mailwatch password set {account}'"
        )
    scan_interval = args.scan_interval
    results: list[tuple[str, str]] = []

    def step(name: str, fn: Callable[[], None]) -> None:
        log(f"== {name}")
        try:
            fn()
            results.append((name, "PASS"))
            log("   -> PASS")
        except StepFailed as exc:
            results.append((name, f"FAIL: {exc}"))
            log(f"   -> FAIL: {exc}")

    # Synthetic message-ids (unique per run).
    run_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    spam_id = f"{MARKER}-spam-{run_id}@ronzz.org"
    ham_id = f"{MARKER}-ham-{run_id}@ronzz.org"

    spam_subject = "URGENT: cheap pills 50% OFF - limited offer act now"
    spam_body = (
        "URGENT ACTION REQUIRED! Buy cheap pills now - limited offer discount "
        "pharmacy pills cheap viagra cialis free shipping. Click here to claim "
        "your free prize money transfer wire bank account. Act now before it expires!"
    )
    ham_subject = "Weekly team meeting minutes"
    ham_body = (
        "Please find attached the minutes of the weekly team meeting and the "
        "agenda for next week's planning session. Regards, the office."
    )

    try:
        # ── 1. Spam filtering + MOVE ─────────────────────────────────
        def step1() -> None:
            log(f"   sending spam message {spam_id}")
            _send(
                account,
                args.smtp_host,
                args.smtp_port,
                pw,
                spam_id,
                spam_subject,
                spam_body,
            )
            log("   waiting for the daemon to MOVE it to Junk…")

            def moved() -> bool:
                c = _client(account, args.imap_host, args.imap_port, pw)
                try:
                    return _msg_in_folder(c, "Junk", spam_id)
                finally:
                    c.disconnect()

            poll("spam message in Junk", args.timeout, 5, moved)

            events = _audit_events(Path(args.audit), spam_id)
            moves = [e for e in events if e.get("event") == "move"]
            if not moves:
                raise StepFailed("audit has no 'move' event for the spam message")
            if moves[-1].get("to_folder") != "Junk":
                raise StepFailed(
                    f"move went to {moves[-1].get('to_folder')!r}, expected 'Junk'"
                )
            cls = [e for e in events if e.get("event") == "classification"]
            if not cls or cls[-1].get("action") != "move_junk":
                raise StepFailed("no classification with action=move_junk")
            log(
                f"   audit: spam={cls[-1].get('scores', {}).get('bayesian')} "
                f"action={cls[-1].get('action')} -> Junk"
            )

        step("1. Spam filtering + MOVE to Junk", step1)

        # ── 2. Ham filtering (stays in INBOX) ────────────────────────
        def step2() -> None:
            log(f"   sending ham message {ham_id}")
            _send(
                account,
                args.smtp_host,
                args.smtp_port,
                pw,
                ham_id,
                ham_subject,
                ham_body,
            )
            log("   waiting for the daemon to classify it…")

            def classified() -> list[dict[str, Any]]:
                return _audit_events(Path(args.audit), ham_id)

            events = poll(
                "ham classification in audit", args.timeout, 5, lambda: classified()
            )
            cls = [e for e in events if e.get("event") == "classification"]
            if not cls:
                raise StepFailed("no classification event for the ham message")
            if cls[-1].get("action") != "none":
                raise StepFailed(
                    f"ham message action={cls[-1].get('action')!r}, "
                    "expected 'none' (must stay in INBOX)"
                )
            c = _client(account, args.imap_host, args.imap_port, pw)
            try:
                still_here = _msg_in_folder(c, "INBOX", ham_id)
            finally:
                c.disconnect()
            if not still_here:
                raise StepFailed("ham message left INBOX despite action=none")
            log(
                f"   audit: spam={cls[-1].get('scores', {}).get('bayesian')} "
                "action=none, still in INBOX"
            )

        step("2. Ham filtering (stays in INBOX)", step2)

        # ── 3. Spam training (non-daemon Junk arrival) ───────────────
        def step3() -> None:
            c = _client(account, args.imap_host, args.imap_port, pw)
            try:
                if not _move_by_message_id(c, "INBOX", "Junk", ham_id):
                    raise StepFailed(
                        "could not move ham message to Junk "
                        "(simulated gateway junk mark)"
                    )
            finally:
                c.disconnect()
            log(
                "   moved ham message INBOX -> Junk (not by the daemon); "
                "waiting for the training idler…"
            )

            def trained_spam() -> bool:
                rows = _db_rows(Path(args.db), "spam_feedback", ham_id)
                return any(
                    r.get("feedback") == "spam" and r.get("source") == "junk_idler"
                    for r in rows
                )

            poll(
                "spam_feedback spam row",
                timeout=args.timeout,
                interval=5,
                fn=trained_spam,
            )
            log("   trained spam (source=junk_idler)")

        step("3. Spam training (non-daemon Junk arrival)", step3)

        # ── 4. Ham training (Junk -> INBOX = "not spam") ─────────────
        def step4() -> None:
            c = _client(account, args.imap_host, args.imap_port, pw)
            try:
                if not _move_by_message_id(c, "Junk", "INBOX", ham_id):
                    raise StepFailed("could not move ham message back to INBOX")
            finally:
                c.disconnect()
            log(
                f"   moved ham message Junk -> INBOX; waiting for the "
                f"reconciliation scan (up to ~{3 * scan_interval}s)…"
            )

            def trained_ham() -> bool:
                rows = _db_rows(Path(args.db), "spam_feedback", ham_id)
                return any(
                    r.get("feedback") == "ham" and r.get("source") == "junk_to_inbox"
                    for r in rows
                )

            poll(
                "spam_feedback ham row",
                timeout=max(args.timeout, 3 * scan_interval + 30),
                interval=10,
                fn=trained_ham,
            )
            log("   trained ham (source=junk_to_inbox)")

        step("4. Ham training (Junk -> INBOX)", step4)

        # ── 5. Bias guard ────────────────────────────────────────────
        def step5() -> None:
            rows = _db_rows(Path(args.db), "spam_feedback", spam_id)
            if rows:
                raise StepFailed(
                    f"daemon-moved message was trained ({rows}) — bias guard failed"
                )
            log("   daemon-moved spam message has NO spam_feedback rows")

        step("5. Bias guard (daemon moves never train)", step5)

    finally:
        # ── Cleanup: synthetic messages + their rows ─────────────────
        log("== cleanup")
        c = _client(account, args.imap_host, args.imap_port, pw)
        try:
            for mid in (spam_id, ham_id):
                for folder in ("INBOX", "Junk"):
                    n = _delete_by_message_id(c, folder, mid)
                    if n:
                        log(f"   deleted {n} message(s) {mid} from {folder}")
        finally:
            c.disconnect()
        _purge_rows(Path(args.db), spam_id)
        _purge_rows(Path(args.db), ham_id)
        log("   purged spam_feedback/daemon_moves rows")

    log("\n========== RESULTS ==========")
    failed = 0
    for name, status in results:
        log(f"{'PASS' if status == 'PASS' else 'FAIL':4}  {name}")
        if status != "PASS":
            failed += 1
    if failed:
        raise StepFailed(f"{failed} step(s) failed — see above")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mailwatch-live-e2e",
        description="Live end-to-end test for the mailwatch daemon.",
    )
    parser.add_argument(
        "--account",
        required=True,
        help="Mailbox to test against (keyring password required)",
    )
    parser.add_argument("--imap-host", default="imap.migadu.com")
    parser.add_argument("--imap-port", type=int, default=993)
    parser.add_argument("--smtp-host", default="smtp.migadu.com")
    parser.add_argument("--smtp-port", type=int, default=465)
    parser.add_argument("--db", default="/var/lib/mailwatch/mailwatch.db")
    parser.add_argument("--audit", default="/var/lib/mailwatch/audit.jsonl")
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help="Per-step wait timeout in seconds",
    )
    parser.add_argument(
        "--scan-interval",
        type=int,
        default=DEFAULT_SCAN_INTERVAL,
        help="daemon.training.scan_interval_seconds of the "
        "running daemon (drives the ham-step wait)",
    )
    args = parser.parse_args(argv)

    if os.environ.get("MAILWATCH_E2E") != "1":
        print(
            "Refusing to run: set MAILWATCH_E2E=1 to confirm this is a "
            "deliberate live test against a real mailbox.",
            file=sys.stderr,
        )
        return 2

    try:
        run(args)
    except StepFailed as exc:
        print(f"\nE2E FAILED: {exc}", file=sys.stderr)
        return 1
    print("\nE2E PASSED — all steps green.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
