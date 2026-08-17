"""Configuration loading and validation for mailwatch.

Accounts + daemon settings live in a TOML file (stdlib ``tomllib``).
Passwords are NOT stored here — they live in the system keyring
(see ``mailwatch.keyring``) keyed by account email.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "/etc/mailwatch/config.toml"


class ConfigError(Exception):
    """Raised when the mailwatch configuration is invalid."""


@dataclass
class TrainingConfig:
    enabled: bool = True
    # How often to run the Junk-folder reconciliation scan (seconds).
    scan_interval_seconds: int = 120
    # Train on Junk arrivals that the daemon did NOT move itself.
    train_spam_on_junk_arrival: bool = True
    # Train ham when a message moves from Junk back to INBOX.
    train_ham_on_junk_to_inbox: bool = True


@dataclass
class AutoBlockConfig:
    enabled: bool = False
    # A domain becomes a block candidate after this many spam hits.
    hits_threshold: int = 3
    # Only count hits within this window (days).
    window_days: int = 14
    # Sieve script name on the server (per account).
    script_name: str = "mailwatch_blocks"


@dataclass
class DaemonConfig:
    dry_run: bool = False
    log_level: str = "INFO"
    # Refresh phishing feeds every N hours (0 = disabled).
    feed_refresh_hours: int = 6
    # Spam threshold for the combined Bayesian/similarity score.
    spam_threshold: float = 0.9
    # Run a catch-up UNSEEN scan every N seconds even without IDLE
    # notifications (guards against missed IDLE events).
    catch_up_scan_seconds: int = 300
    # Junk folder name on the server (Migadu uses "Junk").
    junk_folder: str = "Junk"
    training: TrainingConfig = field(default_factory=TrainingConfig)
    auto_block: AutoBlockConfig = field(default_factory=AutoBlockConfig)


@dataclass
class AccountConfig:
    email: str
    imap_host: str = "imap.migadu.com"
    imap_port: int = 993
    imap_use_ssl: bool = True
    username: str = ""
    # Override the Junk folder for this account if it differs.
    junk_folder: str = ""
    # Optional per-account ManageSieve settings for auto-block.
    sieve_host: str = ""
    sieve_port: int = 4190
    sieve_use_tls: bool = True

    @property
    def imap_username(self) -> str:
        return self.username or self.email

    @property
    def resolved_junk_folder(self) -> str:
        return self.junk_folder or "Junk"

    @property
    def has_sieve(self) -> bool:
        return bool(self.sieve_host)


@dataclass
class MailwatchConfig:
    daemon: DaemonConfig = field(default_factory=DaemonConfig)
    accounts: list[AccountConfig] = field(default_factory=list)

    @property
    def account_emails(self) -> list[str]:
        return [a.email for a in self.accounts]


def _parse_bool(value: Any, key: str, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        if value.strip().lower() in ("true", "yes", "1", "on"):
            return True
        if value.strip().lower() in ("false", "no", "0", "off"):
            return False
    raise ConfigError(f"Config key {key!r}: expected boolean, got {value!r}")


def _parse_int(value: Any, key: str, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ConfigError(
            f"Config key {key!r}: expected integer, got {value!r}"
        ) from None


def _parse_float(value: Any, key: str, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ConfigError(
            f"Config key {key!r}: expected number, got {value!r}"
        ) from None


def _parse_account(raw: dict[str, Any], index: int) -> AccountConfig:
    email = str(raw.get("email", "")).strip()
    if not email:
        raise ConfigError(f"accounts[{index}]: 'email' is required")
    if "@" not in email:
        raise ConfigError(f"accounts[{index}]: '{email}' is not a valid email address")

    try:
        return AccountConfig(
            email=email,
            imap_host=str(raw.get("imap_host", "imap.migadu.com")),
            imap_port=_parse_int(
                raw.get("imap_port"), f"accounts[{index}].imap_port", 993
            ),
            imap_use_ssl=_parse_bool(
                raw.get("imap_use_ssl"), f"accounts[{index}].imap_use_ssl", True
            ),
            username=str(raw.get("username", "")),
            junk_folder=str(raw.get("junk_folder", "")),
            sieve_host=str(raw.get("sieve_host", "")),
            sieve_port=_parse_int(
                raw.get("sieve_port"), f"accounts[{index}].sieve_port", 4190
            ),
            sieve_use_tls=_parse_bool(
                raw.get("sieve_use_tls"), f"accounts[{index}].sieve_use_tls", True
            ),
        )
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(f"accounts[{index}]: invalid account: {exc}") from exc


def load_config(path: str | Path | None = None) -> MailwatchConfig:
    """Load and validate the mailwatch TOML configuration.

    Args:
        path: Path to the config file.  Defaults to
              ``/etc/mailwatch/config.toml``.

    Returns:
        Parsed :class:`MailwatchConfig`.

    Raises:
        ConfigError: If the file is missing, unreadable, or invalid.
    """
    config_path = Path(path) if path else Path(DEFAULT_CONFIG_PATH)
    if not config_path.exists():
        raise ConfigError(
            f"Configuration file not found: {config_path} "
            f"(use --config to point at config.toml)"
        )
    try:
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Cannot read config {config_path}: {exc}") from exc

    daemon_raw = raw.get("daemon", {}) or {}
    training_raw = daemon_raw.get("training", {}) or {}
    autoblock_raw = daemon_raw.get("auto_block", {}) or {}

    daemon = DaemonConfig(
        dry_run=_parse_bool(daemon_raw.get("dry_run"), "daemon.dry_run", False),
        log_level=str(daemon_raw.get("log_level", "INFO")).upper(),
        feed_refresh_hours=_parse_int(
            daemon_raw.get("feed_refresh_hours"),
            "daemon.feed_refresh_hours",
            6,
        ),
        spam_threshold=_parse_float(
            daemon_raw.get("spam_threshold"),
            "daemon.spam_threshold",
            0.9,
        ),
        catch_up_scan_seconds=_parse_int(
            daemon_raw.get("catch_up_scan_seconds"),
            "daemon.catch_up_scan_seconds",
            300,
        ),
        junk_folder=str(daemon_raw.get("junk_folder", "Junk")),
        training=TrainingConfig(
            enabled=_parse_bool(
                training_raw.get("enabled"), "daemon.training.enabled", True
            ),
            scan_interval_seconds=_parse_int(
                training_raw.get("scan_interval_seconds"),
                "daemon.training.scan_interval_seconds",
                120,
            ),
            train_spam_on_junk_arrival=_parse_bool(
                training_raw.get("train_spam_on_junk_arrival"),
                "daemon.training.train_spam_on_junk_arrival",
                True,
            ),
            train_ham_on_junk_to_inbox=_parse_bool(
                training_raw.get("train_ham_on_junk_to_inbox"),
                "daemon.training.train_ham_on_junk_to_inbox",
                True,
            ),
        ),
        auto_block=AutoBlockConfig(
            enabled=_parse_bool(
                autoblock_raw.get("enabled"), "daemon.auto_block.enabled", False
            ),
            hits_threshold=_parse_int(
                autoblock_raw.get("hits_threshold"),
                "daemon.auto_block.hits_threshold",
                3,
            ),
            window_days=_parse_int(
                autoblock_raw.get("window_days"),
                "daemon.auto_block.window_days",
                14,
            ),
            script_name=str(autoblock_raw.get("script_name", "mailwatch_blocks")),
        ),
    )

    if not (0.0 < daemon.spam_threshold <= 1.0):
        raise ConfigError(
            f"daemon.spam_threshold must be in (0, 1], got {daemon.spam_threshold}"
        )

    accounts_raw = raw.get("accounts", []) or []
    if not isinstance(accounts_raw, list):
        raise ConfigError("'accounts' must be a list of account tables")
    if not accounts_raw:
        raise ConfigError(
            "No accounts configured — add at least one [[accounts]] table"
        )

    accounts = [_parse_account(a, i) for i, a in enumerate(accounts_raw)]

    emails = [a.email.lower() for a in accounts]
    if len(set(emails)) != len(emails):
        raise ConfigError("Duplicate account email in config")

    return MailwatchConfig(daemon=daemon, accounts=accounts)


__all__ = [
    "AccountConfig",
    "AutoBlockConfig",
    "ConfigError",
    "DaemonConfig",
    "MailwatchConfig",
    "TrainingConfig",
    "load_config",
]
