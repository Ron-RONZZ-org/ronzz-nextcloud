"""Tests for mailwatch configuration loading and validation."""

from __future__ import annotations

import pytest

from mailwatch.config import (
    ConfigError,
    MailwatchConfig,
    load_config,
)

GOOD_TOML = """
[daemon]
dry_run = true
feed_refresh_hours = 12
spam_threshold = 0.85

[daemon.training]
enabled = true
scan_interval_seconds = 60

[daemon.auto_block]
enabled = true
hits_threshold = 5

[[accounts]]
email = "me@ronzz.org"

[[accounts]]
email = "other@ronzz.org"
imap_host = "imap.example.com"
sieve_host = "managesieve.example.com"
"""


def _write_config(tmp_path, content: str):
    p = tmp_path / "config.toml"
    p.write_text(content)
    return p


def test_load_minimal_config(tmp_path):
    p = _write_config(
        tmp_path,
        """
[[accounts]]
email = "me@ronzz.org"
""",
    )
    cfg = load_config(p)
    assert isinstance(cfg, MailwatchConfig)
    assert cfg.accounts[0].email == "me@ronzz.org"
    assert cfg.accounts[0].imap_host == "imap.migadu.com"  # default
    assert cfg.daemon.spam_threshold == 0.9  # default
    assert cfg.daemon.training.enabled is True
    assert cfg.daemon.auto_block.enabled is False


def test_load_full_config(tmp_path):
    p = _write_config(tmp_path, GOOD_TOML)
    cfg = load_config(p)
    assert cfg.daemon.dry_run is True
    assert cfg.daemon.feed_refresh_hours == 12
    assert cfg.daemon.spam_threshold == 0.85
    assert cfg.daemon.training.scan_interval_seconds == 60
    assert cfg.daemon.auto_block.hits_threshold == 5
    assert [a.email for a in cfg.accounts] == ["me@ronzz.org", "other@ronzz.org"]
    assert cfg.accounts[1].sieve_host == "managesieve.example.com"


def test_missing_config_file(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.toml")


def test_invalid_toml(tmp_path):
    p = _write_config(tmp_path, "not [valid toml")
    with pytest.raises(ConfigError):
        load_config(p)


def test_missing_email(tmp_path):
    p = _write_config(
        tmp_path,
        """
[[accounts]]
imap_host = "imap.migadu.com"
""",
    )
    with pytest.raises(ConfigError, match="email"):
        load_config(p)


def test_no_accounts(tmp_path):
    p = _write_config(
        tmp_path,
        """
[daemon]
dry_run = true
""",
    )
    with pytest.raises(ConfigError, match="No accounts"):
        load_config(p)


def test_duplicate_accounts(tmp_path):
    p = _write_config(
        tmp_path,
        """
[[accounts]]
email = "me@ronzz.org"

[[accounts]]
email = "ME@ronzz.org"
""",
    )
    with pytest.raises(ConfigError, match="Duplicate"):
        load_config(p)


def test_bad_threshold(tmp_path):
    p = _write_config(
        tmp_path,
        """
[daemon]
spam_threshold = 1.5

[[accounts]]
email = "me@ronzz.org"
""",
    )
    with pytest.raises(ConfigError, match="spam_threshold"):
        load_config(p)


def test_bad_port(tmp_path):
    p = _write_config(
        tmp_path,
        """
[[accounts]]
email = "me@ronzz.org"
imap_port = "nine-nine-three"
""",
    )
    with pytest.raises(ConfigError):
        load_config(p)


def test_username_defaults_to_email():
    from mailwatch.config import AccountConfig

    a = AccountConfig(email="me@ronzz.org")
    assert a.imap_username == "me@ronzz.org"
    a2 = AccountConfig(email="me@ronzz.org", username="custom")
    assert a2.imap_username == "custom"
    assert a2.resolved_junk_folder == "Junk"
    a3 = AccountConfig(email="me@ronzz.org", junk_folder="Spam")
    assert a3.resolved_junk_folder == "Spam"
