"""The live E2E script must stay runnable (syntax + CLI intact)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "e2e" / "live_e2e.py"


def test_live_e2e_script_help():
    """--help exits 0 (script imports and parses arguments)."""
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "--account" in result.stdout
    assert "Live end-to-end test" in result.stdout


def test_live_e2e_script_refuses_without_env():
    """Without MAILWATCH_E2E=1 the script must refuse (exit 2)."""
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--account", "me@ronzz.org"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert "MAILWATCH_E2E" in result.stderr
