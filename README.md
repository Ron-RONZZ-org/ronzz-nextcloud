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
| **Dashboard Launcher** | ✅ (1.5.0) | ENT-style portal — see §6.1. Store install fails ("invalid signature"); installed manually into `custom_apps/` (AGPL, `github.com/dpfpic/dashboardlauncher`). |

### 6.1 Portal (Dashboard Launcher) — post-login landing page

- `defaultapp` → `dashboardlauncher`: every login lands on `/apps/dashboardlauncher/`, an ENT-style **button grid** of available apps (note: no search bar — tiles only; a search filter could be added as a small patch later).
- Buttons live in DB table `oc_dashboardlauncher_buttons` (`titre`, `icone`, `route`, `ordre`, `groupes` JSON array of groups, `actif`, `taille`). Routes use the `apps/<id>` form (the page sits 2 path levels deep, so the template's `../../<route>` resolves to `/apps/<id>/`).
- **External-URL buttons (patch, 2026-08-16):** `templates/main.php` now detects absolute URLs (`^https?://`) — those render as-is with `target="_blank" rel="noopener noreferrer"` (the stock template hardcodes `../../`, which would break `https://…` routes). Used by the Webmail tile → `https://webmail.ronzz.org` (see §7). Backward compatible with `apps/<id>` routes.
- Icons (2026-08-16): official Nextcloud app glyphs — dark variants where shipped (`app-dark.svg`, `deck-dark.svg`, `activity-dark.svg`, `spreed/app-dark.svg`), else the app's `app.svg` inverted `#fff`→`#000` (calendar, contacts, photos). Stored as `icon_<sha1:16>.<ext>` in appdata `data/appdata_<instanceid>/dashboardlauncher/icons/` (the `icone` value is the filename). `.button-icon` CSS has no invert filter, so only dark glyphs are visible on the white portal background. Webmail tile uses the NC Mail app's envelope SVG inverted to black.
- Site text stored in appconfig: `occ config:app:set dashboardlauncher site_title|welcome_text|footer_text --value="…"` — `{displayName}` is interpolated server-side.
- Admin UI: **Settings → Administration → Dashboard Launcher** — title/welcome/footer, add/reorder/group-restrict buttons, upload icons.
- Buttons as of 2026-08-16: Fichiers, Calendrier, Contacts, Deck, Photos, Talk, Activité, Webmail. (Text omitted — no standalone page route in NC 34, it's an embedded editor.)
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
- **TLS:** Let's Encrypt ECC cert via acme.sh **DNS-01** (`--dns dns_cf`, Cloudflare token), installed to `/etc/letsencrypt/webmail.ronzz.org/{fullchain,privkey}.pem`; auto-renewed by the existing root cron (`acme.sh --cron`). Domain conf: `/etc/letsencrypt/webmail.ronzz.org_ecc/webmail.ronzz.org.conf`.
- **nginx:** `/etc/nginx/sites-available/webmail.ronzz.org.conf` (symlinked) — HTTP→HTTPS, security headers, `deny /data/`, dotfile block, 50M upload cap, fastcgi to the snappymail socket.

### 7.3 Deployment & config

- **Install:** extract the release zip into `/var/www/snappymail` (owner `www-data`); `data/` moved out of the webroot to `/var/lib/snappymail` via `include.php` (`APP_DATA_FOLDER_PATH` — **must end with `/`**, see §12).
- **PHP-FPM pool:** `/etc/php/8.3/fpm/pool.d/snappymail.conf` — user `snappymail`, socket `/run/php/snappymail-fpm.sock`, `pm.max_children = 4`.
- **Config files** (the admin UI writes the same files — direct edits are fine):
  - `application.ini` (`/var/lib/snappymail/_data_/_default_/configs/`) — admin credentials, `default_domain = "ronzz.org"` (bare logins map to ronzz.org), `force_https = On`, title/loading branding.
  - `domains/default.json` — Migadu defaults: IMAP 993 / SMTP 465 / Sieve 4190, `type: 1` (implicit SSL), **cert verification ON** (`verify_peer`/`verify_peer_name`).
- **Login model (decided, #3): SnappyMail-managed users** — admin → Login → Users creates the webmail account; each user's IMAP account maps to their Migadu mailbox (mailbox password entered once under Settings → Accounts, stored encrypted with Sodium).

### 7.4 Security

- **Admin panel (2026-08-16): gated behind Nextcloud login via OIDC.** The panel moved to its own hostname `https://webmail-admin.ronzz.org` (`admin_panel.host` in `application.ini` — the old `/?admin` on `webmail.ronzz.org` no longer opens the panel). Access requires a valid **Nextcloud session** (NC acts as OIDC IdP via the `H2CK/oidc` app; login includes NC TOTP if enabled): nginx `auth_request` → OIDC-RP sidecar (`/opt/webmail-oidc`, systemd `webmail-oidc`) → session cookie. The previous nginx IP allowlist + static admin password are superseded. Full runbook: **§7.7**; artifacts in `webmail/webmail-admin-oidc/` and `webmail/patches/`.
- The old static credentials still exist as a fallback: `admin_login`/`admin_password` in `application.ini` (bcrypt; the panel → Security section can also set `admin_totp` for an extra factor).
- Hardening options after provisioning: `allow_admin_panel = Off` in `application.ini` disables the panel entirely (re-enable: flip to `On`; no service reload needed — config is read per request).
- HTTPS enforced at nginx (301) and app level (`force_https`). Data dir is 0700 outside webroot. User SMTP server settings keep the Migadu default (prevents data exfiltration to arbitrary hosts).
- `APP_REMOTE_HOST_WHITE_LIST` (mentioned in #4) is **not** a SnappyMail option — the equivalent built-ins are the Fetch-Metadata request checks (on by default; `secfetch_allow = "site=same-site"` was added for the OIDC redirect navigation).

### 7.5 Upgrade (incl. admin-OIDC patch replay)

```bash
cd /tmp && wget https://github.com/the-djmaze/snappymail/releases/download/v<ver>/snappymail-<ver>.zip
# GPG-verify (key 1016E47079145542F8BA133548208BA13290F3EB) before extracting
sudo unzip -o -q snappymail-<ver>.zip -d /var/www/snappymail   # only index.php + data/VERSION are overwritten
sudo chown -R www-data:www-data /var/www/snappymail
# re-apply the tracked admin-OIDC patch (§7.7; upstream PR the-djmaze/snappymail#2066 — safe failure if it drifts)
cd /var/www/snappymail/snappymail/v/<ver>/app/libraries/RainLoop
sudo patch -p1 < /path/to/ronzz-nextcloud/webmail/patches/snappymail-admin-oidc.patch
sudo php -l Actions/Admin.php && sudo php -l ActionsAdmin.php
```

If `patch` fails (drift), the panel stays password-only until the patch is rebased — the OIDC gate simply doesn't open the panel (safe failure).

### 7.6 Backup

- Nightly root cron `35 3 * * *`: `tar czf /var/backups/snappymail/data-<ts>.tgz -C /var/lib snappymail`, 7-day retention (covers settings, encrypted passwords, contacts; mailbox data itself lives on Migadu).
- Plus OCI `daily-5d` boot-volume snapshots (crash-consistent).

### 7.7 Admin panel OIDC bridge (runbook)

> The SnappyMail admin panel (`webmail-admin.ronzz.org`) is gated behind
> **Nextcloud login via OIDC** (NC = IdP through the `H2CK/oidc` app). This
> replaces the old `/?admin` + nginx IP allowlist + static password model (§7.4).

**Components** (live since 2026-08-16; source in `webmail/webmail-admin-oidc/`):
NC `oidc` app v2.0.7 (IdP) → OIDC client `webmail-admin` (`occ oidc:create`, confidential, code flow) → sidecar `/opt/webmail-oidc` (FastAPI, systemd `webmail-oidc`, cookie `wmauth`, sessions in `/var/lib/webmail-oidc/sessions/`) → nginx `auth_request` on the `webmail-admin.ronzz.org` vhost → SnappyMail patch (`webmail/patches/snappymail-admin-oidc.patch`, re-applied on upgrade per §7.7).

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

**Troubleshooting:** 403 "Disallowed Sec-Fetch" after login → `secfetch_allow` not set; 500 on callback → sidecar venv missing `cryptography` (`pip install cryptography`); redirect loop → `admin_panel.host` mismatch or `WMA_ALLOWED_UIDS` excludes the user; panel login form still shown → patch not applied (§7.7).

## 8. Branding

- Name: **Ronzz.ORG** · Slogan: **Where miracles happen.**
- Primary color `#9bf141`, background `#fdfbfb`; logo = `Ronzz-org-emblemo.png` (1308×400).
- Palette also includes `#282c35` (dark) — currently unused; candidate for custom CSS header.
- Commands: `occ theming:config <name|slogan|primary_color|background_color|logo> <value>`
  (logo accepts a path readable by `www-data` inside the container).

## 9. Users & access

| User | Role | Notes |
|---|---|---|
| `ron@ronzz.org` | Admin (group `admin`) | Primary admin account |
| `ronzzshared` | Member | Dedicated **shared/team** account ("Ronzz Shared") |
| `admin` | **Disabled** | Default install admin — disabled via `occ user:disable admin` (2026-08-14) |

- **App passwords:** `occ user:add-app-password <user>` — use for scripts/clients instead of the master password.
- **2FA:** enable per user under Personal → Security (TOTP, built-in).
- **Token hygiene:** after the client account-removal bug, stale desktop tokens were revoked
  (`occ user:auth-tokens:delete <user> <id>`). Verify live tokens via `occ user:auth-tokens:list <user>`.

## 10. Backups

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

## 11. Common operations (occ cheat sheet)

```bash
docker exec -u www-data nextcloud php occ status
docker exec -u www-data nextcloud php occ user:list
docker exec -u www-data nextcloud php occ app:list
docker exec -u www-data nextcloud php occ maintenance:mode --on|--off
docker exec -u www-data nextcloud php occ files:scan --all
docker exec -u www-data nextcloud php occ config:system:get <key>
docker exec -u www-data nextcloud php occ background:cron
```

## 12. Troubleshooting

| Symptom | Check |
|---|---|
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
| `/?admin` → 403 from home, works elsewhere | Wrong — the nginx IP allowlist in `webmail.ronzz.org.conf` covers `185.5.129.0/24` (Lebara mobile CGNAT); if the admin's egress range changed, update the regex (or remove the gate — the panel stays password-protected) |
| Admin panel won't open after hardening | `allow_admin_panel = Off` in `application.ini` — flip to `On` (config is read per request, no reload) |
| `/?admin` on webmail.ronzz.org opens the normal webmail, not the panel | **Expected since 2026-08-16** — the panel moved to `webmail-admin.ronzz.org` and is gated behind NC login via OIDC (§7.7) |
| webmail-admin.ronzz.org → "Disallowed Sec-Fetch" 403 after login | `secfetch_allow = "site=same-site"` missing in `application.ini` (§7.7) |
| OIDC login bounces back to NC or loops | Check `WMA_ALLOWED_UIDS` in `/opt/webmail-oidc/webmail-oidc.env` includes the NC uid; `admin_panel.host` matches; `webmail-oidc` service active (`journalctl -u webmail-oidc`) |
| Panel shows the old password login form | The admin-OIDC patch isn't applied — re-apply per §7.5 (upgrade overwrites it) |
| Webmail can't reach Migadu | Check `domains/default.json` — `imap.migadu.com:993` / `smtp.migadu.com:465`, `type: 1` (implicit SSL), `verify_peer: true` |
| Repeated mailbox-password prompts | SnappyMail decrypts stored passwords with Sodium — the FPM pool user `snappymail` must be able to read `/var/lib/snappymail/SALT.php` |
| Webmail login fails for a bare username | `default_domain` in `application.ini` must be `ronzz.org` (or use the full `user@ronzz.org`) |

## 13. Security notes

- `.env` holds the DB + initial admin passwords — **never commit or share it** (perms 600).
- The Cloudflare API token lives in the root crontab for acme.sh DNS renewals — rotate it if it leaks.
- External link shares: password + expiry, never "can reshare".
- Keep `admin` disabled; use per-user accounts + app passwords for automation.
- All traffic TLS-terminated at nginx; Nextcloud itself listens only on loopback.
- **Webmail:** admin password in `/root/snappymail-admin-password.txt` (root-only) and Migadu mailbox passwords in `/var/lib/snappymail` (Sodium-encrypted, 0700) — never commit either. The admin panel is gated behind **Nextcloud login via OIDC** (§7.7): the OIDC client secret lives in `/opt/webmail-oidc/webmail-oidc.env` (root 600, server-side), sessions in `/var/lib/webmail-oidc/sessions/` (0700). Bridge sessions are not revoked by NC logout (8 h TTL) — revoke by clearing `/var/lib/webmail-oidc/sessions/*`.

## 14. Local desktop client (admin's machine)

- Install: apt `nextcloud-desktop` (33.0.2), started via **systemd user service**
  (`com.nextcloud.desktopclient.nextcloud.service`) — the legacy `~/.config/autostart/Nextcloud.desktop`
  was disabled to prevent double-start.
- Accounts: personal `leo.it.tab.digital` → `~/Nextcloud` · `dashboard.ronzz.org` (Ron) → `~/ron-ronzz-nextcloud` ·
  `dashboard.ronzz.org` (Ronzz Shared) → `~/shared-ronzz-nextcloud` (this repo lives there, at `docs/IT/ronzz-nextcloud/`, and syncs up).
- Config: `~/.config/Nextcloud/nextcloud.cfg` (backups `*.bak-zombiefix` from the dedup fix).

---
*Maintained by IT · deploy date 2026-08-14 · update this doc with every structural change.*
