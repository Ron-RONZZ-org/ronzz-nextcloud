# mailwatch — IMAP IDLE spam/idler daemon (Migadu)

Headless, always-on watcher for Migadu-hosted `@ronzz.org` mail.  Derived
from [lighterbird](https://github.com/Ron-RONZZ-org/lighterbird)'s email
module (see `VENDORED.md` for provenance).  Replaces the discontinued
lighterbird web app's classification role with a small, client-independent
daemon that protects every mail client (phone, Outlook, webmail).

## What it does

```
[IMAP IDLE (RFC 2177)] --on_notification--> [fetch new msg] --> [classify: Bayesian | phishing | similarity]
        ^                                                          |
        |                                                          v
[Migadu IMAP/Sieve] <-- MOVE to Junk / Sieve rule upload ---------[decision: spam? phishing?]
```

1. **Idles on IMAP** (RFC 2177) for every configured account — near-real-time
   new-mail detection, no cron polling.  Per-account threads, 29-min IDLE
   re-issue, exponential reconnect backoff.
2. On a new message, runs the three detectors vendored from lighterbird:
   - **Bayesian** (chi-squared, seed corpus + per-user tokens)
   - **Phishing feeds** (OpenPhish / PhishTank / PhishStats + brand spoof)
   - **MinHash similarity** (near-duplicate detection against known spam)
3. **Acts on classification:**
   - Spam → `MOVE` the message to the Junk folder on the IMAP server
     (UID MOVE, RFC 6851, visible to all clients).
   - Repeat-offender domain (≥ `hits_threshold` spam classifications) →
     optional **Sieve reject rule** pushed via ManageSieve (RFC 5804).
4. **Logs** every classification + action (account, folder, uid, scores,
   action) as JSON lines — see Audit log below.
5. **Trains itself** from Junk-folder activity (two-signal feedback loop,
   see below).

## Two-signal continuous training

The Bayesian classifier only improves with feedback.  The daemon watches
the Junk folder and learns from **independent** observations:

| Signal | Source | Label |
|---|---|---|
| Spam | New Junk arrivals **not moved by the daemon** (Migadu gateway verdicts, manual moves) | train spam |
| Ham | Messages that leave Junk and reappear in INBOX (user says "not spam") | train ham |

The daemon's own moves are recorded in the `daemon_moves` table and
**excluded** from training (matched by Message-ID — after an IMAP MOVE the
UID changes) — no self-confirmation bias.  Each message trains at most once
per label+source.

## Install & deploy (systemd)

```bash
# 1. Service user + venv (paths per your box)
sudo useradd --system --home /var/lib/mailwatch --shell /usr/sbin/nologin mailwatch
sudo mkdir -p /opt/mailwatch /var/lib/mailwatch /etc/mailwatch
sudo chown mailwatch:mailwatch /var/lib/mailwatch

# 2. Install
sudo python3 -m venv /opt/mailwatch/venv
sudo /opt/mailwatch/venv/bin/pip install /path/to/mailwatch

# 3. Config
sudo cp config.example.toml /etc/mailwatch/config.toml
sudo editor /etc/mailwatch/config.toml      # add accounts

# 4. Passwords → system keyring (per account, prompts on stdin)
sudo -u mailwatch /opt/mailwatch/venv/bin/mailwatch password set me@ronzz.org
sudo -u mailwatch /opt/mailwatch/venv/bin/mailwatch password check me@ronzz.org

# 5. systemd unit (see systemd/mailwatch.service)
sudo cp systemd/mailwatch.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mailwatch

# 6. Dry-run before going live:
sudo -u mailwatch /opt/mailwatch/venv/bin/mailwatch run --config /etc/mailwatch/config.toml --once --dry-run
```

> The systemd unit hardens the daemon (`ProtectSystem=strict`,
> `ReadWritePaths=/var/lib/mailwatch`, `PrivateTmp`, no new privileges).
> It is outbound-only — no exposed ports.

## Config

`/etc/mailwatch/config.toml` (see `config.example.toml`):

```toml
[daemon]
dry_run = false                 # classify + log only; never move/push
log_level = "INFO"
feed_refresh_hours = 6          # phishing feed refresh interval (0=off)
spam_threshold = 0.9            # combined score to move to Junk
catch_up_scan_seconds = 300     # UNSEEN rescan guard (missed IDLE events)
junk_folder = "Junk"            # Migadu default

[daemon.training]
enabled = true
scan_interval_seconds = 120     # Junk reconciliation (ham moves)
train_spam_on_junk_arrival = true
train_ham_on_junk_to_inbox = true

[daemon.auto_block]
enabled = false                 # opt-in Sieve auto-block
hits_threshold = 3              # domain blocked after N spam classifications
window_days = 14
script_name = "mailwatch_blocks"

[[accounts]]
email = "me@ronzz.org"
imap_host = "imap.migadu.com"   # Migadu defaults shown
imap_port = 993
imap_use_ssl = true
# username = ""                # defaults to email
# junk_folder = ""             # per-account override
sieve_host = "managesieve.migadu.com"   # needed only if auto_block.enabled
sieve_port = 4190
sieve_use_tls = true
```

## Audit log

Every event is appended as a JSON line to `<data_dir>/audit.jsonl`:

```json
{"ts":"…","event":"classification","account":"me@ronzz.org","folder":"INBOX","uid":42,
 "message_id":"<…>","from_addr":"…","subject":"…","scores":{"bayesian":0.99,…},
 "is_spam":true,"is_phishing":false,"reasons":[],"action":"move_junk"}
```

Event types: `startup`, `shutdown`, `classification`, `move`,
`sieve_block`, `sieve_block_pushed`, `train`, `feed_update`.
`"dry_run": true` is set on every line when running with `--dry-run`.

## CLI

```
mailwatch run --config FILE [--once] [--dry-run] [--debug]   # daemon (or single pass)
mailwatch password set|check EMAIL                           # keyring management
```

- `--once`: one classification + training pass per account, then exit
  (ops/testing).
- `--dry-run`: classify + audit only — never MOVE or push Sieve.
- Single-instance lock: a second `run` refuses to start
  (`flock` on `<data_dir>/mailwatch.lock`).

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest tests/            # 134 tests (includes ported lighterbird tests)
.venv/bin/ruff check src tests
```

The `tests/` suite reuses lighterbird's `tests/test_email/` tests where
possible (spam detector, phishing, similarity, capabilities), adapted to
the daemon's schema (no `accounts`/`messages` tables).

## Live end-to-end test (`e2e/live_e2e.py`)

Proves the daemon's four behaviors against a **real** mailbox (requires
the daemon running + the password in the keyring):

1. **Spam filtering + MOVE** — a synthetic spam message is classified
   (score ≥ threshold) and UID-MOVE'd to Junk.
2. **Ham filtering** — a synthetic neutral message stays in INBOX
   (audit `action=none`).
3. **Spam training** — a message moved to Junk *not by the daemon*
   (simulated gateway/user junk mark) trains the Bayesian classifier
   (`spam_feedback` `junk_idler`).
4. **Ham training** — the same message moved back to INBOX ("not spam")
   trains ham (`spam_feedback` `junk_to_inbox`).
5. **Bias guard** — daemon-moved messages never train.

All synthetic messages (`mailwatch-e2e-*` Message-IDs) are deleted from
the mailbox afterwards; their `spam_feedback`/`daemon_moves` rows are
purged.  Refuses to run without `MAILWATCH_E2E=1`.

```bash
sudo -u mailwatch HOME=/var/lib/mailwatch MAILWATCH_E2E=1 \
    /opt/mailwatch/venv/bin/python /opt/mailwatch/e2e/live_e2e.py \
    --account ron@ronzz.org
# --scan-interval must match daemon.training.scan_interval_seconds (drives
# the ham-step wait); the ham signal needs one full reconciliation scan.
```

Exit code 0 = all steps green.

## Migadu specifics

- IMAP: `imap.migadu.com:993` (SSL), IDLE + UID MOVE supported (Dovecot).
- ManageSieve: `managesieve.migadu.com:4190` (verified working by the
  SnappyMail deploy, README §7).
- Sieve scripts use the core `address` test, **not** the `envelope`
  extension (community-reported broken on Migadu — Postfix/Dovecot on
  separate servers).
- Junk folder is named `Junk` on Migadu.
