# webmail-admin-oidc — SnappyMail admin panel behind Nextcloud login

Gates `https://webmail-admin.ronzz.org` (the SnappyMail admin panel) behind
**Nextcloud login via OIDC** (NC acts as IdP through the `H2CK/oidc` app). The
panel's static bcrypt password + nginx IP allowlist are superseded by this
bridge; `admin_totp` can still be set in the panel for an extra factor.

> **Upstream plan:** the SnappyMail-side change (`webmail/patches/snappymail-admin-oidc.patch`)
> is intended to be proposed upstream (`the-djmaze/snappymail`) as "trusted
> proxy-auth header for the admin panel". Until merged, this patch is re-applied
> on every SnappyMail upgrade (README §7.5 / §7.8).

## Components (live on ronzz-linux-server-2, 2026-08-16)

| Piece | Location | Role |
|---|---|---|
| NC OIDC IdP | `H2CK/oidc` v2.0.7 app (enabled; `occ app:install oidc`) | Issues ID tokens; login incl. NC TOTP |
| OIDC client | `occ oidc:create webmail-admin` (confidential, `code` flow, RS256) | RP credentials for the bridge |
| Sidecar | `/opt/webmail-oidc/` (FastAPI + PyJWT + requests, venv, systemd `webmail-oidc`) | OIDC RP: login/callback/auth/logout; session store `/var/lib/webmail-oidc/sessions/`; cookie `wmauth` |
| nginx vhost | `/etc/nginx/sites-available/webmail-admin.ronzz.org.conf` | `auth_request` gate → sidecar; sets `X-NC-Admin` only after success |
| SnappyMail patch | `webmail/patches/snappymail-admin-oidc.patch` | `IsAdminLoggined()` honors `X-NC-Admin` on the admin host |

## Flow

```
browser → webmail-admin.ronzz.org/?admin
  → nginx auth_request → sidecar /_oidc/auth (cookie wmauth?)
      no  → 401 → @oidc-login → /_oidc/login → NC authorize (dashboard.ronzz.org)
             → NC login (+TOTP) → /_oidc/callback → code exchange → JWKS-verified
               ID token (iss/aud/nonce) → allowlist check → session cookie → back
      yes → 200 + X-NC-Admin:<uid> → SnappyMail IsAdminLoggined() (patched) → panel
```

## Config (server-side secrets — never commit)

`/opt/webmail-oidc/webmail-oidc.env` (root 600): `WMA_CLIENT_ID`, `WMA_CLIENT_SECRET`,
`WMA_REDIRECT_URI`, `WMA_ALLOWED_UIDS` (comma-separated NC uids allowed to open
the panel — currently `ron@ronzz.org`). Panel host: `admin_panel.host =
"webmail-admin.ronzz.org"` in `application.ini`; `secfetch_allow = "site=same-site"`
(allows the same-site navigation after the OIDC redirect).

## Security notes

- `X-NC-Admin` is set by nginx **only after a successful auth_request**; the PHP
  location overrides any client-supplied header (`fastcgi_param HTTP_X_NC_ADMIN $nc_admin`).
- The patch honors the header only when `Host` matches `admin_panel.host`
  (defense in depth — the header is meaningless on `webmail.ronzz.org`).
- Sessions: 8 h TTL, server-side file store, `wmauth` cookie HttpOnly+Secure+Lax.
  NC logout/expiry does not kill an active bridge session (documented tradeoff).
- All admin JSON actions run through `IsAdminLoggined()` → same gate.
