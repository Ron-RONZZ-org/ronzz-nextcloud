"""Tests for the daemon entrypoint: CLI, single-instance lock, --once mode."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

from mailwatch.config import (
    AccountConfig,
    ConnectionConfig,
    DaemonConfig,
    MailwatchConfig,
    TrainingConfig,
)
from mailwatch.errors import EXIT_AUTH_FAILURE, IMAPAuthError, IMAPConnectionError
from mailwatch.watcher import MailwatchDaemon, SingleInstanceLock, main


def _write_config(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(content)
    return p


MINIMAL = """
[[accounts]]
email = "me@ronzz.org"
"""


def test_password_command_missing_email(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["mailwatch", "password", "check", ""])
    assert main(["password", "check", ""]) == 1


def test_run_requires_config(tmp_path, monkeypatch):
    monkeypatch.setattr(
        sys, "argv", ["mailwatch", "run", "--config", str(tmp_path / "none.toml")]
    )
    assert main(["run", "--config", str(tmp_path / "none.toml")]) == 2


def test_run_once_mode(tmp_path, monkeypatch):
    cfg = _write_config(tmp_path, MINIMAL)
    # No keyring password → each account is skipped gracefully.
    rc = main(["run", "--config", str(cfg), "--once"])
    assert rc == 0


def test_single_instance_lock_exclusive(tmp_path):
    lock1 = SingleInstanceLock(tmp_path / "mailwatch.lock")
    lock2 = SingleInstanceLock(tmp_path / "mailwatch.lock")
    assert lock1.acquire() is True
    assert lock2.acquire() is False  # second instance refused
    lock1.release()
    assert lock2.acquire() is True
    lock2.release()


# ── Failure handling: auth → stop, transient → back off ────────────────


def _daemon(
    tmp_path: Path, monkeypatch, cfg: MailwatchConfig | None = None
) -> MailwatchDaemon:
    monkeypatch.setenv("MAILWATCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MAILWATCH_CONFIG_DIR", str(tmp_path))
    return MailwatchDaemon(
        cfg
        or MailwatchConfig(accounts=[AccountConfig(email="me@ronzz.org")])
    )


def test_auth_failure_stops_daemon(tmp_path, monkeypatch):
    daemon = _daemon(tmp_path, monkeypatch)
    daemon._on_auth_failure("me@ronzz.org", IMAPAuthError("bad password"))
    assert daemon._stop.is_set()
    assert daemon._exit_code == EXIT_AUTH_FAILURE
    assert '"auth_failure"' in (tmp_path / "audit.jsonl").read_text()


def test_auth_failure_backs_off_when_stop_disabled(tmp_path, monkeypatch):
    cfg = MailwatchConfig(
        daemon=DaemonConfig(connection=ConnectionConfig(stop_on_auth_failure=False)),
        accounts=[AccountConfig(email="me@ronzz.org")],
    )
    daemon = _daemon(tmp_path, monkeypatch, cfg)
    daemon._on_auth_failure("me@ronzz.org", IMAPAuthError("bad"))
    assert not daemon._stop.is_set()
    assert daemon._exit_code == 0
    assert daemon._backoff.consecutive_failures("me@ronzz.org") == 1


def test_inbox_notification_auth_failure_stops(tmp_path, monkeypatch):
    daemon = _daemon(tmp_path, monkeypatch)

    def boom(account, folder="INBOX"):
        raise IMAPAuthError("bad")

    daemon.pipeline.process_account = boom  # type: ignore[method-assign]
    daemon._on_inbox_notification("me@ronzz.org", "INBOX", "exists")
    assert daemon._exit_code == EXIT_AUTH_FAILURE


def test_inbox_notification_connection_failure_backs_off(tmp_path, monkeypatch):
    daemon = _daemon(tmp_path, monkeypatch)

    def boom(account, folder="INBOX"):
        raise IMAPConnectionError("timed out")

    daemon.pipeline.process_account = boom  # type: ignore[method-assign]
    daemon._on_inbox_notification("me@ronzz.org", "INBOX", "exists")
    assert not daemon._stop.is_set()
    assert daemon._backoff.consecutive_failures("me@ronzz.org") == 1


def test_training_loop_stops_on_auth_failure(tmp_path, monkeypatch):
    cfg = MailwatchConfig(
        daemon=DaemonConfig(
            training=TrainingConfig(enabled=True, scan_interval_seconds=1)
        ),
        accounts=[AccountConfig(email="me@ronzz.org")],
    )
    daemon = _daemon(tmp_path, monkeypatch, cfg)
    monkeypatch.setattr("mailwatch.watcher.get_password", lambda email: "pw")

    calls = {"n": 0}

    def boom(account, password=None):
        calls["n"] += 1
        raise IMAPAuthError("bad")

    daemon.training.scan_account = boom  # type: ignore[method-assign]
    thread = threading.Thread(target=daemon._training_loop, daemon=True)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive(), "training loop must stop on auth failure"
    assert daemon._exit_code == EXIT_AUTH_FAILURE
    assert calls["n"] == 1


def test_run_once_auth_failure_returns_exit_code(tmp_path, monkeypatch):
    cfg_path = _write_config(
        tmp_path,
        """
[[accounts]]
email = "me@ronzz.org"
""",
    )
    monkeypatch.setenv("MAILWATCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MAILWATCH_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("mailwatch.watcher.get_password", lambda email: "pw")
    monkeypatch.setattr(
        "mailwatch.watcher.MailwatchPipeline.process_account",
        lambda self, account, folder="INBOX": (_ for _ in ()).throw(
            IMAPAuthError("bad")
        ),
    )
    assert main(["run", "--config", str(cfg_path), "--once"]) == EXIT_AUTH_FAILURE
