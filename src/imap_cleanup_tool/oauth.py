"""OAuth2 (XOAUTH2) sign-in for IMAP/SMTP - provider-agnostic, JSON-driven.

Standard-library only (``urllib`` + ``json`` + ``base64``), so it stays inside the
CLI/core dependency-free rule. The flow:

* **device code** - the user signs in once interactively (open a URL, type a code);
  we get an access token **and a refresh token**. Device code works everywhere:
  web UI, CLI, and headless servers (nothing to redirect to).
* the refresh token is stored (by the caller, in a connection profile, encrypted
  like a password) and used to mint fresh access tokens **silently**, so the CLI
  and scheduled jobs run unattended.
* IMAP/SMTP then authenticate with the **XOAUTH2** SASL mechanism.

**Providers are data, not code.** Each provider (endpoints, client id, scope,
default IMAP/SMTP hosts) is an entry in ``assets/oauth_providers.json``; adding
Google or any other provider is just filling in that file - no code change. A
small built-in default keeps Microsoft working even if the file is missing.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class OAuthError(Exception):
    """A problem worth showing the user (auth declined, expired, network, …)."""


_PROVIDERS_FILE = Path(__file__).parent / "assets" / "oauth_providers.json"

_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

# Built-in fallback so Microsoft works even if the JSON asset is missing/corrupt.
# The JSON file (when present) is the source of truth and overrides this. The
# Microsoft client id is a *public* client (no secret) so shipping it is expected.
_BUILTIN: dict[str, dict] = {
    "microsoft": {
        "label": "Microsoft",
        "client_id": "2fb2537a-f98e-45b7-9155-f79cb7df4c9b",
        "client_secret": "",
        "devicecode_endpoint":
            "https://login.microsoftonline.com/common/oauth2/v2.0/devicecode",
        "token_endpoint":
            "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "scope": ("https://outlook.office.com/IMAP.AccessAsUser.All "
                  "https://outlook.office.com/SMTP.Send offline_access"),
        # The token endpoint now hands back the newer https://login.microsoft.com/device
        # portal, which is widely reported to reject otherwise-valid codes. Pin the
        # canonical, reliable portal instead.
        "verification_uri": "https://microsoft.com/devicelogin",
        "imap": {"host": "outlook.office365.com", "port": 993},
        "smtp": {"host": "smtp-mail.outlook.com", "port": 587,
                 "security": "starttls"},
    },
}


def load_providers() -> dict[str, dict]:
    """Return the provider config map, JSON file merged over the built-in default
    (JSON wins per key). Keys are lowercased provider ids."""
    providers = {k: dict(v) for k, v in _BUILTIN.items()}
    try:
        data = json.loads(_PROVIDERS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for name, cfg in data.items():
                if isinstance(cfg, dict):
                    providers[str(name).lower()] = dict(cfg)
    except (OSError, ValueError):
        pass
    return providers


def get_provider(name: str) -> dict:
    """Return one provider's config, or raise ``OAuthError`` if unknown/unconfigured."""
    p = load_providers().get((name or "").lower())
    if p is None:
        raise OAuthError(f"Unknown OAuth provider {name!r}.")
    if not (p.get("client_id") or "").strip():
        raise OAuthError(
            f"OAuth provider {name!r} has no client id configured yet "
            "(set it in assets/oauth_providers.json).")
    return p


def available_providers() -> list[dict]:
    """List providers for the UI: ``{name, label, configured, imap, smtp}``.
    ``configured`` is False for entries that still need a client id (e.g. Google
    until its credentials are filled in)."""
    out = []
    for name, cfg in load_providers().items():
        out.append({
            "name": name,
            "label": cfg.get("label") or name.title(),
            "configured": bool((cfg.get("client_id") or "").strip()),
            "imap": cfg.get("imap") or {},
            "smtp": cfg.get("smtp") or {},
        })
    out.sort(key=lambda p: (not p["configured"], p["label"].lower()))
    return out


def _post(url: str, data: dict) -> dict:
    """POST form-encoded data and return parsed JSON (also for HTTP error bodies,
    since OAuth endpoints return JSON errors we must inspect)."""
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))   # OAuth error JSON
        except (ValueError, OSError) as exc2:
            raise OAuthError(f"OAuth request failed (HTTP {exc.code}).") from exc2
    except (urllib.error.URLError, OSError) as exc:
        raise OAuthError(f"OAuth network error: {exc}") from exc


def _with_secret(cfg: dict, params: dict) -> dict:
    """Add ``client_secret`` to a request when the provider defines one (Microsoft
    public clients have none; Google installed apps do)."""
    secret = (cfg.get("client_secret") or "").strip()
    if secret:
        params = dict(params, client_secret=secret)
    return params


def start_device_code(provider: str = "microsoft") -> dict:
    """Begin the device-code flow. Returns a normalized dict with at least
    ``device_code``, ``user_code``, ``verification_uri``, ``interval``,
    ``expires_in``, ``message`` and ``provider``."""
    cfg = get_provider(provider)
    d = _post(cfg["devicecode_endpoint"],
              _with_secret(cfg, {"client_id": cfg["client_id"],
                                 "scope": cfg["scope"]}))
    if "device_code" not in d:
        raise OAuthError(d.get("error_description")
                         or "Could not start the sign-in (device code).")
    uri = d.get("verification_uri") or d.get("verification_url") or ""
    complete = d.get("verification_uri_complete", "")
    message = d.get("message", "")
    # A provider may pin a canonical verification URL (e.g. Microsoft's reliable
    # microsoft.com/devicelogin) in place of the one the API returns. When it does,
    # drop the API's complete-URL and message too (they point at the API's host) so
    # every caller shows the pinned URL.
    override = (cfg.get("verification_uri") or "").strip()
    if override:
        uri, complete, message = override, "", ""
    return {
        "device_code": d["device_code"],
        "user_code": d.get("user_code", ""),
        "verification_uri": uri,
        "verification_uri_complete": complete,
        "interval": int(d.get("interval", 5) or 5),
        "expires_in": int(d.get("expires_in", 900) or 900),
        "message": message,
        "provider": provider.lower(),
    }


def poll_device_code(device_code: str, *, provider: str = "microsoft",
                     interval: int = 5, should_stop=None,
                     sleep=time.sleep, monotonic=time.monotonic,
                     timeout: int = 900) -> dict:
    """Poll the token endpoint until the user finishes signing in. Returns the
    token response ({access_token, refresh_token, expires_in, ...}). Honors
    ``should_stop`` and gives up after ``timeout`` seconds."""
    cfg = get_provider(provider)
    deadline = monotonic() + timeout
    while True:
        if should_stop is not None and should_stop():
            raise OAuthError("Sign-in cancelled.")
        if monotonic() > deadline:
            raise OAuthError("Sign-in timed out (the code expired). Try again.")
        d = _post(cfg["token_endpoint"],
                  _with_secret(cfg, {"client_id": cfg["client_id"],
                                     "grant_type": _DEVICE_GRANT,
                                     "device_code": device_code}))
        err = d.get("error")
        if not err:
            return d
        if err == "authorization_pending":
            sleep(interval)
            continue
        if err == "slow_down":
            interval += 5
            sleep(interval)
            continue
        # authorization_declined / expired_token / access_denied / …
        raise OAuthError(d.get("error_description") or err)


def refresh_access_token(refresh_token: str, provider: str = "microsoft") -> dict:
    """Exchange a refresh token for a fresh access token (silent). The response
    may carry a rotated ``refresh_token`` the caller should persist."""
    cfg = get_provider(provider)
    d = _post(cfg["token_endpoint"],
              _with_secret(cfg, {"client_id": cfg["client_id"],
                                 "grant_type": "refresh_token",
                                 "scope": cfg["scope"],
                                 "refresh_token": refresh_token}))
    if "access_token" not in d:
        raise OAuthError(d.get("error_description")
                         or "Could not refresh the access token (sign in again).")
    return d


def access_token_for(profile: dict, persist=None) -> str:
    """Mint a fresh access token for a loaded OAuth ``profile`` dict (needs
    ``provider`` + ``refresh_token``). If the provider rotates the refresh token
    and ``persist`` is given, call ``persist(new_refresh_token)`` so the caller can
    store it - this is what keeps unattended scheduled jobs working over time."""
    provider = (profile.get("provider") or "").lower()
    refresh_token = profile.get("refresh_token") or ""
    if not refresh_token:
        raise OAuthError("This profile has no stored refresh token - sign in again.")
    tok = refresh_access_token(refresh_token, provider)
    rotated = tok.get("refresh_token")
    if rotated and rotated != refresh_token and callable(persist):
        persist(rotated)
    return tok["access_token"]


def xoauth2_bytes(user: str, access_token: str) -> bytes:
    """The raw XOAUTH2 SASL string as bytes - for ``imaplib.IMAP4.authenticate``
    (imaplib base64-encodes the callback's return value itself)."""
    return f"user={user}\x01auth=Bearer {access_token}\x01\x01".encode("utf-8")


def xoauth2_b64(user: str, access_token: str) -> str:
    """Base64 of the XOAUTH2 string - for SMTP ``AUTH XOAUTH2 <b64>``."""
    return base64.b64encode(xoauth2_bytes(user, access_token)).decode("ascii")
