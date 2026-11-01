"""Sign in with ChatGPT (ChatGPT plan usage for open-source, locally hosted apps).

Implements the official flow documented at https://developers.openai.com/siwc/token-sharing-open-source :
dynamic client registration, a 127.0.0.1 loopback callback, PKCE + state + nonce, ID-token validation
against OpenAI's JWKS, the chatgpt.tokens.use.direct scope check, refresh, and revocation on sign-out.

This only works when the app runs on the user's own computer: the OAuth callback goes to 127.0.0.1.
Credentials are stored outside the project folder in an owner-only file and are never logged.
"""

import base64
import hashlib
import json
import os
import secrets
import tempfile
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import jwt
import requests

APP_NAME = "ServiceNow User Guide Reviewer"
ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = "https://auth.openai.com/api/accounts/authorize"
TOKEN_URL = "https://auth.openai.com/api/accounts/oauth/token"
REVOKE_URL = "https://auth.openai.com/api/accounts/oauth/revoke"
JWKS_URL = "https://auth.openai.com/.well-known/jwks.json"
RESOURCE = "https://api.openai.com/v1"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
USAGE_URL = "https://chatgpt.com/settings/usage"
CALLBACK_PATH = "/auth/callback"
PREFERRED_PORT = 1455
UNUSABLE_REFRESH = {"invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired",
                    "refresh_token_invalidated", "refresh_token_reused"}

_base = Path(os.environ.get("APPDATA") or Path.home() / ".config")
CONFIG_DIR = _base / "servicenow-guide-reviewer"
HOST_FILE = CONFIG_DIR / "host.json"
CREDENTIALS_FILE = CONFIG_DIR / "chatgpt_credentials.json"

_lock = threading.Lock()  # serializes refreshes and file writes
_pending = {}             # the single in-flight authorization attempt


class AuthError(Exception):
    pass


# ---------------------------------------------------------------- storage

def _write_private(path, data):
    """Atomic write with owner-only permissions."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=CONFIG_DIR, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _read(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def host_id():
    """Stable, opaque host identifier, created once before the first sign-in."""
    data = _read(HOST_FILE)
    if not data.get("ext_agent_host_id"):
        data = {"ext_agent_host_id": f"urn:uuid:{uuid.uuid4()}"}
        _write_private(HOST_FILE, data)
    return data["ext_agent_host_id"]


def load_account():
    return _read(CREDENTIALS_FILE)


def is_signed_in():
    acct = load_account()
    return bool(acct.get("access_token")) and PLAN_SCOPE in acct.get("scopes", [])


# ---------------------------------------------------------------- authorization

def _b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self.send_response(404)
            self.end_headers()
            return
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            _complete(params)
            body = "Signed in. You can close this tab and return to the ServiceNow User Guide Reviewer."
        except Exception as e:  # message is shown to the user, never contains tokens
            _pending["error"] = str(e)
            body = f"Sign-in did not complete: {e}. Return to the app to try again."
        _pending["done"] = True
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())
        _stop(self.server)

    def log_message(self, *args):  # keep the authorization code out of logs
        pass


def _stop(server):
    """Stop a callback listener (without blocking the calling thread)."""
    if _pending.get("server") is server:
        _pending["server"] = None
    def stop():
        server.shutdown()
        server.server_close()
    threading.Thread(target=stop, daemon=True).start()


def start_sign_in():
    """Start the loopback listener and open the system browser. Returns the authorization URL."""
    if _pending.get("server"):
        _stop(_pending["server"])
    try:
        server = HTTPServer(("127.0.0.1", PREFERRED_PORT), _CallbackHandler)
    except OSError:
        server = HTTPServer(("127.0.0.1", 0), _CallbackHandler)  # only the port may vary
    redirect_uri = f"http://127.0.0.1:{server.server_address[1]}{CALLBACK_PATH}"

    account = load_account()
    verifier = _b64url(secrets.token_bytes(48))
    state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    params = {
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "resource": RESOURCE,
        "state": state,
        "nonce": nonce,
        "code_challenge_method": "S256",
        "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
        "ext_agent_host_id": host_id(),
    }
    if account.get("client_id"):  # returning account: reuse the issued client ID
        params["client_id"] = account["client_id"]
        if account.get("id_token"):
            params["id_token_hint"] = account["id_token"]
        if account.get("email"):
            params["login_hint"] = account["email"]
    else:
        params["client_id"] = "dynamic_agent_client"
        params["agent_name_hint"] = APP_NAME

    _pending.clear()
    _pending.update(state=state, nonce=nonce, verifier=verifier, redirect_uri=redirect_uri,
                    client_id=account.get("client_id"), server=server, done=False, started=time.time())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"{AUTHORIZE_URL}?{urlencode(params)}"
    webbrowser.open(url)
    return url


def sign_in_status():
    """None while waiting, "ok" on success, or an error message."""
    if not _pending:
        return None
    if _pending.get("done"):
        return _pending.get("error") or "ok"
    if time.time() - _pending["started"] > 600:
        cancel_sign_in()
        return "Sign-in timed out. Please try again."
    return None


def cancel_sign_in():
    if _pending.get("server"):
        _stop(_pending["server"])
    _pending.clear()


def _complete(params):
    if not secrets.compare_digest(params.get("state", ""), _pending.get("state", "")):
        raise AuthError("the sign-in response did not match this attempt (state mismatch)")
    if params.get("error") == "access_denied":
        raise AuthError("access was declined. ChatGPT plan use was not enabled")
    if params.get("error"):
        raise AuthError(f"OpenAI returned an error ({params['error']})")

    saved_id = _pending.get("client_id")
    returned_id = params.get("client_id")
    if saved_id and returned_id and returned_id != saved_id:
        raise AuthError("the response belongs to a different app registration")
    client_id = saved_id or returned_id
    if not client_id or client_id == "dynamic_agent_client":
        raise AuthError("registration is incomplete (no client ID was issued)")

    resp = requests.post(TOKEN_URL, timeout=30, data={
        "grant_type": "authorization_code", "client_id": client_id, "code": params.get("code", ""),
        "code_verifier": _pending["verifier"], "redirect_uri": _pending["redirect_uri"], "resource": RESOURCE,
    })
    if resp.status_code != 200:
        raise AuthError(f"code exchange failed ({_error_code(resp)}). Please sign in again")
    tokens = resp.json()

    claims = validate_id_token(tokens.get("id_token", ""), client_id, _pending["nonce"])
    previous = load_account()
    if previous.get("subject") and previous.get("client_id") == client_id and previous["subject"] != claims["sub"]:
        raise AuthError("you signed in with a different ChatGPT account than the one registered")

    scopes = sorted((tokens.get("scope") or params.get("scope") or "").split())
    record = {
        "email": claims.get("email", ""), "issuer": ISSUER, "subject": claims["sub"], "client_id": client_id,
        "ext_agent_host_id": host_id(), "id_token": tokens["id_token"], "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token", ""), "token_type": tokens.get("token_type", "Bearer"),
        "expires_in": tokens.get("expires_in", 3600), "scopes": scopes, "saved_at": time.time(),
        "welcomed": previous.get("welcomed", False) if previous.get("subject") == claims["sub"] else False,
    }
    with _lock:
        _write_private(CREDENTIALS_FILE, record)
    if PLAN_SCOPE not in scopes:
        raise AuthError("you are signed in, but permission to use your ChatGPT plan was not granted")


_jwks = None


def validate_id_token(id_token, client_id, nonce, jwks_client=None):
    global _jwks
    if jwks_client is None:
        _jwks = _jwks or jwt.PyJWKClient(JWKS_URL, cache_keys=True)
        jwks_client = _jwks
    try:
        key = jwks_client.get_signing_key_from_jwt(id_token)
        claims = jwt.decode(id_token, key.key, algorithms=["RS256"], audience=client_id, issuer=ISSUER,
                            leeway=5, options={"require": ["sub", "exp", "iat"]})
    except (jwt.ImmatureSignatureError, jwt.ExpiredSignatureError):
        raise AuthError("your computer's clock is out of sync with OpenAI. In Windows, open Settings > Time & "
                        "language > Date & time, turn on 'Set time automatically', click 'Sync now', then sign in "
                        "again") from None
    except jwt.PyJWTError as e:
        raise AuthError(f"the ID token could not be verified ({type(e).__name__})") from None
    if claims.get("nonce") != nonce:
        raise AuthError("the ID token nonce did not match")
    if not isinstance(claims.get("sub"), str) or not claims["sub"]:
        raise AuthError("the ID token did not contain a subject")
    if "sid" in claims and (not isinstance(claims["sid"], str) or not claims["sid"].strip()):
        raise AuthError("the ID token contained an invalid session identifier")
    return claims


# ---------------------------------------------------------------- tokens

def _error_code(resp):
    try:
        body = resp.json()
    except ValueError:
        return f"HTTP {resp.status_code}"
    err = body.get("error")
    if isinstance(err, dict):
        return err.get("code") or err.get("type") or f"HTTP {resp.status_code}"
    return err or body.get("detail") or f"HTTP {resp.status_code}"


def access_token():
    """Return a valid access token, refreshing it shortly before expiry."""
    with _lock:
        acct = load_account()
        if not acct.get("access_token"):
            raise AuthError("Not signed in with ChatGPT.")
        if time.time() < acct["saved_at"] + acct.get("expires_in", 3600) - 120:
            return acct["access_token"]
        if not acct.get("refresh_token"):
            raise AuthError("Your ChatGPT session expired. Please sign in again.")
        resp = requests.post(TOKEN_URL, timeout=30, data={
            "grant_type": "refresh_token", "client_id": acct["client_id"],
            "refresh_token": acct["refresh_token"], "resource": RESOURCE,
        })
        if resp.status_code != 200:
            code = _error_code(resp)
            if code in UNUSABLE_REFRESH:
                _clear_tokens(acct)
                raise AuthError("Your ChatGPT session ended. Please sign in again.")
            raise AuthError(f"Could not refresh the ChatGPT session ({code}). Try again shortly.")
        tokens = resp.json()
        acct.update(access_token=tokens["access_token"], expires_in=tokens.get("expires_in", 3600),
                    saved_at=time.time(), refresh_token=tokens.get("refresh_token", acct["refresh_token"]))
        if tokens.get("scope"):
            acct["scopes"] = sorted(tokens["scope"].split())
        _write_private(CREDENTIALS_FILE, acct)
        return acct["access_token"]


def _clear_tokens(acct):
    for k in ("access_token", "refresh_token", "id_token"):
        acct.pop(k, None)
    _write_private(CREDENTIALS_FILE, acct)  # keep email/client_id mapping for the next sign-in


def mark_welcomed():
    with _lock:
        acct = load_account()
        acct["welcomed"] = True
        _write_private(CREDENTIALS_FILE, acct)


def sign_out():
    """Revoke the refresh token, then clear tokens locally. Returns True if revocation was confirmed."""
    with _lock:
        acct = load_account()
        confirmed = False
        if acct.get("refresh_token"):
            for attempt in range(3):
                try:
                    r = requests.post(REVOKE_URL, timeout=15, data={
                        "token": acct["refresh_token"], "token_type_hint": "refresh_token", "client_id": acct["client_id"]})
                    if r.status_code == 200:
                        confirmed = True
                        break
                    if r.status_code < 500:
                        break
                except requests.RequestException:
                    pass
                time.sleep(2 ** attempt)
        if acct:
            _clear_tokens(acct)
        return confirmed


def list_models():
    resp = requests.get(f"{RESOURCE}/models", timeout=30, headers={"Authorization": f"Bearer {access_token()}"})
    if resp.status_code != 200:
        raise AuthError(f"Could not load models ({_error_code(resp)}).")
    return [(m["slug"], m.get("display_name", m["slug"])) for m in resp.json().get("models", [])
            if m.get("visibility") == "list"]
