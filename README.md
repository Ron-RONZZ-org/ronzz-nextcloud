# Ronzz.ORG Nextcloud — Deployment & Operations

> Single source of truth for the Ronzz.ORG Nextcloud instance. Covers the full stack:
> deployment, configuration, applications, backups, upgrades, and day-to-day operations.
> Server-side companion notes are kept locally at `docs/IT/ronzz-linux-server-2.md`
> (not in this repo).

| | |
|---|---|
| **URL** | `https://dashboard.ronzz.org` |
| **Version** | Nextcloud 34.0.2 (34.0.2.1) |
| **Server** | OCI Ampere A1.Flex, 4 vCPU / 11 GiB RAM, 100 GB boot volume — `ronzz-linux-server-2` |
| **Deployed** | 2026-08-14 |
| **Stack** | Docker Compose: `nextcloud:34-apache` + `postgres:17-alpine` + `redis:7-alpine` |
| **Edge** | Host nginx (systemd) → `127.0.0.1:8080`, TLS via acme.sh / Let's Encrypt |
| **Webmail** | `https://webmail.ronzz.org` — SnappyMail 2.38.2 (PHP-FPM, no DB) — see §7 |
| **Help desk** | `https://threads.ronzz.org` — Hesk 3.6.4 issue tracker (PHP-FPM, MySQL) — see §8 |
| **OS** | Ubuntu 24.04.3 LTS (aarch64) |

---

## 1. Architecture

```
Internet ──443──▶ host nginx (systemd, existing) ──127.0.0.1:8080──▶ [nextcloud:34-apache]
                │ dashboard.ronzz.org, LE cert                   │          │
                │ websockets (Talk)                              │    db: postgres:17-alpine (internal)
                                                                │    redis:7-alpine (locks/cache)
```

- Everything runs in Docker on one host; PostgreSQL and Redis are **not exposed** outside the Compose network.
- The host nginx reuses the existing acme.sh / Let's Encrypt pipeline — no new TLS tooling.
- Deployment dir: `/opt/nextcloud/` on the server (`docker-compose.yml`, `.env`, `backup.sh`).

## 2. Infrastructure (OCI)

| Item | Value |
|---|---|
| Instance | `ronzz-linux-server-2` — OCI `eu-paris-1`, AD-1, shape `VM.Standard.A1.Flex` (ARM64) |
| Boot volume | 100 GB (expanded from 46.6 GB, 2026-08-14) — within Always Free 200 GB allowance |
| Volume backups | Policy `daily-5d` (03:00 UTC, incremental, 5-day retention) — OCI console |
| Identity | User OCID / tenancy / API key + fingerprint documented in `~/Syncthing/oci/ronzz-linux-server-2.md` |
| OCI SDK | `/tmp/opencode/resize_boot_volume.py` + venv `/tmp/opencode/ocivenv` (used for the volume resize) |

> **Disk growth:** when `df -h /` crosses ~60%, attach an OCI block volume and move `nc_data` — no reinstall needed.

## 3. DNS & TLS

- **DNS:** A record `dashboard.ronzz.org → 158.178.193.231`, **grey cloud (proxied: false)** — Cloudflare API (zone `ronzz.org`, record id `7371ab210ed1b15ac16a3181d7dd5045`). Grey cloud is required for large file uploads (proxy caps ~100 MB).
- **DNS:** A record `webmail.ronzz.org → 158.178.193.231`, **grey cloud (proxied: false)** — Cloudflare API (zone `ronzz.org`, record id `7ced9195776f299042c9e9baa43bb21b`) — SnappyMail webmail (see §7).
- **TLS:** Let's Encrypt ECC cert, issued via acme.sh (`--home /etc/letsencrypt`, `--ecc`), installed to `/etc/letsencrypt/dashboard.ronzz.org/{fullchain,privkey}.pem`.
- **Renewal:** existing root cron `acme.sh --cron --home /etc/letsencrypt` (reloads nginx via reloadcmd).
- **nginx vhost:** `/etc/nginx/sites-available/dashboard.ronzz.org.conf` — 10 G upload limit, websocket upgrade headers (Talk), HTTP→HTTPS redirect.

## 4. Deployment (Docker Compose)

`/opt/nextcloud/docker-compose.yml` — services: `nextcloud` (apache, `127.0.0.1:8080:80`), `db` (postgres:17-alpine, `shared_buffers=256MB`, `max_connections=50`), `redis` (7-alpine, 128 MB LRU cap).

Named volumes (not project-prefixed, so backup scripts can reference them):

| Volume | Purpose |
|---|---|
| `nc_www` | Nextcloud code + `custom_apps` + `config` |
| `nc_data` | **User files** — the volume to back up and grow |
| `db_data` | PostgreSQL data |
| `redis_data` | Redis persistence |

**Environment** (`.env`, perms 600, root only): `POSTGRES_PASSWORD`, `NC_ADMIN_USER` (initial admin — now disabled), `NC_ADMIN_PASSWORD`. Container env: `POSTGRES_HOST/DB/USER`, `REDIS_HOST`, `TRUSTED_PROXIES=127.0.0.1`, `OVERWRITEPROTOCOL=https`, `OVERWRITEHOST=dashboard.ronzz.org`, `NEXTCLOUD_TRUSTED_DOMAINS`, `PHP_MEMORY_LIMIT=512M`, `PHP_UPLOAD_LIMIT=10G`.

### Upgrade procedure (minor within major)

```bash
cd /opt/nextcloud
docker compose pull
docker compose up -d
docker exec -u www-data nextcloud php occ upgrade
```

- Pin the major tag (`nextcloud:34-apache`); minor releases arrive on `pull`.
- **Major upgrade** (34 → 35): pull the new tag, `up`, `occ upgrade`, then update this doc.
- DB-stored config (theming, disabled apps, custom CSS) survives all upgrades.

## 5. Initial configuration (already applied)

```bash
# occ alias
docker exec -u www-data nextcloud php occ <cmd>

# Lean profile — telemetry & unused apps off
occ app:disable survey_client firstrunwizard
# (user_ldap, externalstorage not installed; federatedfilesharing is a required
#  dependency of file sharing and cannot be disabled — harmless)

# Retention policy: delete everything older than 30 days,
# prune younger versions per the built-in time-window pattern
occ config:system:set versions_retention_obligation --value="auto, 30"
occ config:system:set trashbin_retention_obligation   --value="auto, 30"

# Background jobs via cron (NOTE: command is background:cron, not background-job:cron, in NC 34)
occ background:cron
occ config:system:set defaultapp --value="dashboardlauncher"   # ENT-style portal landing page (see §6.1)
```

Host crontab (root): `*/5 * * * * docker exec -u www-data nextcloud php cron.php`

## 6. Applications

| App | Status | Notes |
|---|---|---|
| **Talk** | ✅ enabled (24.0.4) | Store id is **`spreed`** — `occ app:install talk` fails ("not found"); install with `occ app:install spreed`. Default signaling is fine for small teams; add coturn only if video calls fail behind strict NAT. |
| Office | ⛔ disabled | Shipped core app — `occ app:remove` refuses; disabled is equivalent. |
| Files, Dashboard, Activity, Text, Photos | ✅ | Default set. |
| **Calendar** | ✅ (6.5.3) | Installed 2026-08-16 — previously listed in this doc but actually missing from the instance. |
| **Contacts** | ✅ (8.7.6) | Installed 2026-08-16 (see Calendar). |
| **Deck** | ✅ (1.18.3) | Installed 2026-08-16 (see Calendar). |
| **Forms** | ✅ (5.3.5) | Installed 2026-08-16 via app store (portal tile §6.1). |
| **Announcement Center** | ✅ (7.5.0) | Installed 2026-08-16 via app store (portal tile §6.1). |
| **Whiteboard** | ✅ (1.5.9) | Installed 2026-08-16 via app store. ⚠️ **Crash source:** initial install segfaulted every Apache worker (opcache compiled the PHP files mid-write; 502 for ~30 min). Fixed by `docker restart nextcloud` (clears opcache); safe re-enable afterwards — see §13. File-type app (no standalone page) — portal tile creates a **new** `.whiteboard` per click via `/apps/dashboardlauncher/new-whiteboard` (§6.1), files land in the shared `Whiteboards/` folder (renamed from `Tableaux blancs` 2026-08-16 — ASCII/no-space). Requires a separate collaboration server for real-time; basic use works without. |
| **Dashboard Launcher** | ✅ (1.5.0) | ENT-style portal — see §6.1. Store install fails ("invalid signature"); installed manually into `custom_apps/` (AGPL, `github.com/dpfpic/dashboardlauncher`). |

### 6.1 Portal (Dashboard Launcher) — post-login landing page

- `defaultapp` → `dashboardlauncher`: every login lands on `/apps/dashboardlauncher/`, an ENT-style **button grid** of available apps (note: no search bar — tiles only; a search filter could be added as a small patch later).
- Buttons live in DB table `oc_dashboardlauncher_buttons` (`titre`, `icone`, `route`, `ordre`, `groupes` JSON array of groups, `actif`, `taille`). Routes use the `apps/<id>` form (the page sits 2 path levels deep, so the template's `../../<route>` resolves to `/apps/<id>/`).
- **External-URL buttons (patch, 2026-08-16):** `templates/main.php` now detects absolute URLs (`^https?://`) — those render as-is with `target="_blank" rel="noopener noreferrer"` (the stock template hardcodes `../../`, which would break `https://…` routes). Used by the Webmail tile → `https://webmail.ronzz.org` (see §7). Backward compatible with `apps/<id>` routes.
- **Deep-link buttons (patch, 2026-08-16, superseded same day):** file-type apps with no standalone page (e.g. Whiteboard) can deep-link into the Files app as the route — `apps/files?openfile=<fileid>` opens the target file in its registered editor. Initially used for Whiteboard (starter file in `Tableaux blancs/`), replaced within hours by the "New whiteboard" helper below (the starter file was deleted, the folder renamed to `Whiteboards/`).
- **"New whiteboard" button (patch, 2026-08-16, current):** the Whiteboard tile routes to `/apps/dashboardlauncher/new-whiteboard`, a server-side helper (`PageController::newWhiteboard()`) that creates a **timestamped** `.whiteboard` file in the shared `Whiteboards/` folder via the DirectEditing API and 302-redirects to the fresh editor — one click = always a new canvas, for every account. Requires the files from `dashboardlauncher/` in this repo (see below). Rationale: Whiteboard is a file-type app with no standalone page; the DirectEditing create API is POST-only, so a plain `<a>` tile link can't trigger it. Patched files: `lib/Controller/PageController.php` (+`newWhiteboard()`, injects `IURLGenerator`/`IDirectEditingManager`/`IRootFolder`/`IEventDispatcher`/`LoggerInterface`; dispatches `RegisterDirectEditorEvent` before `create()`) and `appinfo/routes.php` (+route `page#newWhiteboard` → `/new-whiteboard` GET).
- **External-tile spinner fix (patch, 2026-08-16):** `js/dashboardlauncher-loader.js` shows a wait-spinner and freezes the grid on tile click, unfreezing on `pageshow` (back button). External tiles (`target="_blank"`) never navigate the page, so `pageshow` never fires and the portal stayed stuck in the loading state after opening Webmail in a new tab. Fix: skip the freeze/spinner when `link.target === '_blank'`.
- Icons (2026-08-16): official Nextcloud app glyphs — dark variants where shipped (`app-dark.svg`, `deck-dark.svg`, `activity-dark.svg`, `spreed/app-dark.svg`), else the app's `app.svg` inverted `#fff`→`#000` (calendar, contacts, photos). Stored as `icon_<sha1:16>.<ext>` in appdata `data/appdata_<instanceid>/dashboardlauncher/icons/` (the `icone` value is the filename). `.button-icon` CSS has no invert filter, so only dark glyphs are visible on the white portal background. Webmail tile uses the NC Mail app's envelope SVG inverted to black.
- Site text stored in appconfig: `occ config:app:set dashboardlauncher site_title|welcome_text|footer_text --value="…"` — `{displayName}` is interpolated server-side.
- **Upgrade re-apply:** the nextcloud docker image does **not** touch `custom_apps/`, so these patches survive image upgrades. Only if the dashboardlauncher app itself is re-installed/updated from GitHub, re-copy the tracked files from `dashboardlauncher/` in this repo over `custom_apps/dashboardlauncher/` (and re-apply the `templates/main.php` external-URL patch if overwritten — see below).
- Admin UI: **Settings → Administration → Dashboard Launcher** — title/welcome/footer, add/reorder/group-restrict buttons, upload icons.
- Buttons as of 2026-08-16: Fichiers, Calendrier, Contacts, Deck, Photos, Talk, Activité, Webmail, **Formulaires, Annonces, Tableau blanc** (added 2026-08-16, same day). (Text omitted — no standalone page route in NC 34, it's an embedded editor.)
- The widgets Dashboard app stays enabled at `/apps/dashboard/` — it's just no longer the landing page.

## 7. Webmail — webmail.ronzz.org (SnappyMail)

> Self-hosted webmail UI for Migadu-hosted `@ronzz.org` mail (issue #4; part of the webmail master issue #3 — SnappyMail + lighterbird idler + custom plugins).
> The UI connects **outbound** to Migadu IMAP/SMTP — no mail server runs here.

| | |
|---|---|
| **URL** | `https://webmail.ronzz.org` (HTTP → HTTPS 301) |
| **Version** | SnappyMail 2.38.2 (release zip, GPG-verified against `releases@snappymail.eu`) |
| **Deployed** | 2026-08-16 |
| **Stack** | PHP 8.3-FPM (dedicated `snappymail` pool), no DB, no Docker |
| **Webroot** | `/var/www/snappymail` |
| **Data dir** | `/var/lib/snappymail` — outside webroot (`APP_DATA_FOLDER_PATH`), user `snappymail`, 0700 |
| **Mail server** | Migadu — IMAP `imap.migadu.com:993` (SSL), SMTP `smtp.migadu.com:465` (SSL), ManageSieve `managesieve.migadu.com:4190` |

### 7.1 Architecture

```
Internet ──443──▶ host nginx (systemd) ──fastcgi──▶ PHP-FPM 8.3 (pool snappymail, /run/php/snappymail-fpm.sock)
                 webmail.ronzz.org                      │  data: /var/lib/snappymail (0700)
                 LE ECC cert                            │  ──IMAP/SMTP/Sieve──▶ Migadu (ronzz.org mailboxes)
```

- **Native PHP-FPM, not Docker** — matches the wikibase pattern on this box; upgrade = overwrite files; no ARM64 image dependency (official image `djmaze/snappymail` is multi-arch, but unnecessary here).
- The data dir holds user settings, **encrypted mailbox passwords (Sodium)**, caches, contacts — the only state to back up.
- **Vendor-risk note (from #3):** single-maintainer project, last release 2024-10 (repo commits through 2026-03). Mitigation: keep all custom logic in the idler backend + thin plugins so the UI stays replaceable.

### 7.2 DNS & TLS

- **DNS:** A record `webmail.ronzz.org → 158.178.193.231`, grey cloud — Cloudflare API, record id `7ced9195776f299042c9e9baa43bb21b` (see §3).
- **DNS (admin panel):** A record `webmail-admin.ronzz.org → 158.178.193.231`, grey cloud — Cloudflare API, record id `d541bd14bc7fd58df96428b95b55c52d` (2026-08-16; OIDC-gated panel, §7.8).
- **TLS:** Let's Encrypt ECC cert via acme.sh **DNS-01** (`--dns dns_cf`, Cloudflare token), installed to `/etc/letsencrypt/webmail.ronzz.org/{fullchain,privkey}.pem`; auto-renewed by the existing root cron (`acme.sh --cron`). Domain conf: `/etc/letsencrypt/webmail.ronzz.org_ecc/webmail.ronzz.org.conf`.
- **nginx:** `/etc/nginx/sites-available/webmail.ronzz.org.conf` (symlinked) — HTTP→HTTPS, security headers, `deny /data/`, dotfile block, 50M upload cap, fastcgi to the snappymail socket.

### 7.3 Deployment & config

- **Install:** extract the release zip into `/var/www/snappymail` (owner `www-data`); `data/` moved out of the webroot to `/var/lib/snappymail` via `include.php` (`APP_DATA_FOLDER_PATH` — **must end with `/`**, see §13).
- **PHP-FPM pool:** `/etc/php/8.3/fpm/pool.d/snappymail.conf` — user `snappymail`, socket `/run/php/snappymail-fpm.sock`, `pm.max_children = 4`.
- **Config files** (the admin UI writes the same files — direct edits are fine):
  - `application.ini` (`/var/lib/snappymail/_data_/_default_/configs/`) — admin credentials, `default_domain = "ronzz.org"` (bare logins map to ronzz.org), `force_https = On`, title/loading branding.
  - `domains/default.json` — Migadu defaults: IMAP 993 / SMTP 465 / Sieve 4190, `type: 1` (implicit SSL), **cert verification ON** (`verify_peer`/`verify_peer_name`).
  - `domains/ronzz.org.json` — explicit per-domain config (copy of `default.json`; SnappyMail resolves a domain only via its exact file, an alias, or the `default.json` wildcard — see §13).
  - **Domain whitelist (2026-08-16):** both `default.json` and `ronzz.org.json` carry `"whiteList": "@ronzz.org"` — **login is restricted to @ronzz.org addresses** (bare logins get `default_domain` appended). Any other domain is rejected at the domain gate ("not whitelisted"); the shipped `disabled` list additionally blocks gmail/hotmail/outlook/qq/yahoo. No IP restriction — works off-premise (§7.4).
- **Login model (decided, #7 — supersedes #3): unified password, synced from Nextcloud.** The Ronzz NC account password **is** the Migadu mailbox password: webmail login uses the same email + NC password, validated by SnappyMail against Migadu IMAP. No separate mailbox password, no SnappyMail-managed user administration (accounts self-provision on first login). The NC app `nc_migadu_password_sync` propagates every NC password change to the mailbox through the Migadu API — see §7.7. ⚠️ **Data-dir ownership pitfall:** everything under `/var/lib/snappymail` must be owned by the FPM pool user `snappymail` — a `www-data`-owned `domains/*.json` silently breaks login with *"has no domain configuration"* (fixed 2026-08-16; see §13).

### 7.4 Security

- **Admin panel (2026-08-16): gated behind Nextcloud login via OIDC.** The panel moved to its own hostname `https://webmail-admin.ronzz.org` (`admin_panel.host` in `application.ini` — the old `/?admin` on `webmail.ronzz.org` no longer opens the panel). Access requires a valid **Nextcloud session** (NC acts as OIDC IdP via the `H2CK/oidc` app; login includes NC TOTP if enabled): nginx `auth_request` → OIDC-RP sidecar (`/opt/webmail-oidc`, systemd `webmail-oidc`) → session cookie. The previous nginx IP allowlist + static admin password are superseded. Full runbook: **§7.8**; artifacts in `webmail/webmail-admin-oidc/` and `webmail/patches/`.
- The old static credentials still exist as a fallback: `admin_login`/`admin_password` in `application.ini` (bcrypt; the panel → Security section can also set `admin_totp` for an extra factor).
- Hardening options after provisioning: `allow_admin_panel = Off` in `application.ini` disables the panel entirely (re-enable: flip to `On`; no service reload needed — config is read per request).
- HTTPS enforced at nginx (301) and app level (`force_https`). Data dir is 0700 outside webroot. User SMTP server settings keep the Migadu default (prevents data exfiltration to arbitrary hosts).
- `APP_REMOTE_HOST_WHITE_LIST` (mentioned in #4) is **not** a SnappyMail option — the equivalent built-ins are the Fetch-Metadata request checks (on by default; `secfetch_allow = "site=same-site"` was added for the OIDC redirect navigation).
- **Login domain restriction (2026-08-16):** webmail accepts **@ronzz.org accounts only** — enforced by `"whiteList": "@ronzz.org"` in `domains/default.json` + `domains/ronzz.org.json` (works from anywhere; no IP gate). External-domain logins are rejected at the domain gate before any IMAP connection, so the instance can't be used as a generic webmail proxy. The SnappyMail driver is file-based `DefaultDomain` (no autoconfig auto-discovery in this install). The shipped `domains/disabled` list additionally blocks gmail/hotmail/outlook/qq/yahoo.
- **Password-reuse tradeoff (accepted, #7):** the NC password also guards email. NC TOTP protects the portal **only** — IMAP cannot do TOTP. Mitigation: strong, unique master password. A sync failure never blocks the NC password change; divergence is logged and recoverable (§7.7).

### 7.5 Upgrade (incl. admin-OIDC patch replay)

```bash
cd /tmp && wget https://github.com/the-djmaze/snappymail/releases/download/v<ver>/snappymail-<ver>.zip
# GPG-verify (key 1016E47079145542F8BA133548208BA13290F3EB) before extracting
sudo unzip -o -q snappymail-<ver>.zip -d /var/www/snappymail   # only index.php + data/VERSION are overwritten
sudo chown -R www-data:www-data /var/www/snappymail
# re-apply the tracked admin-OIDC patch (§7.8; upstream PR the-djmaze/snappymail#2066 — safe failure if it drifts)
cd /var/www/snappymail/snappymail/v/<ver>/app/libraries/RainLoop
sudo patch -p1 < /path/to/ronzz-nextcloud/webmail/patches/snappymail-admin-oidc.patch
sudo php -l Actions/Admin.php && sudo php -l ActionsAdmin.php
```

If `patch` fails (drift), the panel stays password-only until the patch is rebased — the OIDC gate simply doesn't open the panel (safe failure).

### 7.6 Backup

- Nightly root cron `35 3 * * *`: `tar czf /var/backups/snappymail/data-<ts>.tgz -C /var/lib snappymail`, 7-day retention (covers settings, encrypted passwords, contacts; mailbox data itself lives on Migadu).
- Plus OCI `daily-5d` boot-volume snapshots (crash-consistent).

### 7.7 Unified login — NC ↔ Migadu mailbox lifecycle sync (app `nc_migadu_password_sync`)

> **Model:** the NC account password *is* the Migadu mailbox password. The app mirrors the **NC user lifecycle** to Migadu: every password change (personal settings GUI, admin reset, `occ user:resetpassword`, email reset link) or user creation with a password is pushed to the matching Migadu mailbox (created on the fly if missing) via the Migadu API; every NC user deletion deletes their Migadu mailbox. Webmail keeps validating against Migadu IMAP, so no SnappyMail change was needed. Source: `webmail/nc-migadu-password-sync/` in this repo; Migadu API reference: `docs/IT/migadu-api.md` (server-side).

**How it works:** `User::setPassword()` is the single funnel for all password-change paths → `PasswordUpdatedEvent` (plaintext) → app listener → `PasswordSyncProvider` (interface) → `MigaduProvider` → Migadu API with Basic auth (GET/POST/PUT mailbox, DELETE on user removal). The mailbox is the user's primary email (`user@ronzz.org` → `user`); users with no email, a foreign domain, or an id in `nc_migadu_password_sync_exclude` are skipped.

**Mailbox lifecycle (v1.2.0+):**
- **User added** → `UserCreatedEvent` (with password) → sync path: `GET` the mailbox → `404` → `POST` create (`local_part`, `name` = display name, `password`) → webmail works immediately. If the user was created *without* a password (Migadu requires one to create a mailbox), nothing happens yet — the mailbox is auto-created on the **first password change** (`PUT` after a `404` GET, so a missing mailbox never blocks a password sync). Same self-heal covers legacy users and the old "mailbox lookup 404" failure.
- **User deleted** → `BeforeUserDeletedEvent` captures which mailbox belongs to the user (the address is no longer readable after deletion) → `UserDeletedEvent` (fired only if the NC deletion succeeded) → `DELETE` the mailbox (idempotent: 404 = already gone, and Migadu's quirk of returning HTTP 500 on a successful delete is treated as success). ⚠️ **Destructive and irreversible** — the mailbox and all its mail are removed along with the NC user; this is the requested behaviour, and a safety valve exists: set `occ config:system:set nc_migadu_password_sync_delete_mailboxes --value=false` to keep mailboxes when NC users are deleted.

**Install (manual, like dashboardlauncher — unsigned app):**

```bash
# 1. App files (source: webmail/nc-migadu-password-sync/ in this repo)
docker cp webmail/nc-migadu-password-sync.tar.gz nextcloud:/tmp/
docker exec nextcloud sh -c "cd /var/www/html/custom_apps && tar xzf /tmp/...tgz && mv nc-migadu-password-sync nc_migadu_password_sync && chown -R www-data:www-data nc_migadu_password_sync"
# 2. Enable
docker exec -u www-data nextcloud php occ app:enable nc_migadu_password_sync
```

**Config (system config, `config.php` — the API key must never go in the DB):**

```bash
docker exec -u www-data nextcloud php occ config:system:set nc_migadu_password_sync_api_email --value=<migadu-account-email>   # Migadu account login (see server-side docs)
docker exec -u www-data nextcloud php occ config:system:set nc_migadu_password_sync_api_key    --value=<key>                  # Migadu Admin → My Account → API Keys
docker exec -u www-data nextcloud php occ config:system:set nc_migadu_password_sync_domain     --value=ronzz.org
docker exec -u www-data nextcloud php occ config:system:set nc_migadu_password_sync_exclude     --value=ronzzshared            # dummy email, no mailbox
docker exec -u www-data nextcloud php occ config:system:set nc_migadu_password_sync_delete_mailboxes --value=true # delete mailbox when the NC user is deleted (default true; false = keep it)
```

**Pre-flight check:**

```bash
docker exec -u www-data nextcloud php occ migadu:test
#   OK — API credentials accepted, domain visible, mailboxes listed
#   per-user lines: ron@ronzz.org → mailbox exists · ronzzshared → (excluded)
#   missing mailbox → warning (auto-created on next password sync)
#   trailing section: mailboxes without a matching NC user (orphans, informational)
```

**Bootstrap existing mailboxes** (NC stores hashed passwords — the initial push happens on the *next* password change; there is no bulk "sync all" because the app never sees old passwords):

- **Per user:** `docker exec -e OC_PASS='<new password>' -u www-data nextcloud php occ user:resetpassword <uid> --password-from-env` — fires the event → Migadu updated → webmail login works with the same password.
- Or simply let each user change their own NC password via the dashboard GUI (same event).
- Live mapping (2026-08-16): `ron@ronzz.org` → mailbox `ron@ronzz.org` (synced); `ronzzshared` → **excluded** (email `nextcloud-shared@ronzz.org` is a dummy for the shared-files account, no mailbox).

**Failure handling (divergence is graceful):** the listener never breaks the NC operation (password change or user deletion). On Migadu API failure it retries 3× (1 s / 2 s backoff), then logs at **error** level with user context — `migadu_sync: password sync FAILED for user <uid> …` or `migadu_sync: mailbox deletion FAILED for user <uid> …` (the password is never logged). A failed password sync leaves the mailbox on its previous password — webmail keeps working with it; **recovery:** fix the cause, then re-run `occ user:resetpassword <uid>` (any new password re-fires the sync). A failed mailbox deletion leaves the mailbox in place — recover by checking the cause (credentials, toggle) and re-deleting the NC user, or by deleting the mailbox manually in Migadu Admin. Verify readiness with `occ migadu:test`.

**Passwords policy:** a chosen password must satisfy both NC and Migadu constraints — the Migadu API rejects too-short/weak passwords (HTTP 4xx, logged as a sync failure), so use NC's default ≥ 8-char policy at minimum.

**Provider portability (decided, #7):** all sync logic sits behind the `PasswordSyncProvider` interface; Migadu is one driver behind it. Moving mail hosts later = one new driver + mail migration (`imapsync`) — the migration, not the code, dominates.

**Webmail side:** nothing to configure in SnappyMail — login validates against Migadu IMAP, so the synced password just works. One prerequisite: SnappyMail must resolve the mail domain — an explicit `domains/ronzz.org.json` (copy of `default.json`) is in place; keep data-dir files owned by `snappymail` (§7.3, §13). The SnappyMail admin panel lives on `webmail-admin.ronzz.org`, gated behind NC login via OIDC (§7.8).

### 7.8 Admin panel OIDC bridge (runbook)

> The SnappyMail admin panel (`webmail-admin.ronzz.org`) is gated behind
> **Nextcloud login via OIDC** (NC = IdP through the `H2CK/oidc` app). This
> replaces the old `/?admin` + nginx IP allowlist + static password model (§7.4).

**Components** (live since 2026-08-16; source in `webmail/webmail-admin-oidc/`):
NC `oidc` app v2.0.7 (IdP) → OIDC client `webmail-admin` (`occ oidc:create`, confidential, code flow) → sidecar `/opt/webmail-oidc` (FastAPI, systemd `webmail-oidc`, cookie `wmauth`, sessions in `/var/lib/webmail-oidc/sessions/`) → nginx `auth_request` on the `webmail-admin.ronzz.org` vhost → SnappyMail patch (`webmail/patches/snappymail-admin-oidc.patch`, re-applied on upgrade per §7.5).

**Config (server-side, secrets never committed):**
- `/opt/webmail-oidc/webmail-oidc.env` (root 600): `WMA_CLIENT_ID`, `WMA_CLIENT_SECRET` (from `occ oidc:create`), `WMA_REDIRECT_URI=https://webmail-admin.ronzz.org/_oidc/callback`, `WMA_ALLOWED_UIDS=ron@ronzz.org` (comma-separated NC uids allowed to open the panel)
- `application.ini`: `admin_panel.host = "webmail-admin.ronzz.org"` (panel only on the admin host) and `secfetch_allow = "site=same-site"` (OIDC redirect navigation)
- DNS: A `webmail-admin.ronzz.org → 158.178.193.231` (grey cloud, CF record id `d541bd14bc7fd58df96428b95b55c52d`); LE cert via acme.sh DNS-01 → `/etc/letsencrypt/webmail-admin.ronzz.org/{fullchain,privkey}.pem`

**Flow:** `/?admin` → nginx `auth_request` → sidecar cookie check → 401 → bounce to NC authorize (`dashboard.ronzz.org/apps/oidc/authorize`) → NC login (incl. TOTP) → callback → ID-token validation (JWKS RS256, iss/aud/nonce) → allowlist check → session → panel opens. `X-NC-Admin` is set by nginx only after a successful auth_request; the PHP location overrides any client-supplied value; the patch honors it only when `Host` = `admin_panel.host`.

**Operations:**
- Restart: `sudo systemctl restart webmail-oidc` · Logs: `sudo journalctl -u webmail-oidc -f`
- Health: `curl http://127.0.0.1:8015/_oidc/health`
- Grant/revoke panel access: edit `WMA_ALLOWED_UIDS` in the env file → restart (sessions stay valid until TTL; sessions are not revoked by NC logout — documented tradeoff)
- Add a second admin: add their NC uid to `WMA_ALLOWED_UIDS` — they log in with their own NC credentials (and their own TOTP), no shared password
- Reset everything: stop/disable `webmail-oidc`, remove `admin_panel.host` + `secfetch_allow`, revert the patch (restore from the upgrade zip), panel returns to the static-password model

**Troubleshooting:** 403 "Disallowed Sec-Fetch" after login → `secfetch_allow` not set; 500 on callback → sidecar venv missing `cryptography` (`pip install cryptography`); redirect loop → `admin_panel.host` mismatch or `WMA_ALLOWED_UIDS` excludes the user; panel login form still shown → patch not applied (§7.5).

### 7.9 Mailwatch — lighterbird-derived IMAP IDLE spam idler (issue #2)

> Client-independent spam/phishing protection for `@ronzz.org` mail: a headless
> Python daemon that IDLEs on Migadu IMAP, classifies new mail (Bayesian +
> phishing feeds + MinHash similarity, all vendored from the discontinued
> lighterbird), MOVE's spam to Junk (visible to every client), optionally pushes
> Sieve reject rules for repeat-offender domains, and trains itself from
> Junk-folder activity.  Backend piece of the webmail master issue #3.

| | |
|---|---|
| **Source** | `mailwatch/` in this repo (issue #2) — daemon only, no UI |
| **Scope** | Classification sidecar only — deliberately does NOT sync/search/send (SnappyMail owns those) |
| **Runtime** | Python 3.11+, systemd unit (`mailwatch/systemd/mailwatch.service`), venv `/opt/mailwatch/venv` |
| **Config** | `/etc/mailwatch/config.toml` — accounts + thresholds (see `mailwatch/config.example.toml`) |
| **Credentials** | System keyring per account (`mailwatch password set <email>`) — with #7's unified login, the NC password IS the mailbox password |
| **State** | `/var/lib/mailwatch/mailwatch.db` (phishing feeds, similarity corpus, training feedback, daemon-move exclusions) |
| **Audit** | JSON-lines at `/var/lib/mailwatch/audit.jsonl` — one line per classification/action |

**Deployment (applies issue #2's systemd decision):**

```bash
sudo useradd --system --home /var/lib/mailwatch --shell /usr/sbin/nologin mailwatch
sudo mkdir -p /opt/mailwatch /var/lib/mailwatch /etc/mailwatch && sudo chown mailwatch:mailwatch /var/lib/mailwatch
sudo python3 -m venv /opt/mailwatch/venv
sudo /opt/mailwatch/venv/bin/pip install /opt/mailwatch   # from the mailwatch/ source
sudo cp /opt/mailwatch/config.example.toml /etc/mailwatch/config.toml  # then edit accounts
sudo -u mailwatch /opt/mailwatch/venv/bin/mailwatch password set me@ronzz.org
sudo cp /opt/mailwatch/systemd/mailwatch.service /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl enable --now mailwatch
```

**Dry-run first:** `sudo -u mailwatch /opt/mailwatch/venv/bin/mailwatch run --config /etc/mailwatch/config.toml --once --dry-run` — classify + audit only, no moves.

**Key behaviors (as designed in issue #2):**

- RFC 2177 IDLE on `imap.migadu.com:993` per account (29-min re-issue, exponential backoff); catch-up UNSEEN rescan as a missed-notification guard.
- Spam → `UID MOVE` to `Junk` (~30 s after arrival); phishing-feed hits are moved and logged separately (`action=move_junk_phishing`).
- Repeat-offender domain (default ≥3 spam hits / 14 days) → optional Sieve reject pushed to `managesieve.migadu.com:4190` (core `address` test only — the `envelope` extension is community-reported broken on Migadu; `auto_block.enabled` is off by default).
- **Two-signal training (bias-safe):** trains spam on Junk arrivals the daemon did NOT move (matched by Message-ID — UIDs change on MOVE) and trains ham on Junk→INBOX moves.  No self-confirmation.
- Single-instance `flock` guard; SIGTERM/SIGINT graceful shutdown.

**Ops notes:** upgrade = `pip install --upgrade /opt/mailwatch` + restart unit; check health via `systemctl status mailwatch` + tail `/var/lib/mailwatch/audit.jsonl`; phishing feeds refresh every `feed_refresh_hours` (default 6 h).

## 8. Hesk — threads.ronzz.org (lightweight issue tracker)

> Deployed 2026-08-17. Internal "odd issues" tracker (assigning + status + categories, ticket-list feel). Chosen over Zammad/Vikunja/FreeScout for footprint + ticket semantics; email intake deferred (additive later). Source: `hesk/` in this repo; server-side notes: `docs/IT/ronzz-linux-server-2.md`.

| | |
|---|---|
| **URL** | `https://threads.ronzz.org` — public submit/status pages **open**; `/admin/` **gated behind NC login via OIDC (full SSO)** |
| **Version** | Hesk 3.6.4 (PHP 8.3-FPM pool `hesk`, no Docker) |
| **DB** | MySQL `hesk` (existing local MySQL service, not NC's Postgres) |
| **Backup** | root cron 03:40 `mysqldump hesk` → `/var/backups/hesk/`, 7-day retention |

### 8.1 Auth — staff panel behind Nextcloud login (Option B full SSO)

Same OIDC pattern as §7.8, but **auto-login** instead of a shared admin gate:

- NC `H2CK/oidc` client `hesk` (confidential, code flow) → sidecar `/opt/hesk-oidc/` (FastAPI, systemd `hesk-oidc` :8016, cookie `heskauth`, `HESK_ALLOWED_UIDS=ron@ronzz.org,ronzzshared`) → nginx `auth_request` on `/admin/` only → **patched `admin/index.php`** (`hesk_oidc_auto_login()`, `hesk/patches/hesk-oidc-sso.patch` — CRLF-aware, re-apply on upgrade, safe failure = password form)
- SSO lookup matches Hesk staff `user` = **NC uid** exactly (e.g. `ron@ronzz.org`); add a staff member = create the Hesk user with the NC uid + add to `HESK_ALLOWED_UIDS`
- **SSO bypasses Hesk-side MFA — NC login (incl. TOTP) is the single second factor** (same tradeoff as §7.8)
- Artifacts: `hesk/threads-oidc/` (sidecar fork + systemd unit + nginx vhost + runbook), `hesk/patches/hesk-oidc-sso.patch`

### 8.2 Ops

```bash
sudo systemctl restart hesk-oidc        # OIDC sidecar
curl http://127.0.0.1:8016/_oidc/health # {"ok": true}
sudo systemctl restart php8.3-fpm       # after pool changes
sudo tail -f /var/log/nginx/threads*.log
```

### 8.3 Email intake (deferred — do later when wanted)

Hesk IMAP fetching is purely additive: create `tickets@ronzz.org` in Migadu → Hesk admin → Settings → Email → IMAP fetching ON (`imap.migadu.com:993`, "keep a copy" ON) → cron line hitting `inc/mail/hesk_pop3.php?key=<URL_ACCESS_KEY>`. Requires `php-imap` (`apt install php8.3-imap` + pool restart).

## 9. Branding

- Name: **Ronzz.ORG** · Slogan: **Where miracles happen.**
- Primary color `#9bf141`, background `#fdfbfb`; logo = `Ronzz-org-emblemo.png` (1308×400).
- Palette also includes `#282c35` (dark) — currently unused; candidate for custom CSS header.
- Commands: `occ theming:config <name|slogan|primary_color|background_color|logo> <value>`
  (logo accepts a path readable by `www-data` inside the container).

## 10. Users & access

| User | Role | Notes |
|---|---|---|
| `ron@ronzz.org` | Admin (group `admin`) | Primary admin account — **unified login**: NC password = Migadu mailbox password, synced (§7.7) |
| `ronzzshared` | Member | Dedicated **shared/team** account ("Ronzz Shared") — **excluded** from mailbox sync: its email `nextcloud-shared@ronzz.org` is a dummy (no Migadu mailbox) |
| `admin` | **Disabled** | Default install admin — disabled via `occ user:disable admin` (2026-08-14) |

- **App passwords:** `occ user:add-app-password <user>` — use for scripts/clients instead of the master password.
- **2FA:** enable per user under Personal → Security (TOTP, built-in).
- **Token hygiene:** after the client account-removal bug, stale desktop tokens were revoked
  (`occ user:auth-tokens:delete <user> <id>`). Verify live tokens via `occ user:auth-tokens:list <user>`.

## 11. Backups

**Nightly local backup** — `/opt/nextcloud/backup.sh`, cron `30 3 * * *`:

```bash
docker exec -u www-data nextcloud php occ maintenance:mode --on   # (trap ensures off)
docker exec nextcloud-db pg_dump -U nextcloud -d nextcloud > /var/backups/nextcloud/db-<ts>.sql
docker run --rm -v nc_data:/data:ro -v /var/backups/nextcloud:/backup \
    alpine tar czf /backup/data-<ts>.tgz -C /data .
```

- Retention: 7 days (oldest SQL/tgz removed).
- **Plus** OCI `daily-5d` boot-volume backups (crash-consistent; the local dump covers app-consistency).
- Note: `nc_www` (Nextcloud code + `custom_apps/`, incl. Dashboard Launcher) is **not** in the nightly tar — only the OCI `daily-5d` snapshots cover it.
- **Webmail (separate cron `35 3 * * *`):** `tar czf /var/backups/snappymail/data-<ts>.tgz -C /var/lib snappymail`, 7-day retention — see §7.6.

### Restore procedure

```bash
# 1. Stop & clear app
docker exec -u www-data nextcloud php occ maintenance:mode --on
docker compose stop nextcloud

# 2. Database
docker run --rm -v db_data:/data -v /var/backups/nextcloud:/backup \
    alpine sh -c 'rm -rf /data/* /data/..?* /data/.[!.]*'
docker compose up -d db && sleep 5
docker exec -i nextcloud-db psql -U nextcloud -d postgres -c "DROP DATABASE IF EXISTS nextcloud; CREATE DATABASE nextcloud OWNER nextcloud;"
docker exec -i nextcloud-db psql -U nextcloud nextcloud < /path/to/db-<ts>.sql

# 3. Data
docker run --rm -v nc_data:/data -v /var/backups/nextcloud:/backup \
    alpine sh -c 'rm -rf /data/* /data/..?* /data/.[!.]* && tar xzf /backup/data-<ts>.tgz -C /data'

docker compose start nextcloud
docker exec -u www-data nextcloud php occ maintenance:mode --off
docker exec -u www-data nextcloud php occ files:scan --all
```

> Always test the restore path before you need it.

## 12. Common operations (occ cheat sheet)

```bash
docker exec -u www-data nextcloud php occ status
docker exec -u www-data nextcloud php occ user:list
docker exec -u www-data nextcloud php occ app:list
docker exec -u www-data nextcloud php occ maintenance:mode --on|--off
docker exec -u www-data nextcloud php occ files:scan --all
docker exec -u www-data nextcloud php occ config:system:get <key>
docker exec -u www-data nextcloud php occ background:cron
```

## 13. Troubleshooting

| Symptom | Check |
|---|---|
| 502 Bad Gateway on dashboard.ronzz.org, nginx itself healthy | **Apache worker crash-loop inside the container.** `docker logs nextcloud` shows repeated `child pid N exit signal Segmentation fault (11)` while `docker ps` reports `Up`. CLI `occ` still works (opcache is CLI-disabled). **Cause:** an app installed via the app store while Apache was running — opcache cached the PHP files mid-write (hit 2026-08-16 with Whiteboard 1.5.9). **Fix:** `docker restart nextcloud` (clears opcache); verify `docker logs` shows no new segfaults, then optionally re-enable the app. Do not install store apps while the instance is serving traffic without a plan for this. |
| `occ app:install talk` → "not found" | Use `spreed` (see §6) |
| `occ background-job:cron` → "not defined" | Use `background:cron` (NC 34 rename) |
| `occ app:install dashboardlauncher` → "invalid signature" | Community app fails the store signature check — install manually: download the GitHub release tar into `custom_apps/`, then `occ app:enable` (integrity check flags it as unsigned, which is expected for non-NC-signed apps) |
| Portal button dead-ends (404) | Buttons must use `apps/<id>` routes; the Text app has no standalone page in NC 34 — don't add it |
| Web-login smoke test with curl fails (`csrfCheckFailed`) | NC 34 requires a `Origin: https://dashboard.ronzz.org` header **and** URL-encoded POST fields (`--data-urlencode`) — raw `-d` corrupts the BREACH-encrypted CSRF token (`+` → space) |
| Wrong client IP in logs | `TRUSTED_PROXIES` must include the edge (127.0.0.1) |
| Talk video calls fail | Add coturn (`coturn/coturn` image, arm64) + configure Talk TURN settings |
| Disk filling | Retention is `auto, 30`; check `df -h /`, `occ files:scan`, then grow/attach volume |
| Fingerprint validation in OCI SDK | Real OCI fingerprints are **MD5 of the DER public key** (16 bytes); SDK regex accepts 16 groups |
| SnappyMail first run → `mkdir() failed` (data dir) | `APP_DATA_FOLDER_PATH` in `include.php` **must end with `/`** — `/var/lib/snappymail/` (without it SnappyMail concatenates the install-marker dir name) |
| `/?admin` → 403 from home, works elsewhere | Obsolete since 2026-08-16 — the nginx IP allowlist was removed when the panel moved behind OIDC (§7.7/§7.8); see the next row |
| Admin panel won't open after hardening | `allow_admin_panel = Off` in `application.ini` — flip to `On` (config is read per request, no reload) |
| `/?admin` on webmail.ronzz.org opens the normal webmail, not the panel | **Expected since 2026-08-16** — the panel moved to `webmail-admin.ronzz.org` and is gated behind NC login via OIDC (§7.8) |
| webmail-admin.ronzz.org → "Disallowed Sec-Fetch" 403 after login | `secfetch_allow = "site=same-site"` missing in `application.ini` (§7.8) |
| OIDC login bounces back to NC or loops | Check `WMA_ALLOWED_UIDS` in `/opt/webmail-oidc/webmail-oidc.env` includes the NC uid; `admin_panel.host` matches; `webmail-oidc` service active (`journalctl -u webmail-oidc`) |
| Panel shows the old password login form | The admin-OIDC patch isn't applied — re-apply per §7.5 (upgrade overwrites it) |
| Webmail can't reach Migadu | Check `domains/default.json` — `imap.migadu.com:993` / `smtp.migadu.com:465`, `type: 1` (implicit SSL), `verify_peer: true` |
| Repeated mailbox-password prompts | SnappyMail decrypts stored passwords with Sodium — the FPM pool user `snappymail` must be able to read `/var/lib/snappymail/SALT.php` |
| Webmail login fails for a bare username | `default_domain` in `application.ini` must be `ronzz.org` (or use the full `user@ronzz.org`) |
| Login → "…has no domain configuration" / "Ce domaine n'est pas autorisé" | A `domains/*.json` is unreadable by the FPM user. The pool runs as `snappymail`; files under `/var/lib/snappymail` must be `snappymail:snappymail` (0600). A `www-data`-owned `default.json` makes `file_exists()` pass but `file_get_contents()` fail → empty config → domain rejected (hit 2026-08-16). Fix: `chown snappymail:snappymail …/domains/*.json` (and keep an explicit `ronzz.org.json` — SnappyMail only resolves a domain via its exact file, an alias, or the `default.json` wildcard) |
| Login with an external email → "not whitelisted" / "Account is not allowed" | **By design** — webmail accepts @ronzz.org accounts only (`whiteList: "@ronzz.org"` in `default.json` + `ronzz.org.json`, §7.4). No IP gate needed; works off-premise |
| Need to allow another domain | Edit `whiteList` in `domains/default.json` (e.g. `"@ronzz.org @other.org"` — space-separated) and add an explicit `domains/<domain>.json` if it needs custom IMAP/SMTP |
| `occ migadu:test` → "Missing configuration" | Run the four `config:system:set` commands in §7.7 |
| `occ migadu:test` → HTTP 401 | Migadu API email/key wrong or expired — regenerate the key in Migadu Admin → My Account → API Keys; the account email is the **Migadu login** (not necessarily the mailbox address; see server-side `docs/IT/migadu-api.md`) |
| `occ migadu:test` → mailbox lookup HTTP 404 | The NC user's email has no Migadu mailbox — **self-healing since v1.2.0**: the mailbox is auto-created on the next password sync (user creation with a password, or a password change). Optionally create it manually, or exclude the user (`nc_migadu_password_sync_exclude`) if the email is a dummy |
| NC user deleted but Migadu mailbox still exists | Check `nc_migadu_password_sync_delete_mailboxes` isn't `false`; check the NC log for `migadu_sync: mailbox deletion FAILED …`; `occ migadu:test` lists "mailboxes without a matching Nextcloud user" to verify. Recover by re-deleting the NC user or deleting the mailbox in Migadu Admin |
| NC password change OK but webmail login fails | Sync failure — check the NC log for `migadu_sync: password sync FAILED …`; recover by re-running `occ user:resetpassword <uid>` (fires the event again). See §7.7 |
| Email-recovery link flow seems to "forget" the password | Not a bug — NC's `setPassword()` funnel is the same for the reset link; the sync runs exactly like any other change (§7.7) |

## 14. Security notes

- `.env` holds the DB + initial admin passwords — **never commit or share it** (perms 600).
- The Cloudflare API token lives in the root crontab for acme.sh DNS renewals — rotate it if it leaks.
- External link shares: password + expiry, never "can reshare".
- Keep `admin` disabled; use per-user accounts + app passwords for automation.
- All traffic TLS-terminated at nginx; Nextcloud itself listens only on loopback.
- **Webmail:** admin password in `/root/snappymail-admin-password.txt` (root-only) and Migadu mailbox passwords in `/var/lib/snappymail` (Sodium-encrypted, 0700) — never commit either. The admin panel is gated behind **Nextcloud login via OIDC** (§7.8): the OIDC client secret lives in `/opt/webmail-oidc/webmail-oidc.env` (root 600, server-side), sessions in `/var/lib/webmail-oidc/sessions/` (0700). Bridge sessions are not revoked by NC logout (8 h TTL) — revoke by clearing `/var/lib/webmail-oidc/sessions/*`.
- **Migadu API key:** `nc_migadu_password_sync_api_key` in `config.php` (inside the `nc_www` volume) grants **admin-level access to every ronzz.org mailbox** — treat it like the Cloudflare token: rotate via Migadu Admin → My Account → API Keys if it leaks. The key and API email are only covered by OCI snapshots (`nc_www` is not in the nightly tar, §11).
- **Mailbox deletion on NC user deletion is destructive** (v1.2.0+): it happens automatically and removes the mailbox irreversibly. If a migration or a "keep mail after account removal" policy is ever needed, flip `nc_migadu_password_sync_delete_mailboxes` to `false` *before* deleting users (see §7.7).

## 15. Local desktop client (admin's machine)

- Install: apt `nextcloud-desktop` (33.0.2), started via **systemd user service**
  (`com.nextcloud.desktopclient.nextcloud.service`) — the legacy `~/.config/autostart/Nextcloud.desktop`
  was disabled to prevent double-start.
- Accounts: personal `leo.it.tab.digital` → `~/Nextcloud` · `dashboard.ronzz.org` (Ron) → `~/ron-ronzz-nextcloud` ·
  `dashboard.ronzz.org` (Ronzz Shared) → `~/shared-ronzz-nextcloud` (this repo lives there, at `docs/IT/ronzz-nextcloud/`, and syncs up).
- Config: `~/.config/Nextcloud/nextcloud.cfg` (backups `*.bak-zombiefix` from the dedup fix).

---
*Maintained by IT · deploy date 2026-08-14 · update this doc with every structural change.*
