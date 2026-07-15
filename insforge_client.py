"""InsForge account for the desktop app — strictly optional.

MeetingScribe works fully offline; signing in only enables "View on phone"
sync. Auth is InsForge's REST API (email/password or Google), and the
long-lived refresh token lives in the macOS Keychain — never in this
folder, never in a file.

Endpoints (discovered against this project's backend):
  POST /api/auth/users              {email,password,name} -> sign up
  POST /api/auth/sessions           {email,password}      -> sign in
  POST /api/auth/email/verify       {email,otp}           -> verify code
  POST /api/auth/refresh            cookie+CSRF           -> new access token
  GET  /api/auth/oauth/<provider>?redirect_uri&code_challenge... -> authorize
  POST /api/auth/oauth/exchange     {code,code_verifier}  -> sign in
  /api/database/records/<table>     PostgREST-style data API (RLS enforced)

The access token is short-lived and kept in memory only. The anon key is a
public client key (row-level security does the real gating).
"""

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("meetingscribe.insforge")

BASE_URL = "https://5uh76ypz.us-east.insforge.app"
ANON_KEY = "anon_dbf1b8c9f04392c12792087d5906c75ff9133b59d7d00873403547e8bdb602e1"
CALLBACK_PATH = "/api/auth/callback"  # must stay in allowedRedirectUrls

_KEYCHAIN_SERVICE = "MeetingScribe"
_KEYCHAIN_ACCOUNT = "insforge"

_lock = threading.RLock()
_access_token = None
_access_expiry = 0.0
_pending_oauth = {}  # state -> {verifier, at}


class AuthError(RuntimeError):
    pass


# ------------------------------------------------------------------ keychain --

def _keychain_read():
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", _KEYCHAIN_SERVICE,
             "-a", _KEYCHAIN_ACCOUNT, "-w"],
            capture_output=True, text=True, timeout=10)
        if proc.returncode != 0:
            return None
        return json.loads(proc.stdout.strip())
    except (subprocess.SubprocessError, OSError, ValueError):
        return None


def _keychain_write(data):
    try:
        subprocess.run(
            ["security", "add-generic-password", "-U", "-s", _KEYCHAIN_SERVICE,
             "-a", _KEYCHAIN_ACCOUNT, "-w", json.dumps(data)],
            capture_output=True, text=True, timeout=10, check=True)
        return True
    except (subprocess.SubprocessError, OSError):
        log.warning("could not write the Keychain item")
        return False


def _keychain_delete():
    subprocess.run(
        ["security", "delete-generic-password", "-s", _KEYCHAIN_SERVICE,
         "-a", _KEYCHAIN_ACCOUNT],
        capture_output=True, text=True, timeout=10)


# ---------------------------------------------------------------------- http --

def _request(method, path, *, token=ANON_KEY, body=None, headers=None, timeout=15):
    """-> (status, parsed_json, response_headers)."""
    req = urllib.request.Request(
        BASE_URL + path, method=method,
        data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None), dict(r.headers)
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read() or b"null")
        except ValueError:
            payload = None
        return e.code, payload, dict(e.headers or {})
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise AuthError(f"Could not reach the sync service ({e}).") from e


def _refresh_cookie_from(headers):
    for key, value in headers.items():
        if key.lower() == "set-cookie" and "insforge_refresh_token=" in value:
            match = re.search(r"insforge_refresh_token=([^;]+)", value)
            if match:
                return match.group(1)
    return None


def _store_session(data, headers, email=None):
    """Persist a sign-in/exchange response. -> state dict."""
    global _access_token, _access_expiry
    refresh = _refresh_cookie_from(headers)
    user = data.get("user") or {}
    record = {
        "refresh_token": refresh,
        "csrf_token": data.get("csrfToken"),
        "email": email or user.get("email"),
        "user_id": user.get("id"),
    }
    if not refresh:
        log.warning("sign-in response had no refresh cookie; session won't persist")
    _keychain_write(record)
    with _lock:
        _access_token = data.get("accessToken")
        _access_expiry = _jwt_expiry(_access_token)
    return state()


def _jwt_expiry(token):
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0))
    except Exception:
        return time.time() + 300  # unknown — assume 5 minutes


# ---------------------------------------------------------------------- auth --

def state():
    record = _keychain_read()
    if not record or not record.get("refresh_token"):
        return {"signed_in": False, "email": None, "user_id": None}
    return {"signed_in": True, "email": record.get("email"),
            "user_id": record.get("user_id")}


def sign_up(email, password):
    status, data, headers = _request(
        "POST", "/api/auth/users",
        body={"email": email, "password": password})
    if status in (200, 201) and (data or {}).get("accessToken"):
        return _store_session(data, headers, email=email)
    if status in (200, 201) and (data or {}).get("requireEmailVerification"):
        return {"needs_verification": True, "email": email}
    raise AuthError(_friendly(data, "Could not create the account."))


def verify_email(email, code):
    status, data, headers = _request(
        "POST", "/api/auth/email/verify",
        body={"email": email, "otp": str(code).strip()})
    if status == 200 and (data or {}).get("accessToken"):
        return _store_session(data, headers, email=email)
    raise AuthError(_friendly(data, "That code didn't work — check the email and try again."))


def sign_in(email, password):
    status, data, headers = _request(
        "POST", "/api/auth/sessions",
        body={"email": email, "password": password})
    if status == 200 and (data or {}).get("accessToken"):
        return _store_session(data, headers, email=email)
    if status == 200 and (data or {}).get("requireEmailVerification"):
        return {"needs_verification": True, "email": email}
    raise AuthError(_friendly(data, "Sign-in failed — check the email and password."))


def sign_out():
    global _access_token, _access_expiry
    with _lock:
        _access_token = None
        _access_expiry = 0.0
    _keychain_delete()
    return state()


def _friendly(data, fallback):
    message = (data or {}).get("message") or ""
    return f"{fallback}" if not message else f"{fallback} ({message})"


# --------------------------------------------------------------------- oauth --

def oauth_start(provider, port):
    """-> the authorize URL to open in the user's browser."""
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    oauth_state = secrets.token_urlsafe(16)
    now = time.time()
    with _lock:
        for key in [k for k, v in _pending_oauth.items() if now - v["at"] > 600]:
            del _pending_oauth[key]
        _pending_oauth[oauth_state] = {"verifier": verifier, "at": now}
    redirect_uri = f"http://127.0.0.1:{port}{CALLBACK_PATH}"
    query = urllib.parse.urlencode({
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": oauth_state,
    })
    # The backend answers with the provider's real authorize URL.
    status, data, _ = _request("GET", f"/api/auth/oauth/{provider}?{query}")
    if status == 200 and (data or {}).get("authUrl"):
        return data["authUrl"]
    raise AuthError(_friendly(data, f"Could not start {provider} sign-in."))


def oauth_finish(code, oauth_state=None):
    with _lock:
        pending = _pending_oauth.pop(oauth_state, None) if oauth_state else None
        if pending is None and len(_pending_oauth) == 1:
            # Some providers drop the state param; a single in-flight attempt
            # is unambiguous.
            pending = _pending_oauth.popitem()[1]
    if pending is None:
        raise AuthError("This sign-in link expired — try again.")
    status, data, headers = _request(
        "POST", "/api/auth/oauth/exchange",
        body={"code": code, "code_verifier": pending["verifier"]})
    if status == 200 and (data or {}).get("accessToken"):
        return _store_session(data, headers)
    raise AuthError(_friendly(data, "Google sign-in failed — try again."))


# ------------------------------------------------------------- access tokens --

def access_token():
    """A valid user access token, refreshing if needed. None when signed out."""
    global _access_token, _access_expiry
    with _lock:
        if _access_token and time.time() < _access_expiry - 30:
            return _access_token
    record = _keychain_read()
    if not record or not record.get("refresh_token"):
        return None
    status, data, headers = _request(
        "POST", "/api/auth/refresh",
        headers={
            "Cookie": f"insforge_refresh_token={record['refresh_token']}",
            "X-CSRF-Token": record.get("csrf_token") or "",
        })
    if status == 200 and (data or {}).get("accessToken"):
        new_refresh = _refresh_cookie_from(headers)
        if new_refresh and new_refresh != record["refresh_token"]:
            record["refresh_token"] = new_refresh
        if data.get("csrfToken"):
            record["csrf_token"] = data["csrfToken"]
        _keychain_write(record)
        with _lock:
            _access_token = data["accessToken"]
            _access_expiry = _jwt_expiry(_access_token)
        return _access_token
    log.warning("token refresh failed (%s); signing out locally", status)
    sign_out()
    return None


# ----------------------------------------------------------------- data API --

def db_request(method, path_and_query, *, body=None, prefer=None):
    """Data-API call with the signed-in user's JWT. -> (status, json)."""
    token = access_token()
    if token is None:
        raise AuthError("Not signed in.")
    headers = {"Prefer": prefer} if prefer else {}
    status, data, _ = _request(
        method, f"/api/database/records/{path_and_query}",
        token=token, body=body, headers=headers, timeout=30)
    return status, data
