# ruff: noqa: S110, S112, B007
"""IMAP IDLE manager — push notification via RFC 2177.

Vendored from lighterbird's ``imap/idle.py`` (commit 99baa8ef) and
adapted for mailwatch: ``start_for_account`` now accepts a ``folder``
parameter so the daemon can watch both INBOX and the Junk folder
(training idler) with the same manager.

Usage::

    idle_mgr = IMAPIdleManager()
    idle_mgr.start_for_account(
        account_email="user@example.com",
        host="imap.migadu.com", port=993, use_ssl=True,
        username="user", password="...",
        folder="INBOX",
        on_notification=my_callback,
    )
    ...
    idle_mgr.stop_all()
"""

from __future__ import annotations

import logging
import select
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# Maximum time to stay in IDLE before re-issuing (29 min, RFC 2177 recommends < 30)
_IDLE_TIMEOUT = 29 * 60

# Exponential backoff for reconnection
_BASE_RETRY_DELAY = 30  # seconds
_MAX_RETRY_DELAY = 300  # 5 minutes
_MAX_RECONNECT_ATTEMPTS = 10

# How often to check for stop event while in IDLE (seconds)
_IDLE_POLL_INTERVAL = 30.0


class IMAPIdleThread:
    """Single-account IDLE thread.

    Connects to the IMAP server, SELECTs *folder*, and enters IDLE loop
    using RFC 2177.  On server push notifications (EXISTS, FLAGS), calls
    the callback.  Handles reconnection with exponential backoff.

    Args:
        account_email: Account to monitor.
        host: IMAP server host.
        port: IMAP server port.
        use_ssl: Whether to use SSL.
        username: IMAP username.
        password: IMAP password.
        folder: Folder to watch (default ``"INBOX"``).
        on_notification: Callback ``(account_email, folder, event_type)``
                         where event_type is ``'exists'`` or ``'flags'``.
    """

    def __init__(
        self,
        account_email: str,
        host: str,
        port: int,
        use_ssl: bool,
        username: str,
        password: str,
        on_notification: Callable[[str, str, str], None],
        folder: str = "INBOX",
    ):
        self.account_email = account_email
        self.folder = folder
        self._host = host
        self._port = port
        self._use_ssl = use_ssl
        self._username = username
        self._password = password
        self._on_notification = on_notification
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._last_heartbeat: float = 0.0
        self._reconnect_count = 0
        self._connected = False

    def start(self) -> None:
        """Start the IDLE thread."""
        if self._thread and self._thread.is_alive():
            logger.warning(
                "[idle] Thread already running for %s/%s",
                self.account_email,
                self.folder,
            )
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f"idle-{self.account_email[:16]}-{self.folder[:8]}",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "[idle] Started IDLE thread for %s/%s", self.account_email, self.folder
        )

    def stop(self, timeout: float = 3.0) -> None:
        """Signal the IDLE thread to stop and join.

        Args:
            timeout: Max seconds to wait for the thread to finish.
        """
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning(
                    "[idle] Thread for %s/%s did not stop within %.1fs",
                    self.account_email,
                    self.folder,
                    timeout,
                )
        logger.info(
            "[idle] Stopped IDLE thread for %s/%s", self.account_email, self.folder
        )

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def status(self) -> dict[str, Any]:
        return {
            "account_email": self.account_email,
            "folder": self.folder,
            "connected": self._connected,
            "alive": self.is_alive,
            "last_heartbeat": datetime.fromtimestamp(
                self._last_heartbeat, tz=UTC
            ).isoformat()
            if self._last_heartbeat
            else None,
            "reconnects": self._reconnect_count,
        }

    def _run(self) -> None:
        """Main IDLE loop with reconnection."""
        while not self._stop_event.is_set():
            try:
                self._run_idle_loop()
            except Exception as exc:
                logger.warning(
                    "[idle] IDLE loop error for %s/%s: %s",
                    self.account_email,
                    self.folder,
                    exc,
                )
            if self._stop_event.is_set():
                break
            # Exponential backoff before reconnect
            delay = min(
                _BASE_RETRY_DELAY * (2**self._reconnect_count),
                _MAX_RETRY_DELAY,
            )
            self._reconnect_count += 1
            if self._reconnect_count > _MAX_RECONNECT_ATTEMPTS:
                logger.error(
                    "[idle] Gave up on %s/%s after %d reconnection attempts",
                    self.account_email,
                    self.folder,
                    _MAX_RECONNECT_ATTEMPTS,
                )
                break
            logger.info(
                "[idle] Reconnecting %s/%s in %ds (attempt %d/%d)",
                self.account_email,
                self.folder,
                delay,
                self._reconnect_count,
                _MAX_RECONNECT_ATTEMPTS,
            )
            self._stop_event.wait(delay)

    def _run_idle_loop(self) -> None:
        """Proper RFC 2177 IDLE loop with push notifications.

        Connects, SELECTs *folder* (read-only), then repeatedly
        enters/exits IDLE to receive push notifications (EXISTS, FLAGS)
        from the server.  Uses ``select.select()`` to poll for data with
        a timeout so the thread can react to stop events.
        """
        from mailwatch.email.imap.client import IMAPClient

        client = IMAPClient(self._host, self._port, self._use_ssl)
        try:
            client.connect(self._username, self._password)
            self._connected = True
            self._reconnect_count = 0
            self._last_heartbeat = time.monotonic()

            if not client.capabilities.has_idle:
                logger.info(
                    "[idle] Server for %s/%s does not support IDLE, skipping",
                    self.account_email,
                    self.folder,
                )
                return

            ok, _uidvalidity, _modseq = client.select_folder_ex(
                self.folder,
                readonly=True,
            )
            if not ok:
                logger.warning(
                    "[idle] Cannot SELECT %s for %s",
                    self.folder,
                    self.account_email,
                )
                return

            while not self._stop_event.is_set():
                # ── Enter IDLE (RFC 2177) ──────────────────────────────
                # imaplib has NO IDLE support: ``_command("IDLE")`` raises
                # KeyError because "IDLE" is absent from imaplib.Commands
                # (and IMAP4 has no fileno() for select).  Send the command
                # manually and poll the raw socket.
                try:
                    tag = client.conn._new_tag()
                    client.conn.send(f"{tag} IDLE\r\n".encode("ascii"))
                except Exception:
                    logger.warning(
                        "[idle] Failed to enter IDLE for %s/%s",
                        self.account_email,
                        self.folder,
                    )
                    break

                idle_start = time.monotonic()

                # ── Process push notifications ─────────────────────────
                while not self._stop_event.is_set():
                    elapsed = time.monotonic() - idle_start
                    if elapsed >= _IDLE_TIMEOUT:
                        break  # Re-issue IDLE per RFC 2177

                    remaining = min(_IDLE_TIMEOUT - elapsed, _IDLE_POLL_INTERVAL)
                    try:
                        # Poll the raw socket (imaplib.IMAP4 has no
                        # fileno()); for SSL connections the underlying
                        # socket becomes readable when a TLS record with
                        # server data arrives.
                        r, _, _ = select.select([client.conn.sock], [], [], remaining)
                    except (TypeError, AttributeError):
                        # select() on SSL sockets may raise TypeError in some edge cases
                        break

                    if not r:
                        # Timeout with no data — check stop_event and loop
                        continue

                    # Server pushed data — read the untagged response(s)
                    try:
                        line = client.conn._get_line()
                    except Exception:
                        break

                    try:
                        line_str = line.decode("ascii", errors="replace")
                    except Exception:
                        continue

                    if "EXISTS" in line_str:
                        self._on_notification(
                            self.account_email,
                            self.folder,
                            "exists",
                        )
                    if "FLAGS" in line_str or "FETCH" in line_str:
                        self._on_notification(
                            self.account_email,
                            self.folder,
                            "flags",
                        )

                # ── Exit IDLE ──────────────────────────────────────────
                try:
                    client.conn.send(b"DONE\r\n")
                    typ, _data = client.conn._command_complete(tag, "IDLE")
                    # _data may contain untagged responses that accumulated
                    # before DONE was processed — check for notifications
                    if isinstance(_data, (list, tuple)):
                        resp_text = " ".join(
                            d.decode("ascii", errors="replace")
                            if isinstance(d, bytes)
                            else str(d)
                            for d in _data
                        )
                        if "EXISTS" in resp_text:
                            self._on_notification(
                                self.account_email,
                                self.folder,
                                "exists",
                            )
                        if "FLAGS" in resp_text or "FETCH" in resp_text:
                            self._on_notification(
                                self.account_email,
                                self.folder,
                                "flags",
                            )
                except Exception as exc:
                    logger.debug(
                        "[idle] IDLE exit for %s/%s: %s",
                        self.account_email,
                        self.folder,
                        exc,
                    )

                self._last_heartbeat = time.monotonic()

        except Exception as exc:
            logger.warning(
                "[idle] Connection error for %s/%s: %s",
                self.account_email,
                self.folder,
                exc,
            )
        finally:
            self._connected = False
            try:
                client.disconnect()
            except Exception:
                pass


class IMAPIdleManager:
    """Manage all per-account IDLE threads.

    Handles lifecycle: start, stop, status reporting.
    """

    def __init__(self):
        self._threads: dict[tuple[str, str], IMAPIdleThread] = {}
        self._lock = threading.Lock()

    def _key(self, account_email: str, folder: str) -> tuple[str, str]:
        return (account_email, folder)

    def start_for_account(
        self,
        account_email: str,
        host: str,
        port: int,
        use_ssl: bool,
        username: str,
        password: str,
        on_notification: Callable[[str, str, str], None],
        folder: str = "INBOX",
    ) -> bool:
        """Start an IDLE thread for an account+folder pair.

        Args:
            account_email: Account to monitor.
            host: IMAP server host.
            port: IMAP server port.
            use_ssl: Whether to use SSL.
            username: IMAP username.
            password: IMAP password.
            on_notification: Callback for notifications.
            folder: Folder to watch (default ``"INBOX"``).

        Returns:
            True if the thread was started, False if already running.
        """
        key = self._key(account_email, folder)
        with self._lock:
            existing = self._threads.get(key)
            if existing and existing.is_alive:
                logger.debug("[idle] Already running for %s/%s", account_email, folder)
                return False

            thread = IMAPIdleThread(
                account_email=account_email,
                host=host,
                port=port,
                use_ssl=use_ssl,
                username=username,
                password=password,
                on_notification=on_notification,
                folder=folder,
            )
            self._threads[key] = thread

        thread.start()
        return True

    def stop_for_account(self, account_email: str, folder: str = "INBOX") -> None:
        """Stop the IDLE thread for a specific account+folder."""
        key = self._key(account_email, folder)
        with self._lock:
            thread = self._threads.pop(key, None)
        if thread:
            thread.stop()

    def restart_for_account(
        self,
        account_email: str,
        host: str,
        port: int,
        use_ssl: bool,
        username: str,
        password: str,
        on_notification: Callable[[str, str, str], None],
        folder: str = "INBOX",
    ) -> bool:
        """Restart the IDLE thread for an account (stop then start)."""
        key = self._key(account_email, folder)
        with self._lock:
            existing = self._threads.pop(key, None)
        if existing:
            existing.stop(timeout=2.0)
        return self.start_for_account(
            account_email,
            host,
            port,
            use_ssl,
            username,
            password,
            on_notification,
            folder=folder,
        )

    def stop_all(self) -> None:
        """Stop all IDLE threads."""
        with self._lock:
            keys = list(self._threads.keys())
        for key in keys:
            self.stop_for_account(*key)

    @property
    def active_threads(self) -> list[tuple[str, str]]:
        """List (account_email, folder) pairs with active IDLE threads."""
        with self._lock:
            return list(self._threads.keys())

    def status_all(self) -> list[dict[str, Any]]:
        """Get status dict for all IDLE threads."""
        results = []
        with self._lock:
            for key, thread in list(self._threads.items()):
                results.append(thread.status)
        return results


__all__ = [
    "IMAPIdleManager",
    "IMAPIdleThread",
]
