# ruff: noqa: S110
"""Sieve filter management (vendored from lighterbird @ 99baa8ef). — local CRUD + remote ManageSieve sync.

Forked from A-lien's ``sieve.py``. Sieve scripts are stored locally
and optionally synced to a ManageSieve-capable server.
"""

from __future__ import annotations

import socket
import ssl
from typing import Any

# ── Local validation ────────────────────────────────────────────────────────


def validate_sieve(content: str) -> tuple[bool, str]:
    """Validate Sieve script syntax locally using sievelib.

    Args:
        content: Sieve script source.

    Returns:
        ``(is_valid, error_message)`` tuple.
    """
    try:
        from sievelib.parser import Parser
    except ImportError:
        return True, ""  # skip validation if library not installed

    p = Parser()
    if p.parse(content):
        return True, ""
    return False, p.error


# ── Remote management ───────────────────────────────────────────────────────


class SieveManager:
    """Thin wrapper around managesieve protocol (RFC 5804).

    Connects to a ManageSieve server to list, upload, download,
    delete, and activate Sieve scripts.
    """

    def __init__(self, host: str, port: int = 4190, use_tls: bool = True):
        self.host = host
        self.port = port
        self.use_tls = use_tls
        self._client: Any = None

    def connect(self, username: str, password: str) -> None:
        """Connect and authenticate to ManageSieve server.

        Args:
            username: Authentication username.
            password: Authentication password.

        Raises:
            ConnectionError: If connection or login fails.
        """
        try:
            from managesieve import MANAGESIEVE as SieveClient
        except ImportError:
            raise ConnectionError(
                "managesieve library not installed. Install: pip install managesieve"
            )

        try:
            self._client = SieveClient(self.host, self.port, use_tls=self.use_tls)
            login_ok = self._client.login("", username, password)
            if login_ok != "OK":
                reason = self._client.response_text or login_ok
                raise ConnectionError(f"Sieve login failed for {username}: {reason}")
        except (
            socket.gaierror,
            ConnectionRefusedError,
            TimeoutError,
            ssl.SSLError,
            OSError,
        ) as e:
            raise ConnectionError(
                f"Sieve connection failed to {username}@{self.host}:{self.port} — {e}"
            ) from e
        except ConnectionError:
            raise
        except Exception as e:
            raise ConnectionError(
                f"Sieve connection failed to {username}@{self.host}:{self.port} — {e}"
            ) from e

    def disconnect(self) -> None:
        if self._client:
            try:
                self._client.logout()
            except Exception:
                pass
            self._client = None

    def list_scripts(self) -> list[dict[str, str]]:
        """List Sieve scripts on the server.

        Returns:
            List of dicts with ``name`` and ``active`` keys.
        """
        if not self._client:
            raise RuntimeError("Not connected. Call connect() first.")
        raw = self._client.listscripts()
        if isinstance(raw, tuple) and len(raw) >= 2:
            raw = raw[1]  # ('OK', [...]) → [...]
        scripts = raw or []
        if not isinstance(scripts, list):
            return []
        return [
            {"name": s[0], "active": bool(s[1]) if len(s) > 1 else False}
            for s in scripts
            if isinstance(s, (list, tuple)) and len(s) >= 1
        ]

    def get_script(self, name: str) -> str:
        """Download a Sieve script from the server.

        Args:
            name: Script name.

        Returns:
            Sieve script content as string.
        """
        if not self._client:
            raise RuntimeError("Not connected. Call connect() first.")
        result = self._client.getscript(name)
        # managesieve library returns ('OK', content) tuple for getscript
        if isinstance(result, tuple) and len(result) >= 2:
            return str(result[1]) if result[1] else ""
        if isinstance(result, tuple) and len(result) >= 1:
            return str(result[0]) if result[0] else ""
        return str(result) if result else ""

    def put_script(self, name: str, content: str) -> None:
        """Upload a Sieve script to the server."""
        if not self._client:
            raise RuntimeError("Not connected. Call connect() first.")
        self._client.putscript(name, content)

    def delete_script(self, name: str) -> None:
        """Delete a Sieve script from the server."""
        if not self._client:
            raise RuntimeError("Not connected. Call connect() first.")
        self._client.deletescript(name)

    def activate_script(self, name: str) -> None:
        """Activate a Sieve script on the server."""
        if not self._client:
            raise RuntimeError("Not connected. Call connect() first.")
        self._client.setactive(name)

    def __enter__(self) -> SieveManager:
        return self

    def __exit__(self, *args: Any) -> None:
        self.disconnect()


__all__ = ["SieveManager", "validate_sieve"]
