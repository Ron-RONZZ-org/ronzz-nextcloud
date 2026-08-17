"""Tests for the Sieve auto-block script generation and offender tracking."""

from __future__ import annotations

import pytest

from mailwatch.config import AccountConfig, AutoBlockConfig
from mailwatch.db import get_db
from mailwatch.sieve_block import SieveAutoBlocker, generate_block_script


def test_generate_block_script_domain_only():
    script = generate_block_script(["evil.example"])
    assert 'require ["reject", "address"];' in script
    assert 'if address :domain :contains "from" "evil.example"' in script
    assert 'reject "Blocked domain: evil.example";' in script
    # No envelope extension — Migadu-incompatible (issue #2).
    assert "envelope" not in script


def test_generate_block_script_multiple_domains_sorted():
    script = generate_block_script(["zeta.example", "alpha.example"])
    assert script.index("alpha.example") < script.index("zeta.example")


def test_generate_block_script_quotes_escaped():
    script = generate_block_script(['evil"quoted.example'])
    assert 'reject "Blocked domain: evil\\"quoted.example";' in script


def test_generate_block_script_empty():
    assert generate_block_script([]).strip() == ""


@pytest.fixture
def db(tmp_path):
    return get_db(tmp_path / "sieve.db")


@pytest.fixture
def account() -> AccountConfig:
    return AccountConfig(
        email="me@ronzz.org",
        sieve_host="managesieve.migadu.com",
        sieve_port=4190,
    )


def _blocker(db):
    return SieveAutoBlocker(db, AutoBlockConfig(hits_threshold=3, window_days=14))


def test_register_hit_and_threshold(db):
    b = _blocker(db)
    assert b.register_hit("me@ronzz.org", "evil.example") == 1
    assert b.register_hit("me@ronzz.org", "evil.example") == 2
    assert b.count_recent_hits("me@ronzz.org", "evil.example", 14) == 2


def test_register_hit_per_account(db):
    b = _blocker(db)
    b.register_hit("a@ronzz.org", "evil.example")
    b.register_hit("b@ronzz.org", "evil.example")
    assert b.count_recent_hits("a@ronzz.org", "evil.example", 14) == 1
    assert b.count_recent_hits("b@ronzz.org", "evil.example", 14) == 1


def test_record_block_and_list(db):
    b = _blocker(db)
    b.register_hit("me@ronzz.org", "evil.example")
    b.record_block("me@ronzz.org", "evil.example")
    assert b.list_blocked("me@ronzz.org") == ["evil.example"]
    # Different account unaffected
    assert b.list_blocked("other@ronzz.org") == []


def test_push_block_script_dry_run(db, account):
    b = _blocker(db)
    b.register_hit("me@ronzz.org", "evil.example")
    b.record_block("me@ronzz.org", "evil.example")
    # No managesieve library interaction in dry-run mode.
    assert b.push_block_script(account, "pw", dry_run=True) is True


def test_push_block_script_no_blocks_skips(db, account, monkeypatch):
    b = _blocker(db)
    called = {"n": 0}

    def _fail(*a, **k):
        called["n"] += 1
        raise AssertionError("should not push with no blocks")

    monkeypatch.setattr("mailwatch.sieve_block.SieveManager", _fail)
    assert b.push_block_script(account, "pw") is True
    assert called["n"] == 0
