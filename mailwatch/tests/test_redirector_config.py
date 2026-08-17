"""Tests for the send-to-hesk redirector config."""

from __future__ import annotations

from mailwatch.config import (
    ConfigError,
    RedirectConfig,
    RedirectTarget,
    load_config,
)


def _write_config(tmp_path, content: str):
    p = tmp_path / "config.toml"
    p.write_text(content)
    return p


def test_redirect_defaults_disabled(tmp_path):
    p = _write_config(
        tmp_path,
        """
[[accounts]]
email = "ron@ronzz.org"
""",
    )
    cfg = load_config(p)
    redirect = cfg.accounts[0].redirect
    assert isinstance(redirect, RedirectConfig)
    assert redirect.enabled is False
    assert redirect.source_folder == "send-to-hesk"
    assert redirect.target is None


def test_redirect_enabled_with_target(tmp_path):
    p = _write_config(
        tmp_path,
        """
[[accounts]]
email = "ron@ronzz.org"

[accounts.redirect]
enabled = true
source_folder = "send-to-hesk"

[accounts.redirect.target]
email = "hi@ronzz.org"
destination_folder = "INBOX"
""",
    )
    cfg = load_config(p)
    redirect = cfg.accounts[0].redirect
    assert redirect.enabled is True
    assert redirect.source_folder == "send-to-hesk"
    assert isinstance(redirect.target, RedirectTarget)
    assert redirect.target.email == "hi@ronzz.org"
    assert redirect.target.destination_folder == "INBOX"
    assert redirect.target.imap_host == "imap.migadu.com"  # default
    assert redirect.target.imap_username == "hi@ronzz.org"  # username default


def test_redirect_after_move_folder(tmp_path):
    p = _write_config(
        tmp_path,
        """
[[accounts]]
email = "ron@ronzz.org"

[accounts.redirect]
enabled = true
after_move_folder = "send-to-hesk-done"

[accounts.redirect.target]
email = "hi@ronzz.org"
""",
    )
    cfg = load_config(p)
    assert cfg.accounts[0].redirect.after_move_folder == "send-to-hesk-done"


def test_redirect_target_requires_email(tmp_path):
    p = _write_config(
        tmp_path,
        """
[[accounts]]
email = "ron@ronzz.org"

[accounts.redirect]
enabled = true

[accounts.redirect.target]
email = ""
""",
    )
    import pytest

    with pytest.raises(ConfigError):
        load_config(p)


def test_redirect_target_invalid_email(tmp_path):
    p = _write_config(
        tmp_path,
        """
[[accounts]]
email = "ron@ronzz.org"

[accounts.redirect]
enabled = true

[accounts.redirect.target]
email = "not-an-email"
""",
    )
    import pytest

    with pytest.raises(ConfigError):
        load_config(p)
