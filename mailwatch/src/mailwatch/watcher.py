"""mailwatch — headless IMAP IDLE spam/idler daemon (entrypoint).

Usage::

    mailwatch --config /etc/mailwatch/config.toml            # daemon mode
    mailwatch --once --config ...                            # single pass
    mailwatch password set me@ronzz.org                      # keyring setup

Signals: SIGTERM/SIGINT trigger a graceful shutdown (IDLE threads
stopped, DB closed).
"""

from __future__ import annotations

import argparse
import fcntl
import logging
import os
import signal
import sys
import threading
from pathlib import Path

from mailwatch.audit import AuditLog
from mailwatch.config import ConfigError, MailwatchConfig, load_config
from mailwatch.db import MailwatchDB, get_db
from mailwatch.email.imap.idle import IMAPIdleManager
from mailwatch.keyring import get_password, set_password
from mailwatch.pipeline import AccountLock, MailwatchPipeline
from mailwatch.sieve_block import SieveAutoBlocker
from mailwatch.training import TrainingIdler

logger = logging.getLogger("mailwatch")


# ── Single-instance lock ────────────────────────────────────────────────


class SingleInstanceLock:
    """Advisory flock-based single-instance guard."""

    def __init__(self, lock_path: str | Path) -> None:
        self.lock_path = Path(lock_path)
        self._fh: object | None = None

    def acquire(self) -> bool:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        # Keep the handle open for the lock's lifetime — closing it would
        # release the flock.  noqa: SIM115 (deliberate non-context open).
        fh = open(self.lock_path, "w")  # noqa: SIM115
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is not None:
            try:
                fcntl.flock(self._fh, fcntl.LOCK_UN)
            except OSError:
                pass
            self._fh.close()
            self._fh = None


# ── Logging ─────────────────────────────────────────────────────────────


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


# ── Daemon core ─────────────────────────────────────────────────────────


class MailwatchDaemon:
    """Wires IDLE manager + pipeline + training + schedulers."""

    def __init__(self, cfg: MailwatchConfig, *, once: bool = False) -> None:
        self.cfg = cfg
        self.once = once
        self.db: MailwatchDB = get_db()
        self.audit = AuditLog(dry_run=cfg.daemon.dry_run)
        self.blocker = SieveAutoBlocker(self.db, cfg.daemon.auto_block)
        self.pipeline = MailwatchPipeline(
            self.db,
            cfg.daemon,
            self.audit,
            self.blocker,
        )
        self.training = TrainingIdler(self.db, cfg.daemon.training, self.audit)
        self.idle = IMAPIdleManager()
        self.locks = AccountLock()
        self._stop = threading.Event()
        self._scheduler_threads: list[threading.Thread] = []

    # ── Lifecycle ─────────────────────────────────────────────────────

    def run(self) -> int:
        """Start everything and block until stopped (or finish --once)."""
        if self.once:
            return self._run_once()

        self._register_signal_handlers()
        self._start_idle_threads()
        self._start_scheduler_threads()
        self.audit.emit(
            "startup", accounts=self.cfg.account_emails, dry_run=self.cfg.daemon.dry_run
        )
        logger.info(
            "mailwatch started: %d account(s), dry_run=%s, training=%s, auto_block=%s",
            len(self.cfg.accounts),
            self.cfg.daemon.dry_run,
            self.cfg.daemon.training.enabled,
            self.cfg.daemon.auto_block.enabled,
        )

        try:
            while not self._stop.wait(1.0):
                pass
        except KeyboardInterrupt:
            pass
        finally:
            self._shutdown()
        return 0

    def _shutdown(self) -> None:
        logger.info("Shutting down…")
        self._stop.set()
        self.idle.stop_all()
        self.audit.emit("shutdown")

    def _register_signal_handlers(self) -> None:
        def _handle(signum: int, _frame: object) -> None:
            logger.info("Received signal %d", signum)
            self._stop.set()

        signal.signal(signal.SIGTERM, _handle)
        signal.signal(signal.SIGINT, _handle)

    # ── IDLE threads ──────────────────────────────────────────────────

    def _start_idle_threads(self) -> None:
        for account in self.cfg.accounts:
            pw = get_password(account.email)
            if not pw:
                logger.warning(
                    "No password in keyring for %s — IDLE not started. "
                    "Run: mailwatch password set %s",
                    account.email,
                    account.email,
                )
                continue
            self.idle.start_for_account(
                account_email=account.email,
                host=account.imap_host,
                port=account.imap_port,
                use_ssl=account.imap_use_ssl,
                username=account.imap_username,
                password=pw,
                on_notification=self._on_inbox_notification,
                folder="INBOX",
            )
            if self.cfg.daemon.training.enabled:
                self.idle.start_for_account(
                    account_email=account.email,
                    host=account.imap_host,
                    port=account.imap_port,
                    use_ssl=account.imap_use_ssl,
                    username=account.imap_username,
                    password=pw,
                    on_notification=self._on_junk_notification,
                    folder=account.resolved_junk_folder,
                )

    def _on_inbox_notification(
        self, account_email: str, folder: str, event_type: str
    ) -> None:
        """IDLE callback for INBOX — classify new messages."""
        if event_type != "exists":
            return
        account = self._find_account(account_email)
        if account is None:
            return
        if not self.locks.acquire(account_email):
            return
        try:
            self.pipeline.process_account(account, folder="INBOX")
        except Exception as exc:
            logger.warning(
                "[watcher] INBOX processing failed for %s: %s", account_email, exc
            )
        finally:
            self.locks.release(account_email)

    def _on_junk_notification(
        self, account_email: str, folder: str, event_type: str
    ) -> None:
        """IDLE callback for Junk — run a training reconciliation scan."""
        if event_type != "exists":
            return
        account = self._find_account(account_email)
        if account is None:
            return
        if not self.locks.acquire(account_email):
            return
        try:
            summary = self.training.scan_account(account)
            if summary.get("spam") or summary.get("ham"):
                logger.info("[training] %s: %s", account_email, summary)
        except Exception as exc:
            logger.warning("[watcher] Junk scan failed for %s: %s", account_email, exc)
        finally:
            self.locks.release(account_email)

    def _find_account(self, account_email: str):
        for account in self.cfg.accounts:
            if account.email.lower() == account_email.lower():
                return account
        return None

    # ── Scheduler threads ─────────────────────────────────────────────

    def _start_scheduler_threads(self) -> None:
        # Catch-up INBOX scans (guards against missed IDLE notifications).
        if self.cfg.daemon.catch_up_scan_seconds > 0:
            self._spawn(self._catch_up_loop, "catch-up-scan")

        # Periodic training reconciliation (detects Junk→INBOX ham moves).
        if self.cfg.daemon.training.enabled:
            self._spawn(self._training_loop, "training-reconcile")

        # Phishing feed refresh.
        if self.cfg.daemon.feed_refresh_hours > 0:
            self._spawn(self._feed_loop, "feed-refresh")

    def _spawn(self, target, name: str) -> None:
        t = threading.Thread(target=target, name=name, daemon=True)
        t.start()
        self._scheduler_threads.append(t)

    def _catch_up_loop(self) -> None:
        interval = self.cfg.daemon.catch_up_scan_seconds
        while not self._stop.wait(interval):
            for account in self.cfg.accounts:
                if not get_password(account.email):
                    continue
                if not self.locks.acquire(account.email, timeout=1.0):
                    continue
                try:
                    self.pipeline.process_account(account, folder="INBOX")
                except Exception as exc:
                    logger.warning(
                        "[watcher] Catch-up scan failed for %s: %s", account.email, exc
                    )
                finally:
                    self.locks.release(account.email)

    def _training_loop(self) -> None:
        interval = self.cfg.daemon.training.scan_interval_seconds
        while not self._stop.wait(interval):
            for account in self.cfg.accounts:
                if not get_password(account.email):
                    continue
                if not self.locks.acquire(account.email, timeout=1.0):
                    continue
                try:
                    summary = self.training.scan_account(account)
                    if summary.get("spam") or summary.get("ham"):
                        logger.info("[training] %s: %s", account.email, summary)
                except Exception as exc:
                    logger.warning(
                        "[watcher] Training scan failed for %s: %s", account.email, exc
                    )
                finally:
                    self.locks.release(account.email)

    def _feed_loop(self) -> None:
        interval = self.cfg.daemon.feed_refresh_hours * 3600
        # First refresh shortly after startup (never block startup on it).
        self._stop.wait(30)
        while not self._stop.is_set():
            try:
                totals = self.pipeline.refresh_phishing_feeds()
                logger.info("[feeds] Refreshed phishing feeds: %s", totals)
            except Exception as exc:
                logger.warning("[feeds] Refresh failed: %s", exc)
            self._stop.wait(interval)

    # ── --once mode ───────────────────────────────────────────────────

    def _run_once(self) -> int:
        self.audit.emit(
            "startup",
            accounts=self.cfg.account_emails,
            once=True,
            dry_run=self.cfg.daemon.dry_run,
        )
        logger.info(
            "Running single pass over %d account(s) (dry_run=%s)",
            len(self.cfg.accounts),
            self.cfg.daemon.dry_run,
        )
        total = 0
        for account in self.cfg.accounts:
            if not self.locks.acquire(account.email):
                continue
            try:
                n = self.pipeline.process_account(account, folder="INBOX")
                total += n
                if self.cfg.daemon.training.enabled:
                    summary = self.training.scan_account(account)
                    logger.info("[training] %s: %s", account.email, summary)
            except Exception as exc:
                logger.warning("Account %s failed: %s", account.email, exc)
            finally:
                self.locks.release(account.email)
        self.audit.emit("shutdown", once=True)
        logger.info("Single pass complete: %d message(s) processed", total)
        return 0


# ── Password subcommand ─────────────────────────────────────────────────


def _cmd_password(args: argparse.Namespace) -> int:
    email = args.email.lower().strip()
    if args.action == "set":
        import getpass

        password = getpass.getpass(f"Password for {email}: ")
        if not password:
            print("Empty password — aborting", file=sys.stderr)
            return 1
        if set_password(email, password):
            print(f"Password stored in keyring for {email}")
            return 0
        print(
            "Failed to store password (keyring backend unavailable?)", file=sys.stderr
        )
        return 1
    elif args.action == "check":
        pw = get_password(email)
        if pw:
            print(f"Password present for {email}")
            return 0
        print(f"No password in keyring for {email}", file=sys.stderr)
        return 1
    return 1


# ── CLI ────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mailwatch",
        description="Headless IMAP IDLE spam/idler daemon (Migadu).",
    )
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="Run the daemon (default).")
    run.add_argument(
        "--config",
        default=None,
        help="Path to config.toml (default /etc/mailwatch/config.toml)",
    )
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="Classify + log only; never move messages or push Sieve",
    )
    run.add_argument(
        "--once",
        action="store_true",
        help="Run a single pass over all accounts, then exit",
    )
    run.add_argument("--debug", action="store_true", help="Debug logging")

    pw = sub.add_parser("password", help="Manage keyring passwords")
    pw.add_argument("action", choices=["set", "check"])
    pw.add_argument("email", help="Account email")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "password":
        return _cmd_password(args)

    # Default command: run
    dry_run = bool(getattr(args, "dry_run", False))
    once = bool(getattr(args, "once", False))
    debug = bool(getattr(args, "debug", False))

    try:
        cfg = load_config(getattr(args, "config", None))
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if dry_run:
        cfg.daemon.dry_run = True
    if debug:
        cfg.daemon.log_level = "DEBUG"
    _setup_logging(cfg.daemon.log_level)

    # Single-instance lock (skip in --once mode: short-lived).
    if not once:
        from mailwatch.paths import data_dir

        lock = SingleInstanceLock(data_dir() / "mailwatch.lock")
        if not lock.acquire():
            print(
                "Another mailwatch instance is already running (lock held) — exiting.",
                file=sys.stderr,
            )
            return 1
    else:
        lock = None

    try:
        daemon = MailwatchDaemon(cfg, once=once)
        return daemon.run()
    finally:
        if lock is not None:
            lock.release()


if __name__ == "__main__":
    sys.exit(main())
