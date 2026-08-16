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
- Icons (2026-08-16): official Nextcloud app glyphs — dark variants where shipped (`app-dark.svg`, `deck-dark.svg`, `activity-dark.svg`, `spreed/app-dark.svg`), else the app's `app.svg` inverted `#fff`→`#000` (calendar, contacts, photos). Stored as `icon_<sha1:16>.<ext>` in appdata `data/appdata_<instanceid>/dashboardlauncher/icons/` (the `icone` value is the filename). `.button-icon` CSS has no invert filter, so only dark glyphs are visible on the white portal background.
- Site text stored in appconfig: `occ config:app:set dashboardlauncher site_title|welcome_text|footer_text --value="…"` — `{displayName}` is interpolated server-side.
- Admin UI: **Settings → Administration → Dashboard Launcher** — title/welcome/footer, add/reorder/group-restrict buttons, upload icons.
- Buttons as of 2026-08-16: Fichiers, Calendrier, Contacts, Deck, Photos, Talk, Activité. (Text omitted — no standalone page route in NC 34, it's an embedded editor.)
- The widgets Dashboard app stays enabled at `/apps/dashboard/` — it's just no longer the landing page.

## 7. Branding

- Name: **Ronzz.ORG** · Slogan: **Where miracles happen.**
- Primary color `#9bf141`, background `#fdfbfb`; logo = `Ronzz-org-emblemo.png` (1308×400).
- Palette also includes `#282c35` (dark) — currently unused; candidate for custom CSS header.
- Commands: `occ theming:config <name|slogan|primary_color|background_color|logo> <value>`
  (logo accepts a path readable by `www-data` inside the container).

## 8. Users & access

| User | Role | Notes |
|---|---|---|
| `ron@ronzz.org` | Admin (group `admin`) | Primary admin account |
| `ronzzshared` | Member | Dedicated **shared/team** account ("Ronzz Shared") |
| `admin` | **Disabled** | Default install admin — disabled via `occ user:disable admin` (2026-08-14) |

- **App passwords:** `occ user:add-app-password <user>` — use for scripts/clients instead of the master password.
- **2FA:** enable per user under Personal → Security (TOTP, built-in).
- **Token hygiene:** after the client account-removal bug, stale desktop tokens were revoked
  (`occ user:auth-tokens:delete <user> <id>`). Verify live tokens via `occ user:auth-tokens:list <user>`.

## 9. Backups

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

## 10. Common operations (occ cheat sheet)

```bash
docker exec -u www-data nextcloud php occ status
docker exec -u www-data nextcloud php occ user:list
docker exec -u www-data nextcloud php occ app:list
docker exec -u www-data nextcloud php occ maintenance:mode --on|--off
docker exec -u www-data nextcloud php occ files:scan --all
docker exec -u www-data nextcloud php occ config:system:get <key>
docker exec -u www-data nextcloud php occ background:cron
```

## 11. Troubleshooting

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

## 12. Security notes

- `.env` holds the DB + initial admin passwords — **never commit or share it** (perms 600).
- The Cloudflare API token lives in the root crontab for acme.sh DNS renewals — rotate it if it leaks.
- External link shares: password + expiry, never "can reshare".
- Keep `admin` disabled; use per-user accounts + app passwords for automation.
- All traffic TLS-terminated at nginx; Nextcloud itself listens only on loopback.

## 13. Local desktop client (admin's machine)

- Install: apt `nextcloud-desktop` (33.0.2), started via **systemd user service**
  (`com.nextcloud.desktopclient.nextcloud.service`) — the legacy `~/.config/autostart/Nextcloud.desktop`
  was disabled to prevent double-start.
- Accounts: personal `leo.it.tab.digital` → `~/Nextcloud` · `dashboard.ronzz.org` (Ron) → `~/ron-ronzz-nextcloud` ·
  `dashboard.ronzz.org` (Ronzz Shared) → `~/shared-ronzz-nextcloud` (this repo lives there, at `docs/IT/ronzz-nextcloud/`, and syncs up).
- Config: `~/.config/Nextcloud/nextcloud.cfg` (backups `*.bak-zombiefix` from the dedup fix).

---
*Maintained by IT · deploy date 2026-08-14 · update this doc with every structural change.*
