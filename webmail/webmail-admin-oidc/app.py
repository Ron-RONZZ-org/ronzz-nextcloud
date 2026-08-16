#!/usr/bin/env python3
"""OIDC-RP sidecar — gates the SnappyMail admin panel behind Nextcloud login.

Flow: nginx auth_request → GET /_oidc/auth (cookie check) → 200 + X-NC-Admin or 401.
On 401 nginx redirects the browser to /_oidc/login → NC OIDC authorize (H2CK/oidc)
→ callback validates the ID token (JWKS RS256, iss/aud/nonce) → session cookie.

Config via environment (systemd unit), see /opt/webmail-oidc/webmail-oidc.service.
"""
import json
import os
import secrets
import time
import urllib.parse
from pathlib import Path

import jwt  # PyJWT
import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

CLIENT_ID = os.environ["WMA_CLIENT_ID"]
CLIENT_SECRET = os.environ["WMA_CLIENT_SECRET"]
REDIRECT_URI = os.environ.get(
    "WMA_REDIRECT_URI", "https://webmail-admin.ronzz.org/_oidc/callback"
)
ISSUER = os.environ.get("WMA_ISSUER", "https://dashboard.ronzz.org")
AUTHZ_URL = os.environ.get(
    "WMA_AUTHZ_URL", "https://dashboard.ronzz.org/apps/oidc/authorize"
)
TOKEN_URL = os.environ.get(
    "WMA_TOKEN_URL", "https://dashboard.ronzz.org/apps/oidc/token"
)
JWKS_URL = os.environ.get(
    "WMA_JWKS_URL", "https://dashboard.ronzz.org/apps/oidc/jwks"
)
ALLOWED_UIDS = {
    u.strip()
    for u in os.environ.get("WMA_ALLOWED_UIDS", "ron@ronzz.org").split(",")
    if u.strip()
}
SESSION_DIR = Path(os.environ.get("WMA_SESSION_DIR", "/var/lib/webmail-oidc/sessions"))
SESSION_TTL = int(os.environ.get("WMA_SESSION_TTL", "28800"))  # 8 h
COOKIE = "wmauth"
SCOPE = "openid profile email"

app = FastAPI()
SESSION_DIR.mkdir(parents=True, exist_ok=True)


def _new_session(uid: str) -> str:
    token = secrets.token_urlsafe(32)
    (SESSION_DIR / token).write_text(
        json.dumps({"uid": uid, "exp": int(time.time()) + SESSION_TTL})
    )
    return token


def _read_session(token: str) -> str | None:
    f = SESSION_DIR / token
    try:
        d = json.loads(f.read_text())
    except Exception:
        return None
    if d.get("exp", 0) < time.time():
        f.unlink(missing_ok=True)
        return None
    return d.get("uid")


def _verify_id_token(id_token: str) -> dict:
    jwks = jwt.PyJWKClient(JWKS_URL)
    key = jwks.get_signing_key_from_jwt(id_token).key
    return jwt.decode(
        id_token, key, algorithms=["RS256"], audience=CLIENT_ID, issuer=ISSUER
    )


@app.get("/_oidc/login")
def login(next: str = "/?admin"):
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    params = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPE,
            "state": state,
            "nonce": nonce,
        }
    )
    resp = RedirectResponse(url=f"{AUTHZ_URL}?{params}")
    resp.set_cookie(
        "wmauth_state", f"{state}:{nonce}", httponly=True, secure=True,
        samesite="lax", max_age=600, path="/",
    )
    resp.set_cookie(
        "wmauth_next", next, httponly=True, secure=True, samesite="lax",
        max_age=600, path="/",
    )
    return resp


@app.get("/_oidc/callback")
def callback(code: str, state: str, request: Request):
    st = request.cookies.get("wmauth_state", "")
    if not st or not state or not secrets.compare_digest(st.split(":", 1)[0], state):
        return JSONResponse({"error": "state mismatch"}, status_code=400)
    nonce = st.split(":", 1)[1] if ":" in st else ""

    r = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
        },
        auth=(CLIENT_ID, CLIENT_SECRET),
        timeout=20,
    )
    if r.status_code != 200:
        return JSONResponse(
            {"error": f"token endpoint HTTP {r.status_code}: {r.text[:200]}"},
            status_code=502,
        )
    tok = r.json()
    if "id_token" not in tok:
        return JSONResponse({"error": "no id_token in response"}, status_code=502)

    claims = _verify_id_token(tok["id_token"])
    if claims.get("nonce") != nonce:
        return JSONResponse({"error": "nonce mismatch"}, status_code=400)

    uid = claims.get("preferred_username") or claims.get("sub") or claims.get("email")
    if uid not in ALLOWED_UIDS:
        return JSONResponse(
            {"error": f"uid '{uid}' not in admin allowlist"}, status_code=403
        )

    session_token = _new_session(uid)
    next_url = request.cookies.get("wmauth_next", "/?admin")
    resp = RedirectResponse(url=next_url)
    resp.set_cookie(
        COOKIE, session_token, httponly=True, secure=True, samesite="lax",
        max_age=SESSION_TTL, path="/",
    )
    resp.delete_cookie("wmauth_state", path="/")
    resp.delete_cookie("wmauth_next", path="/")
    return resp


@app.get("/_oidc/auth")
def auth(request: Request):
    token = request.cookies.get(COOKIE)
    uid = _read_session(token) if token else None
    if not uid:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse({"uid": uid}, headers={"X-NC-Admin": uid})


@app.get("/_oidc/logout")
def logout(request: Request):
    token = request.cookies.get(COOKIE)
    if token:
        (SESSION_DIR / token).unlink(missing_ok=True)
    resp = RedirectResponse(url="https://webmail.ronzz.org/")
    resp.delete_cookie(COOKIE, path="/")
    return resp


@app.get("/_oidc/health")
def health():
    return {"ok": True}
