"""Tests for the daemon entrypoint: CLI, single-instance lock, --once mode."""

from __future__ import annotations

import sys
from pathlib import Path

from mailwatch.watcher import SingleInstanceLock, main


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
