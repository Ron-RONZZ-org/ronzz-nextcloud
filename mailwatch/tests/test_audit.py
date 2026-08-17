"""Tests for the audit JSON-lines log."""

from __future__ import annotations

import json

from mailwatch.audit import AuditLog


def test_emit_writes_json_line(tmp_path):
    path = tmp_path / "audit.jsonl"
    audit = AuditLog(path)
    audit.emit("test", account="a@b.c", uid=42)
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["event"] == "test"
    assert rec["account"] == "a@b.c"
    assert rec["uid"] == 42
    assert "ts" in rec


def test_dry_run_flag_in_every_line(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl", dry_run=True)
    audit.emit("classification")
    rec = json.loads(audit.path.read_text())
    assert rec["dry_run"] is True


def test_classification_event_shape(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    audit.classification(
        account="me@ronzz.org",
        folder="INBOX",
        uid=1,
        message_id="<m@x>",
        from_addr="spam@evil.example",
        subject="URGENT",
        scores={"bayesian": 0.95, "phishing": 0.0, "similarity": 0.0, "combined": 0.95},
        is_spam=True,
        is_phishing=False,
        reasons=[],
        action="move_junk",
    )
    rec = json.loads(audit.path.read_text())
    assert rec["event"] == "classification"
    assert rec["scores"]["bayesian"] == 0.95
    assert rec["action"] == "move_junk"
    assert rec["is_spam"] is True


def test_append_not_truncate(tmp_path):
    path = tmp_path / "audit.jsonl"
    audit = AuditLog(path)
    audit.emit("one")
    audit.emit("two")
    assert len(path.read_text().strip().splitlines()) == 2
