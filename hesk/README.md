# Hesk — lightweight FOSS ticket tracker at threads.ronzz.org

Hesk (PHP + MySQL) chosen over Zammad/Vikunja/FreeScout for the ronzz.ORG
"odd issues" tracker (issue: watercooler, AI-inference provider transition, …).
Justifying rationale, feature mapping, and the alternatives comparison live in
the session notes; this directory holds the deployable artifacts.

**Decision summary (2026-08-17):**
- **What we need:** assigning, status tracking/update, tagging-ish (categories),
  ticket list feel, minimal footprint. → Hesk is the lightest thing with real
  ticket-list semantics (one PHP app + the **existing** local MySQL; no
  Rails/Node/ES/Docker, no new DB server).
- **Auth (Option B):** full SSO — staff panel gated behind Nextcloud login via
  OIDC (H2CK/oidc IdP + FastAPI sidecar, same pattern as webmail-admin) with
  header-based auto-login patched into Hesk.
- **Email intake:** deferred. Hesk IMAP fetching is a Settings toggle + cron
  line + a Migadu mailbox — fully additive later, no structural change.

> **Status: LIVE on ronzz-linux-server-2 (2026-08-17).** Verified E2E: valid
> sidecar session → `/admin/index.php` → 302 `admin_main.php` → 200 "Help Desk".
> Server-side source of truth: `RonzzIT:LinuxServer2` (gated wiki) ("Hesk" section).

## Contents

```
hesk/
├── README.md                      ← this runbook
├── patches/
│   ├── hesk-oidc-sso.patch        ← admin/index.php OIDC auto-login (CRLF-aware) +
│   │                                 inc/common.inc.php: skip the Hesk "elevator"
│   │                                 (re-auth) for SSO sessions
│   ├── hesk-reply-embed.patch     ← admin_reply_ticket.php: embed reply text in
│   │                                 the notification email (natural replies)
│   └── hesk-subjects.patch        ← language/en/text.php: natural email subjects
│                                     (Re: subject [#TRACK]) — reply + ticket received
├── email-templates/
│   ├── new_reply_by_staff.txt     ← plain: just %%MESSAGE%% (no URL/footer)
│   └── new_reply_by_staff.html    ← HTML: just the message
└── threads-oidc/                  ← OIDC sidecar fork (mirrors webmail-admin-oidc/)
    ├── README.md                  ← auth bridge runbook
    ├── app.py                     ← FastAPI OIDC-RP sidecar (env: HESK_*)
    ├── threads-oidc.service       ← systemd unit (port 8016)
    └── threads.ronzz.org.conf     ← nginx vhost (gate /admin/ only)
```

## Natural-reply emails (2026-08-17)

The customer-facing reply email is just the reply text — no tracking-URL or
site-title footer, so it reads like a normal email. Two pieces (re-apply on
every Hesk upgrade, like the SSO patch):

1. **`hesk-reply-embed.patch`** — `admin_reply_ticket.php`: before
   `hesk_notifyCustomer('new_reply_by_staff')`, sets `$ticket['message']` /
   `$ticket['message_html']` to the reply text so `%%MESSAGE%%` renders it.
   Apply from the Hesk root: `sudo -u www-data patch -p1 < …/hesk-reply-embed.patch`
   (CRLF-aware, verified byte-for-byte).
2. **`email-templates/`** — copy `new_reply_by_staff.txt` + `.html` into
   `language/en/emails/` and `language/en/html_emails/`.

**What's intentionally kept (essential to Hesk):** `[#TRACK_ID]` in the email
subject (reply-loop matching) and the code-injected "Reply above this line"
marker (`EMAIL_HR`, quote-stripping on intake). Everything else is noise-free.

**Email subjects** (`hesk-subjects.patch`, applies to `language/en/text.php`):
- Reply: `Re: %%SUBJECT%% [#%%TRACK_ID%%]`
- Ticket received: `Re: %%SUBJECT%% - We have received your email [#%%TRACK_ID%%]`

The tracking code is de-emphasized (end of subject) but still matched by
Hesk's intake regex (`\[#XXX-XXX-XXXX\]` anywhere in the space-stripped
subject). Re-apply with the other patches on Hesk upgrades.


## Deployment checklist (mirrors README §7 conventions)

### 1. DB — existing local MySQL (no new DB server; NC's Postgres is untouched)

```bash
# Use the maintenance account (root has no socket auth):
sudo mysql --defaults-file=/etc/mysql/debian.cnf -e \
  "CREATE DATABASE IF NOT EXISTS hesk CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; \
   CREATE USER IF NOT EXISTS 'hesk'@'localhost' IDENTIFIED BY '<secret>'; \
   GRANT ALL PRIVILEGES ON hesk.* TO 'hesk'@'localhost'; FLUSH PRIVILEGES;"
# secret → /root/hesk-db-password.txt (root 600)
```

### 2. App — PHP-FPM pool + Hesk files

```bash
# pool: /etc/php/8.3/fpm/pool.d/hesk.conf — user hesk, socket /run/php/hesk-fpm.sock, max_children 4
sudo useradd -r -s /usr/sbin/nologin hesk
# Source: hesk.com's download is bot-gated; use the GitHub mirror (same version):
git clone --depth 1 https://github.com/brunohonda/hesk.git   # hesk/ subdir = Hesk 3.6.4
sudo rsync -a hesk/ /var/www/hesk/ && sudo chown -R hesk:hesk /var/www/hesk
# run install.php (GET install/install.php → agree → DB form) — the FPM pool must
# own hesk_settings.inc.php + attachments/ + cache/ (writable checks)
# then: sudo rm -rf /var/www/hesk/install   (vhost denies /install/ afterwards)
```

### 3. DNS & TLS (same recipe as §7.2)

- Cloudflare API: A `threads.ronzz.org → 158.178.193.231`, **grey cloud**
- `acme.sh --issue --dns dns_cf -d threads.ronzz.org --ecc --keylength ec-256 --home /etc/letsencrypt`
  (auto-renewed by the existing root cron)

### 4. nginx vhost

- `hesk/threads-oidc/threads.ronzz.org.conf` → `/etc/nginx/sites-available/`
- Gate `/admin/` via `auth_request` → sidecar `127.0.0.1:8016`; public pages open;
  `fastcgi_param HTTP_X_HESK_USER $hesk_user` in the `/admin/` PHP location

### 5. OIDC bridge (full SSO)

```bash
# NC side (one-time)
docker exec -u www-data nextcloud php occ oidc:create hesk \
    --redirect-uris=https://threads.ronzz.org/_oidc/callback   # capture client id+secret

# Server side (mirror /opt/webmail-oidc)
sudo useradd -r -s /usr/sbin/nologin hesk-oidc
sudo mkdir -p /opt/hesk-oidc && sudo cp hesk/threads-oidc/app.py /opt/hesk-oidc/
sudo chown -R hesk-oidc:hesk-oidc /opt/hesk-oidc   # venv must be created by the user
sudo -u hesk-oidc python3 -m venv /opt/hesk-oidc/.venv && sudo -u hesk-oidc /opt/hesk-oidc/.venv/bin/pip install fastapi 'uvicorn[standard]' PyJWT requests cryptography
sudo mkdir -p /var/lib/hesk-oidc/sessions && sudo chown -R hesk-oidc:hesk-oidc /var/lib/hesk-oidc
# /opt/hesk-oidc/hesk-oidc.env (hesk-oidc:hesk-oidc 640 — service user must read it):
#   HESK_CLIENT_ID / HESK_CLIENT_SECRET / HESK_REDIRECT_URI / HESK_ALLOWED_UIDS
sudo cp hesk/threads-oidc/threads-oidc.service /etc/systemd/system/hesk-oidc.service
sudo systemctl daemon-reload && sudo systemctl enable --now hesk-oidc
curl http://127.0.0.1:8016/_oidc/health   # {"ok": true}
```

### 6. Hesk SSO patch

```bash
# Run from the Hesk ROOT (/var/www/hesk), not admin/ — the patch paths are a/admin/index.php + a/inc/common.inc.php
cd /var/www/hesk
sudo cp admin/index.php admin/index.php.upstream-bak && sudo chown www-data:www-data admin/index.php.upstream-bak
sudo cp inc/common.inc.php inc/common.inc.php.upstream-bak && sudo chown www-data:www-data inc/common.inc.php.upstream-bak
sudo -u www-data patch -p1 < /var/www/hesk/patches/hesk-oidc-sso.patch   # needs write perms: run as root if the files are hesk-owned
sudo -u hesk php -l admin/index.php && sudo -u hesk php -l inc/common.inc.php   # "No syntax errors detected"
```

> The patch is CRLF-aware and verified byte-for-byte against the upstream
> `admin/index.php` + `inc/common.inc.php` (both `login` and `default` actions
> hook `hesk_oidc_auto_login()`; the elevator gate in
> `hesk_check_user_elevation()` honors the `sso` session flag). If `patch`
> reports "different line endings", the Hesk release changed the file —
> regenerate with `diff -u` against the new source following the pattern in
> `hesk/patches/hesk-oidc-sso.patch`.

**Elevator behavior (what the `inc/common.inc.php` hunk does):** Hesk asks
sensitive pages (Team, MFA management, customer management/import) to
re-enter the Hesk password ("elevator", `admin/elevator.php`) whenever the
`elevated` session marker expired (`elevator_duration`, default 60 min). That
is a dead end for SSO users whose Hesk password is unknown by design. The
patch marks SSO sessions (`$_SESSION['sso'] = true` in `hesk_oidc_auto_login()`)
and makes `hesk_check_user_elevation()` return early for them — nginx
re-validates the NC session (incl. TOTP) on **every** `/admin/` request, so the
periodic re-auth is redundant for SSO users. Password-form fallback logins
keep the stock elevator behavior. Safe failure: no `sso` flag → stock code.

### 7. Staff accounts

Create Hesk staff users whose `user` **exactly equals** the NC uid
(`ron@ronzz.org`, `ronzzshared`) — the SSO lookup matches on that value.
Add the same uids to `HESK_ALLOWED_UIDS`.

### 8. Email intake (deferred — do later when wanted)

```bash
# 1. Migadu: create tickets@ronzz.org (API key pattern from nc_migadu_password_sync)
# 2. Hesk admin → Settings → Email: IMAP fetching ON, imap.migadu.com:993 SSL,
#    user tickets@ronzz.org, "keep a copy" ON
# 3. Requires php-imap: sudo apt install php8.3-imap && sudo systemctl restart php8.3-fpm
# 4. root crontab (same cadence as NC cron):
#    */5 * * * * curl -s "https://threads.ronzz.org/inc/mail/hesk_pop3.php?key=<URL_ACCESS_KEY>"
```

### 9. Backup (live: root cron 03:40, README §11)

```bash
# root crontab line (LIVE since 2026-08-17):
# 40 3 * * * bash -c "mysqldump --defaults-file=/etc/mysql/debian.cnf hesk > /var/backups/hesk/db-$(date +%F).sql 2>/dev/null && find /var/backups/hesk -name 'db-*.sql' -mtime +7 -delete" >> /var/log/hesk-backup.log 2>&1
# mysqldump via the maintenance account (root has no socket auth on this box)
# 7-day retention, same as NC/webmail; OCI daily-5d snapshots cover the rest
```

### 10. Docs

Update the main README.md when deployed (your own rule: every structural change
is documented): new §"Hesk" mirroring §7/§7.8, hostname/version/deploy date,
and the auth bridge reference to `hesk/threads-oidc/README.md`. **Done 2026-08-17**
(README §8 + source-of-truth `RonzzIT:LinuxServer2` (gated wiki) "Hesk" section).
