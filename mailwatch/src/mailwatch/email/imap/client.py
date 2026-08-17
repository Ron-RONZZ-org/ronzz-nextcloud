# ruff: noqa: S110
"""IMAP client wrapper — connection, folder listing, search, move.

Vendored from lighterbird's ``imap/client.py`` (commit 99baa8ef) and
trimmed for the mailwatch daemon.  Removed: full sync engine
(``sync_folder``), attachment storage, folder CRUD, message deletion,
DB-backed lazy body fetch (replaced by the slim ``fetch_uids``).
"""

from __future__ import annotations

import base64
import email as email_lib
import imaplib
import logging
import re
import socket
import ssl
from typing import Any

from mailwatch.email.imap.capabilities import IMAPCapabilities, detect_capabilities
from mailwatch.email.imap.parser import parse_email_message

logger = logging.getLogger(__name__)

_SPECIAL_USE_MAP = {
    "\\Inbox": "INBOX",
    "\\Sent": "Sent",
    "\\Trash": "Trash",
    "\\Drafts": "Drafts",
    "\\Junk": "Junk",
    "\\Spam": "Spam",
    "\\Archive": "Archive",
    "\\All": "All Mail",
    "\\Flagged": "Starred",
}


# Regex for RFC 3501 LIST response: (flags) SP delimiter SP mailbox-name
# Matches:  (flags) "delimiter" "mailbox"  or  (flags) delimiter mailbox
_LIST_RE = re.compile(
    rb"\(([^)]*)\)\s+"
    rb'(?:"([^"]*)"|(\S+))\s+'
    rb'(?:"([^"]*)"|(\S+))',
)

# Regex to extract UID from FETCH response
_IMAP_UID_RE = re.compile(rb"UID (\d+)")


def _imap_quote_folder(name: str) -> str:
    """Quote a folder name for use in IMAP commands.

    Python 3.13's imaplib does not always quote folder names containing
    special characters (``&``, spaces) in ``_simple_command``, causing
    SELECT/EXAMINE to fail.  This helper ensures the name is quoted.
    """
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _to_imap_date(iso_date: str) -> str:
    """Convert ISO date (``YYYY-MM-DD``) to IMAP date format (``DD-Mon-YYYY``).

    IMAP SEARCH uses ``SINCE 1-Jan-2024`` not ``SINCE 2024-01-01``.
    Returns the original string if parsing fails.
    """
    import datetime

    try:
        dt = datetime.datetime.strptime(iso_date, "%Y-%m-%d").date()
        return dt.strftime("%d-%b-%Y")
    except (ValueError, TypeError):
        return iso_date


def decode_imap_utf7(name: str) -> str:
    """Decode an IMAP modified UTF-7 folder name (RFC 3501 §5.1.3) to Unicode.

    Modified UTF-7 encodes non-ASCII characters as ``&<base64>-`` where
    the base64 is the UTF-16BE encoding of the character(s).  ``&-`` is
    a literal ``&``.  Printable ASCII passes through unchanged.

    This is used for *display* only.  IMAP commands (SELECT, LIST, etc.)
    require the original modified UTF-7 encoding, so folder names in the
    database are kept in their raw IMAP form.
    """
    parts: list[str] = []
    i = 0
    while i < len(name):
        if name[i] != "&":
            parts.append(name[i])
            i += 1
            continue
        # Found '&' — look for closing '-'
        j = name.find("-", i + 1)
        if j == -1:
            # No closing dash — treat '&' as literal
            parts.append("&")
            i += 1
        elif j == i + 1:
            # '&-' — literal ampersand
            parts.append("&")
            i = j + 1
        else:
            # '&<base64>-' — encoded UTF-16BE
            encoded = name[i + 1 : j]
            try:
                # IMAP modified base64 has no padding — add it
                pad = 4 - len(encoded) % 4
                if pad != 4:
                    encoded_padded = encoded + "=" * pad
                else:
                    encoded_padded = encoded
                raw = base64.b64decode(encoded_padded, validate=True)
                parts.append(raw.decode("utf-16-be", errors="replace"))
            except (ValueError, OSError):
                # Garbled encoding — preserve original
                parts.append(name[i : j + 1])
            i = j + 1
    return "".join(parts)


def _parse_list_response(line: bytes) -> dict[str, Any] | None:
    """Parse a single RFC 3501 LIST response line.

    Returns None if the line is unparseable.
    """
    m = _LIST_RE.match(line)
    if not m:
        return None

    flags_raw, delim_q, delim_bare, name_q, name_bare = m.groups()
    flags = flags_raw.decode("ascii", errors="replace")
    delimiter = (delim_q or delim_bare or "").decode("utf-8", errors="replace")
    name = (name_q or name_bare or b"").decode("utf-8", errors="replace")

    # Extract special-use flags from the flags string
    special_use = None
    for flag in flags.split():
        if flag.startswith("\\") and flag != "\\NoSelect" and flag != "\\NoInferiors":
            candidate = _SPECIAL_USE_MAP.get(flag)
            if candidate:
                special_use = candidate
                break

    return {
        "flags": flags,
        "delimiter": delimiter,
        "name": name,
        "special_use": special_use,
    }


class IMAPClient:
    """Minimal IMAP client for the mailwatch daemon.

    Wraps ``imaplib`` with connect/login, capability detection, folder
    selection, server-side search, UID FETCH of message bodies, UID
    MOVE (RFC 6851 with COPY fallback), and flag updates.
    """

    def __init__(self, host: str, port: int = 993, use_ssl: bool = True):
        self.host = host
        self.port = port
        self.use_ssl = use_ssl
        self._conn: imaplib.IMAP4 | None = None
        self.capabilities: IMAPCapabilities = IMAPCapabilities()

    def connect(self, username: str, password: str) -> None:
        """Connect and login to IMAP server.

        After successful login, detects server capabilities.
        """
        try:
            if self.use_ssl:
                self._conn = imaplib.IMAP4_SSL(self.host, self.port, timeout=30)
            else:
                self._conn = imaplib.IMAP4(self.host, self.port, timeout=30)
            self._conn.login(username, password)
            self.capabilities = detect_capabilities(self._conn)
        except imaplib.IMAP4.error as e:
            raise ConnectionError(
                f"IMAP authentication failed for {username} at {self.host}:{self.port} — {e}"
            ) from e
        except (
            socket.gaierror,
            ConnectionRefusedError,
            TimeoutError,
            ssl.SSLError,
            OSError,
        ) as e:
            raise ConnectionError(
                f"IMAP connection failed: {username} at {self.host}:{self.port} — {e}"
            ) from e

    @property
    def conn(self) -> imaplib.IMAP4:
        if self._conn is None:
            raise RuntimeError("Not connected. Call connect() first.")
        return self._conn

    def disconnect(self) -> None:
        if self._conn:
            try:
                self._conn.logout()
            except Exception:
                pass
            self._conn = None

    def list_folders(self) -> list[dict[str, Any]]:
        """List all IMAP folders/mailboxes with SPECIAL-USE flags."""
        result: list[dict[str, Any]] = []
        typ, data = self.conn.list()
        if typ != "OK" or not data:
            logger.warning(
                "list_folders: IMAP LIST returned typ=%r with %d line(s)",
                typ,
                len(data) if data else 0,
            )
            return result
        skipped = 0
        for line in data:
            parsed = _parse_list_response(line)
            if parsed:
                result.append(parsed)
            else:
                skipped += 1
        if skipped:
            logger.warning(
                "list_folders: parsed %d folder(s), skipped %d unparseable line(s)",
                len(result),
                skipped,
            )
        else:
            logger.info(
                "list_folders: parsed %d folder(s)",
                len(result),
            )
        return result

    def search_by_header(
        self, folder: str, header_name: str, header_value: str
    ) -> list[bytes]:
        """Search for messages in a folder by a header value.

        Uses ``UID SEARCH HEADER <name> <value>``.

        Args:
            folder: Folder to search in.
            header_name: Header name (e.g. ``"Message-ID"``).
            header_value: Value to match.

        Returns:
            List of matching UIDs as bytes (empty if none).
        """
        try:
            self._select_folder(folder)
            typ, data = self.conn.uid("SEARCH", "HEADER", header_name, header_value)
            if typ != "OK" or not data or not data[0]:
                return []
            return data[0].split()
        except Exception as e:
            logger.warning("IMAP SEARCH for header %r failed: %s", header_name, e)
            return []

    # ── Folder selection ───────────────────────────────────────────────

    def _select_folder(self, folder: str) -> bool:
        """Select a folder for write operations. Returns True on success."""
        typ, data = self.conn.select(_imap_quote_folder(folder), readonly=False)
        return typ == "OK"

    def select_folder_ex(
        self, folder: str, readonly: bool = True, condstore: bool = False
    ) -> tuple[bool, int | None, int | None]:
        """Select a folder and return UIDVALIDITY and HIGHESTMODSEQ.

        Parses the SELECT response for ``[UIDVALIDITY N]`` and
        ``[HIGHESTMODSEQ M]`` response codes (RFC 3501, RFC 4551).

        Args:
            folder: Folder name to select.
            readonly: If True, SELECT with readonly=True (no write lock).
            condstore: If True, enables CONDSTORE on this SELECT
                       (RFC 4551).  Server must advertise CONDSTORE.

        Returns:
            Tuple of (success, uidvalidity, highest_modseq).
            Values may be None if not advertised by the server.
        """
        try:
            quoted = _imap_quote_folder(folder)
            if condstore and self.capabilities.has_condstore:
                typ, data = self.conn._simple_command("SELECT", quoted, b"(CONDSTORE)")
            else:
                typ, data = self.conn.select(quoted, readonly=readonly)

            if typ != "OK":
                return False, None, None

            uidvalidity: int | None = None
            highest_modseq: int | None = None

            untagged = data or []
            for resp in untagged:
                if isinstance(resp, bytes):
                    resp_str = resp.decode("ascii", errors="replace")
                else:
                    continue

                if "UIDVALIDITY" in resp_str:
                    m = re.search(r"UIDVALIDITY\s+(\d+)", resp_str)
                    if m:
                        uidvalidity = int(m.group(1))

                if "HIGHESTMODSEQ" in resp_str:
                    m = re.search(r"HIGHESTMODSEQ\s+(\d+)", resp_str)
                    if m:
                        highest_modseq = int(m.group(1))

            return True, uidvalidity, highest_modseq
        except Exception:
            logger.warning("select_folder_ex failed for %r", folder, exc_info=True)
            return False, None, None

    def copy_message(self, uid: int, from_folder: str, to_folder: str) -> bool:
        """Copy a message from *from_folder* to *to_folder* via IMAP UID COPY.

        Requires folder to be selected (select is done internally).
        Returns True on success, False on failure.
        """
        if not self._select_folder(from_folder):
            return False
        typ, data = self.conn.uid("COPY", str(uid), to_folder)
        return typ == "OK"

    def move_message(self, uid: int, from_folder: str, to_folder: str) -> bool:
        """Move a message from *from_folder* to *to_folder* via IMAP UID MOVE.

        Falls back to COPY + STORE + EXPUNGE if MOVE (RFC 6851) is not
        supported by the server.
        Returns True on success, False on failure.
        """
        if not self._select_folder(from_folder):
            return False
        try:
            typ, data = self.conn.uid("MOVE", str(uid), to_folder)
            if typ == "OK":
                return True
        except imaplib.IMAP4.error:
            pass
        # Fallback: COPY + STORE \Deleted + EXPUNGE
        typ, data = self.conn.uid("COPY", str(uid), to_folder)
        if typ != "OK":
            return False
        try:
            self.conn.uid("STORE", str(uid), "+FLAGS", "(\\Deleted)")
            self.conn.expunge()
        except imaplib.IMAP4.error:
            return False
        return True

    def set_flags(
        self,
        uid: int,
        folder: str,
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> bool:
        """Add or remove IMAP flags on a message by UID.

        Args:
            uid: IMAP UID of the message.
            folder: Folder name to select.
            add: List of flags to add (e.g. ``["\\Seen"]``).
            remove: List of flags to remove.

        Returns:
            True on success, False on failure.
        """
        if not self._select_folder(folder):
            return False
        try:
            if add:
                flag_str = " ".join(add)
                typ, _ = self.conn.uid(
                    "STORE", str(uid), "+FLAGS.SILENT", f"({flag_str})"
                )
                if typ != "OK":
                    return False
            if remove:
                flag_str = " ".join(remove)
                typ, _ = self.conn.uid(
                    "STORE", str(uid), "-FLAGS.SILENT", f"({flag_str})"
                )
                if typ != "OK":
                    return False
            return True
        except imaplib.IMAP4.error:
            return False

    def append_message(
        self, folder: str, raw_message: bytes, flags: list[str] | None = None
    ) -> bool:
        """Append a raw RFC822 message into *folder* via IMAP APPEND.

        The message bytes are written verbatim (headers, body, and
        attachments untouched) — used by the redirector to deliver a
        message into the destination mailbox exactly as it was fetched.

        Args:
            folder: Destination folder (e.g. ``"INBOX"``).
            raw_message: Full raw RFC822 message bytes.
            flags: Optional IMAP flags to set (e.g. ``["\\Seen"]``).

        Returns:
            True on success, False on failure.
        """
        # imaplib.append() expects the flags as a *string* already wrapped
        # in parentheses (e.g. "(\\Seen)"), NOT a list — a list is joined
        # into "(\\Seen)" again by imaplib, producing invalid IMAP
        # ("Invalid characters in keyword").  Normalize defensively:
        #   ["\\Seen"]            -> "(\\Seen)"
        #   "(\\Seen)"            -> "(\\Seen)"
        #   ["\\Seen", "\\Flagged"] -> "(\\Seen \\Flagged)"
        #   [] / None             -> None (no flags)
        flag_arg: str | None = None
        if flags:
            if isinstance(flags, str):
                flag_str = flags.strip()
            else:
                flag_str = " ".join(str(f) for f in flags).strip()
            if flag_str:
                flag_arg = (
                    flag_str if flag_str.startswith("(") else f"({flag_str})"
                )
        try:
            typ, _ = self.conn.append(
                _imap_quote_folder(folder),
                flag_arg,
                None,  # internal date — leave server default
                raw_message,
            )
            return typ == "OK"
        except (imaplib.IMAP4.error, OSError) as exc:
            logger.warning(
                "append_message: APPEND to %r failed (%d bytes): %s",
                folder,
                len(raw_message),
                exc,
            )
            return False

    def fetch_raw(
        self,
        uid: int,
        folder: str,
        mark_seen: bool = False,
    ) -> bytes | None:
        """Fetch the raw RFC822 bytes of a single message by UID.

        Unlike :meth:`fetch_uids` (which parses into dicts for
        classification), this returns the *unmodified* message bytes —
        exactly what the redirector needs to preserve headers.

        Args:
            uid: IMAP UID of the message.
            folder: Folder to fetch from.
            mark_seen: If True, use ``BODY[]`` (marks ``\\Seen``) instead of
                       ``BODY.PEEK[]``.

        Returns:
            Raw message bytes, or None on failure.
        """
        if not self._select_folder(folder):
            return None
        item = "BODY[]" if mark_seen else "BODY.PEEK[]"
        try:
            typ, data = self.conn.uid("FETCH", str(uid), f"({item})")
        except imaplib.IMAP4.error as exc:
            logger.warning("fetch_raw: FETCH UID %s in %r failed: %s", uid, folder, exc)
            return None
        if typ != "OK" or not data:
            logger.warning("fetch_raw: no data for UID %s in %r", uid, folder)
            return None
        for item_data in data:
            if isinstance(item_data, tuple) and len(item_data) >= 2:
                return item_data[1]
        return None


    def fetch_uids(
        self,
        folder: str,
        uids: list[int],
        mark_seen: bool = False,
    ) -> list[dict[str, Any]]:
        """Fetch full message bodies for the given UIDs.

        Selects the folder, fetches ``(FLAGS BODY.PEEK[] UID)`` for each
        UID, and parses each message via :func:`parse_email_message`.
        Attachment blobs are not stored (``store_attachments=False``) —
        mailwatch only needs subject/body/headers for classification.

        Args:
            folder: Folder to fetch from.
            uids: Message UIDs to fetch.
            mark_seen: If True, set the ``\\Seen`` flag after a successful
                       fetch (uses ``BODY[]`` instead of ``BODY.PEEK[]``).

        Returns:
            List of parsed message dicts (see ``parse_email_message``).
        """
        if not uids:
            return []
        ok, _uidvalidity, _modseq = self.select_folder_ex(
            folder, readonly=not mark_seen
        )
        if not ok:
            logger.warning("fetch_uids: cannot select folder %r", folder)
            return []

        results: list[dict[str, Any]] = []
        # Fetch in chunks to keep single FETCH commands manageable.
        chunk_size = 50
        for start in range(0, len(uids), chunk_size):
            chunk = uids[start : start + chunk_size]
            uid_set = ",".join(str(u) for u in chunk)
            item = "BODY[]" if mark_seen else "BODY.PEEK[]"
            try:
                typ, data = self.conn.uid("FETCH", uid_set, f"(FLAGS {item} UID)")
            except imaplib.IMAP4.error as exc:
                logger.warning(
                    "fetch_uids: FETCH failed for %d UID(s) in %r: %s",
                    len(chunk),
                    folder,
                    exc,
                )
                continue
            if typ != "OK" or not data:
                continue

            for item_data in data:
                if not isinstance(item_data, tuple) or len(item_data) < 2:
                    continue
                raw_data = item_data[1]
                # Extract the UID from the FETCH response header (e.g.
                # b'1 (FLAGS () UID 42 BODY[] {123}').
                uid_match = _IMAP_UID_RE.search(
                    item_data[0] if isinstance(item_data[0], bytes) else b""
                )
                imap_uid = int(uid_match.group(1)) if uid_match else chunk[0]
                try:
                    msg = email_lib.message_from_bytes(raw_data)
                    parsed = parse_email_message(
                        msg,
                        account_email="",
                        folder_name=folder,
                        imap_uid=imap_uid,
                        store_attachments=False,
                    )
                    parsed["account_email"] = ""  # filled by caller
                    results.append(parsed)
                except Exception as exc:
                    logger.warning(
                        "fetch_uids: parse error for UID %s in %r: %s",
                        imap_uid,
                        folder,
                        exc,
                    )
        return results

    def fetch_message_ids(self, folder: str) -> dict[int, str]:
        """Map UID → Message-ID for all messages in a folder.

        Lightweight header-only fetch used by the training idler to
        reconcile Junk/INBOX contents (no full bodies).

        Args:
            folder: Folder to scan.

        Returns:
            Dict mapping UID (int) → Message-ID (str, angle brackets
            stripped).  Messages without a Message-ID map to "".
        """
        ok, _uidvalidity, _modseq = self.select_folder_ex(folder, readonly=True)
        if not ok:
            logger.warning("fetch_message_ids: cannot select folder %r", folder)
            return {}

        result: dict[int, str] = {}
        try:
            typ, data = self.conn.uid(
                "FETCH",
                "1:*",
                "(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])",
            )
        except imaplib.IMAP4.error as exc:
            logger.warning("fetch_message_ids: FETCH failed in %r: %s", folder, exc)
            return result
        if typ != "OK" or not data:
            return result

        from email.parser import BytesHeaderParser

        for item in data:
            if not isinstance(item, tuple) or len(item) < 2:
                continue
            uid_match = _IMAP_UID_RE.search(
                item[0] if isinstance(item[0], bytes) else b""
            )
            if not uid_match:
                continue
            uid = int(uid_match.group(1))
            try:
                header_msg = BytesHeaderParser().parsebytes(item[1])
                raw_id = header_msg.get("Message-ID", "")
                result[uid] = str(raw_id).strip("<>") if raw_id else ""
            except Exception:
                result[uid] = ""
        return result

    def search_remote(
        self,
        folder: str,
        query: str,
        criteria: dict[str, str] | None = None,
    ) -> list[int]:
        """Server-side IMAP SEARCH, returns matching UIDs.

        Delegates body text search to the IMAP server using ``UID SEARCH
        TEXT`` / ``UID SEARCH SUBJECT`` / etc.

        Args:
            folder: Folder to search in (e.g. ``"INBOX"``).
            query: Free-text search string.
            criteria: Optional dict with structured filters:
                - ``from_``: sender pattern
                - ``subject``: subject pattern
                - ``after``: date string (YYYY-MM-DD)
                - ``before``: date string (YYYY-MM-DD)

        Returns:
            List of IMAP UIDs matching the search.

        Raises:
            ConnectionError: If the IMAP connection fails.
        """
        try:
            self.conn.select(_imap_quote_folder(folder), readonly=True)
        except Exception as exc:
            raise ConnectionError(
                f"Cannot select folder {folder!r} for search: {exc}"
            ) from exc

        parts: list[str] = []

        if criteria:
            from_str = criteria.get("from_", "")
            if from_str:
                parts.append(f'FROM "{from_str}"')
            to_str = criteria.get("to", "")
            if to_str:
                parts.append(f'TO "{to_str}"')
            cc_str = criteria.get("cc", "")
            if cc_str:
                parts.append(f'CC "{cc_str}"')
            subj = criteria.get("subject", "")
            if subj:
                parts.append(f'SUBJECT "{subj}"')
            participant_str = criteria.get("participant", "")
            if participant_str:
                parts.append(
                    f'OR FROM "{participant_str}" '
                    f'OR TO "{participant_str}" '
                    f'CC "{participant_str}"'
                )
            after = criteria.get("after", "")
            if after:
                parts.append(f"SINCE {_to_imap_date(after)}")
            before = criteria.get("before", "")
            if before:
                parts.append(f"BEFORE {_to_imap_date(before)}")

        if query:
            parts.append(f'TEXT "{query}"')

        if not parts:
            return []

        search_cmd = " ".join(parts)

        try:
            typ, data = self.conn.uid("search", None, search_cmd)
        except Exception as exc:
            logger.warning(
                "search_remote: UID SEARCH failed in %r: %s",
                folder,
                exc,
            )
            return []

        if typ != "OK" or not data or not data[0]:
            return []

        return [int(uid) for uid in data[0].split()]


__all__ = ["IMAPClient", "decode_imap_utf7"]
