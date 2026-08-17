"""Tests for IMAP capability detection."""

from __future__ import annotations

from unittest.mock import MagicMock

from mailwatch.email.imap.capabilities import (
    IMAPCapabilities,
    detect_capabilities,
)


class TestIMAPCapabilities:
    def test_defaults_all_false(self):
        caps = IMAPCapabilities()
        assert caps.has_condstore is False
        assert caps.has_qresync is False
        assert caps.has_idle is False
        assert caps.has_move is False
        assert caps.has_uidplus is False


class TestDetectCapabilities:
    def _make_conn(self, capability_response: tuple[str, list[bytes]]):
        conn = MagicMock()
        conn.capability.return_value = capability_response
        return conn

    def test_empty_response(self):
        conn = self._make_conn(("OK", []))
        caps = detect_capabilities(conn)
        assert caps.has_condstore is False

    def test_not_ok_response(self):
        conn = self._make_conn(("BAD", [b"failed"]))
        caps = detect_capabilities(conn)
        assert caps.has_condstore is False

    def test_condstore_detected(self):
        conn = self._make_conn(("OK", [b"IMAP4rev1 CONDSTORE IDLE"]))
        caps = detect_capabilities(conn)
        assert caps.has_condstore is True
        assert caps.has_idle is True
        assert caps.has_qresync is False

    def test_qresync_detected(self):
        conn = self._make_conn(("OK", [b"IMAP4rev1 CONDSTORE QRESYNC"]))
        caps = detect_capabilities(conn)
        assert caps.has_condstore is True
        assert caps.has_qresync is True

    def test_move_detected(self):
        conn = self._make_conn(("OK", [b"IMAP4rev1 MOVE UIDPLUS"]))
        caps = detect_capabilities(conn)
        assert caps.has_move is True
        assert caps.has_uidplus is True

    def test_idle_detected(self):
        conn = self._make_conn(("OK", [b"IMAP4rev1 IDLE"]))
        caps = detect_capabilities(conn)
        assert caps.has_idle is True

    def test_all_capabilities(self):
        conn = self._make_conn(
            (
                "OK",
                [b"IMAP4rev1 CONDSTORE QRESYNC IDLE MOVE UIDPLUS"],
            )
        )
        caps = detect_capabilities(conn)
        assert caps.has_condstore is True
        assert caps.has_qresync is True
        assert caps.has_idle is True
        assert caps.has_move is True
        assert caps.has_uidplus is True

    def test_no_capabilities(self):
        conn = self._make_conn(("OK", [b"IMAP4rev1 LOGIN-REFERRALS"]))
        caps = detect_capabilities(conn)
        assert caps.has_condstore is False
        assert caps.has_idle is False
        assert caps.has_move is False
        assert caps.has_uidplus is False

    def test_exception_handled(self):
        conn = MagicMock()
        conn.capability.side_effect = RuntimeError("IMAP down")
        caps = detect_capabilities(conn)
        assert caps.has_condstore is False
