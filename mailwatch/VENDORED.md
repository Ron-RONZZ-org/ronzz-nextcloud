# Vendored code provenance

The `src/mailwatch/email/` package is derived from
[lighterbird](https://github.com/Ron-RONZZ-org/lighterbird)
(`Ron-RONZZ-org/lighterbird`), commit
**`99baa8ef7d2ab8711ecf7ae64d4d70fd5f9317d8`** (2026-08-17).

Per issue #2's decision ("reuse via git subtree/submodule or vendor the
needed modules — do not fork-and-drift"): the modules are **vendored**
because lighterbird's email module imports `lighterbird.core.*` and the
separate `lightercore` package, which would drag FastAPI/uvicorn/openai/
sqlite-vec/fastembed into a daemon that needs none of them.  Subtree
merges from a discontinued upstream add no value.  Vendoring keeps the
surface minimal; provenance headers + this file prevent silent drift.

## What was vendored (with adaptation)

| File | Adaptation |
|---|---|
| `email/imap/capabilities.py` | import rewrite only |
| `email/imap/helpers.py` | import rewrite only |
| `email/imap/parser.py` | import rewrite only |
| `email/imap/client.py` | trimmed: dropped full sync engine (`sync_folder`), attachment storage, folder CRUD, message deletion, DB-backed lazy fetch; added slim `fetch_uids()` and `fetch_message_ids()` |
| `email/imap/idle.py` | `folder` parameter added so the same manager watches INBOX and Junk; **IDLE entry fixed for Python 3.11+ (2026-08-17)**: imaplib's `_command("IDLE")` raises `KeyError` (`IDLE` absent from `imaplib.Commands`) and `IMAP4` has no `fileno()` — IDLE is now entered by sending the command manually and `select` polls `conn.sock` (both verified against live `imap.migadu.com`) |
| `email/filters/spam_detect.py` | `lightercore.paths.config_dir` → `mailwatch.paths.config_dir` |
| `email/filters/spam_tokens.json` | copied verbatim (2005 SpamAssassin seed) |
| `email/filters/phishing.py` | import rewrite + **feed fixes (2026-08-17)**: `follow_redirects=True` (OpenPhish/PhishTank answer 302), phishtank URL `https`, phishstats switched to the JSON API (`api.phishstats.info/api/phishing`, `_process_feed` gained a `json` format) — the old `phish_score.csv` is 404 (+ `phishing_brands.json` copied) |
| `email/filters/spam_similarity.py` | `add_spam()` takes `from_addr` directly instead of querying the dropped `messages` table |
| `email/filters/sieve.py` | import rewrite only |
| `email/keyring.py` | rewritten against the `keyring` package directly (lighterbird delegated to `lighterllm`) |
| `email/db.py` | **shim only** — re-exports `mailwatch.db` (the daemon's own schema) |

## What was deliberately NOT vendored

- `email/services/*` (accounts, messages, msg_ops, backlog, dead_letter,
  flag_sync, msg_send, sieve_crud, sieve_remote) — the daemon moves
  messages via `IMAPClient.move_message()` directly and pushes Sieve via
  `SieveManager` directly; no local CRUD services needed.
- `email/imap/sync.py`, `storage.py`, `connpool.py` — full-sync engine.
- `email/smtp.py`, `email/undo.py`, `server_detect.py` — out of scope
  (no send, no GUI).
- `filters/spam.py` (`SpamManager`) — its `to_sieve()` uses the
  `envelope` extension, reported broken on Migadu.  Replaced by
  `mailwatch/sieve_block.py` (header-based `address` rules).

## Upstream drift policy

lighterbird is **discontinued** (owned by Ron-RONZZ-org, no active
development).  If a future upstream change must be merged:

1. Diff the new upstream file against the vendored copy.
2. Apply import rewrites (`lighterbird.` → `mailwatch.`) + the
   adaptations listed above.
3. Update this file with the new commit hash.
4. Re-run the test suite (ported tests cover the detectors).
