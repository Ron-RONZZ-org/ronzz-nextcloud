# hesk-oidc — Hesk staff panel behind Nextcloud login (full SSO, Option B)

Gates `https://threads.ronzz.org/admin/` (the Hesk staff panel) behind
**Nextcloud login via OIDC** (NC acts as IdP through the `H2CK/oidc` app) and
**auto-logs the matching Hesk staff account in** — no Hesk password needed
for staff (the panel's own login form remains as a safe fallback).

> **Upstream:** Hesk has no native OIDC/LDAP (its OAuth is Microsoft-365
> specific). This bridge + patch is a ronzz.org-specific integration, in the
> same spirit as `webmail/patches/snappymail-admin-oidc.patch`. Re-apply the
> patch on every Hesk upgrade; if it drifts, the panel falls back to the
> normal password login (safe failure).

## Components (planned for ronzz-linux-server-2; hostname threads.ronzz.org)

| Piece | Location | Role |
|---|---|---|
| NC OIDC IdP | `H2CK/oidc` app (enabled; `occ app:install oidc`) | Issues ID tokens; login incl. NC TOTP |
| OIDC client | `occ oidc:create hesk` (confidential, `code` flow, RS256) | RP credentials for the bridge |
| Sidecar | `/opt/hesk-oidc/` (FastAPI + PyJWT + requests, venv, systemd `hesk-oidc`) | OIDC RP: login/callback/auth/logout; session store `/var/lib/hesk-oidc/sessions/`; cookie `heskauth` |
| nginx vhost | `/etc/nginx/sites-available/threads.ronzz.org.conf` | `auth_request` gate on `/admin/` **only**; public ticket pages open; sets `X-Hesk-User` only after success |
| Hesk patch | `hesk/patches/hesk-oidc-sso.patch` | `admin/index.php` auto-login when `X-Hesk-User` matches a staff account |
| DB | MariaDB (localhost) — `hesk` database | Hesk's own DB (NC's Postgres is untouched) |

## Flow

```
browser → threads.ronzz.org/admin/...
  → nginx auth_request → sidecar /_oidc/auth (cookie heskauth?)
      no  → 401 → @oidc-login → /_oidc/login → NC authorize (dashboard.ronzz.org)
             → NC login (+TOTP) → /_oidc/callback → code exchange → JWKS-verified
               ID token (iss/aud/nonce) → allowlist check → session cookie → back
      yes → 200 + X-Hesk-User:<nc-uid> → nginx sets HTTP_X_HESK_USER
             → patched admin/index.php hesk_oidc_auto_login()
               → matches Hesk staff user by `user` = <nc-uid> → process_successful_login()
               → redirect via hesk_verifyGoto() → admin_main.php
```

Public pages (`/` submit form, `/ticket.php` status, `/knowledgebase.php`) are
**not** gated — Hesk's design deliberately has no customer accounts.

## Config (server-side secrets — never commit)

`/opt/hesk-oidc/hesk-oidc.env` (root 600): `HESK_CLIENT_ID`, `HESK_CLIENT_SECRET`
(from `occ oidc:create`), `HESK_REDIRECT_URI=https://threads.ronzz.org/_oidc/callback`,
`HESK_ALLOWED_UIDS=ron@ronzz.org,ronzzshared` (comma-separated NC uids allowed
into the panel). Create matching Hesk staff accounts whose `user` equals the NC
uid (e.g. `ron@ronzz.org`) — the SSO lookup is by that exact value.

## Security notes

- `X-Hesk-User` is set by nginx **only after a successful auth_request**; the
  `/admin/` PHP location overrides any client-supplied header
  (`fastcgi_param HTTP_X_HESK_USER $hesk_user;`).
- The patched function honors the header only when `Host` contains
  `threads.ronzz.org` (defense in depth — the header is meaningless anywhere
  else, and the public `location /` does not forward it).
- SSO bypasses Hesk's own password + Hesk-side MFA: the **NC login (incl. TOTP)
  is the single second factor**. If a staff user needs Hesk's own MFA, they can
  still use the password form (the `login` case checks SSO first, falls through
  on no-header).
- Sessions: 8 h TTL, server-side file store, `heskauth` cookie HttpOnly+Secure+Lax.
  NC logout/expiry does not kill an active bridge session (documented tradeoff,
  same as webmail-admin).
- The `hesk_oidc_auto_login()` call site is guarded by `!empty($_SESSION['id'])`
  so a logged-in user is never re-authed mid-session.

## Upgrade procedure (Hesk version bumps)

```bash
# Re-download Hesk zip → /var/www/hesk (overwrites files)
cd /var/www/hesk/admin
cp index.php index.php.upstream-bak
patch -p1 < /path/to/ronzz-nextcloud/hesk/patches/hesk-oidc-sso.patch
php -l index.php   # must be "No syntax errors detected"
# remove install/ dir again; if patch fails (drift), panel falls back to
# password login until the patch is rebased against the new index.php
```

## Operations

- Restart: `sudo systemctl restart hesk-oidc` · Logs: `sudo journalctl -u hesk-oidc -f`
- Health: `curl http://127.0.0.1:8016/_oidc/health`
- Grant/revoke panel access: edit `HESK_ALLOWED_UIDS` in the env file → restart
  (sessions stay valid until TTL)
- Add a second admin: add their NC uid to `HESK_ALLOWED_UIDS` **and** create the
  matching Hesk staff account (same `user` string) — they log in with their own
  NC credentials (and their own TOTP), no shared password
- Reset everything: stop/disable `hesk-oidc`, revert the patch (restore from
  upstream), remove the `auth_request` block from the vhost — panel returns to
  the static-password model

## Troubleshooting

- 403 "Disallowed Sec-Fetch"-style loop after login → nginx `auth_request` OK but
  redirect target wrong: check `HESK_REDIRECT_URI` matches the vhost and the
  `@oidc-login` `next` parameter
- 500 on callback → sidecar venv missing `cryptography` (`pip install cryptography`)
- Panel shows the password form despite NC login → patch not applied (`hesk_oidc_auto_login`
  missing from `admin/index.php`), or `HESK_ALLOWED_UIDS` excludes the NC uid, or the
  Hesk staff `user` differs from the NC uid
- Redirect loop → `Host` check in the patched function requires the Host header to
  contain `threads.ronzz.org` — make sure nginx passes `Host $host`
