"""Shared test fixtures for mailwatch.

Adds ``src/`` to ``sys.path`` so tests can import the package without
installation, and redirects config/data dirs to temp directories.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Make ``src/mailwatch`` importable regardless of how pytest is invoked.
_MAILWATCH_ROOT = Path(__file__).resolve().parent
_SRC = _MAILWATCH_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.fixture(autouse=True)
def _isolated_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point config/data dirs at a temp dir for every test.

    Directories are created lazily by ``config_dir()``/``data_dir()``
    (and by individual test fixtures) — do not pre-create them here or
    vendored fixtures calling ``mkdir()`` will hit FileExistsError.
    """
    cfg = tmp_path / "config"
    data = tmp_path / "data"
    monkeypatch.setenv("MAILWATCH_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("MAILWATCH_DATA_DIR", str(data))
    return cfg
