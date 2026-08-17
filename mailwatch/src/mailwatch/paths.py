"""Path resolution for mailwatch.

Replaces ``lightercore.paths`` in the vendored lighterbird modules.

``config_dir`` holds *classifier state* (Bayesian per-user tokens) —
NOT the config file, whose path is passed explicitly via ``--config``.
It must be writable at runtime: on systemd deployments point
``MAILWATCH_CONFIG_DIR`` at the data dir (e.g. ``/var/lib/mailwatch``),
never ``/etc`` (read-only under ``ProtectSystem=strict``).

``data_dir`` holds the SQLite database + audit log.

Both are overridable via environment variables so tests and the
systemd service can place them anywhere.
"""

from __future__ import annotations

import os
from pathlib import Path

_DEFAULT_CONFIG_DIR = "~/.config/mailwatch"
_DEFAULT_DATA_DIR = "~/.local/share/mailwatch"

# Environment override — used by tests (tmp_path) and the systemd unit.
_CONFIG_DIR_ENV = "MAILWATCH_CONFIG_DIR"
_DATA_DIR_ENV = "MAILWATCH_DATA_DIR"


def config_dir() -> Path:
    """Return the mailwatch config directory, creating it if needed."""
    path = Path(os.environ.get(_CONFIG_DIR_ENV, _DEFAULT_CONFIG_DIR)).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def data_dir() -> Path:
    """Return the mailwatch data directory, creating it if needed."""
    path = Path(os.environ.get(_DATA_DIR_ENV, _DEFAULT_DATA_DIR)).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


__all__ = ["config_dir", "data_dir"]
