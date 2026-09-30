"""GitHub sign-in via the OAuth *device flow* (no client secret needed).

Flow: request a device/user code -> user approves it in the browser ->
we poll until GitHub hands us an access token -> the token is kept in the
system keyring and given to git through a one-shot credential helper.

Everything here is blocking; call it from a worker thread.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

SERVICE = "spyder-git-desktop"
KEYRING_USER = "github.com"
SETTINGS_PATH = os.path.join(os.path.expanduser("~"), ".spyder-git-desktop.json")

#: Put the Client ID of your own GitHub OAuth App here to ship the plugin
#: pre-configured (client IDs are public; do NOT put a client secret here).
DEFAULT_CLIENT_ID = ""

DEVICE_CODE_URL = "https://github.com/login/device/code"
TOKEN_URL = "https://github.com/login/oauth/access_token"
USER_URL = "https://api.github.com/user"
SCOPE = "repo"

_memory_store = {}  # fallback when no keyring backend is available


class AuthError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code

    @property
    def cancelled(self):
        return self.code == "cancelled"


# --------------------------------------------------------------------------- #
# Client ID (public identifier of the OAuth app)
# --------------------------------------------------------------------------- #
def get_client_id() -> str:
    cid = os.environ.get("SPYDER_GIT_DESKTOP_CLIENT_ID", "").strip()
    if cid:
        return cid
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as fh:
            cid = json.load(fh).get("client_id", "").strip()
    except (OSError, ValueError):
        cid = ""
    return cid or DEFAULT_CLIENT_ID


def save_client_id(client_id: str):
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    data["client_id"] = client_id.strip()
    with open(SETTINGS_PATH, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


# --------------------------------------------------------------------------- #
# Token storage
# --------------------------------------------------------------------------- #
def store_token(login: str, token: str):
    payload = json.dumps({"login": login, "token": token})
    try:
        import keyring

        keyring.set_password(SERVICE, KEYRING_USER, payload)
    except Exception:  # noqa: BLE001 - no usable backend: keep for this session only
        _memory_store["github"] = payload


def load_token():
    """Return ``(login, token)`` or ``None``."""
    payload = _memory_store.get("github")
    if payload is None:
        try:
            import keyring

            payload = keyring.get_password(SERVICE, KEYRING_USER)
        except Exception:  # noqa: BLE001
            payload = None
    if not payload:
        return None
    try:
        data = json.loads(payload)
        return data["login"], data["token"]
    except (ValueError, KeyError):
        return None


def clear_token():
    _memory_store.pop("github", None)
    try:
        import keyring

        keyring.delete_password(SERVICE, KEYRING_USER)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def _request(url, data=None, token=None):
    headers = {"Accept": "application/json", "User-Agent": "spyder-git-desktop"}
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except ValueError:
            raise AuthError(f"GitHub returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise AuthError(f"Could not reach GitHub: {exc}") from exc


# --------------------------------------------------------------------------- #
# Device flow
# --------------------------------------------------------------------------- #
def request_device_code(client_id: str) -> dict:
    resp = _request(DEVICE_CODE_URL, {"client_id": client_id, "scope": SCOPE})
    if "device_code" not in resp:
        code = resp.get("error", "")
        msg = resp.get("error_description") or code or "Unexpected response from GitHub"
        if code == "device_flow_disabled":
            msg = (
                "Device Flow is not enabled for this OAuth app. Open the app's "
                "settings on GitHub and tick “Enable Device Flow”."
            )
        raise AuthError(msg, code)
    resp.setdefault("interval", 5)
    resp.setdefault("expires_in", 900)
    return resp


def wait_for_token(client_id: str, device: dict, cancel: threading.Event):
    """Poll until the user approves; returns ``(login, token)`` and stores it."""
    interval = int(device["interval"])
    deadline = time.time() + int(device["expires_in"])
    while time.time() < deadline:
        if cancel.wait(interval):
            raise AuthError("Sign-in cancelled.", "cancelled")
        resp = _request(
            TOKEN_URL,
            {
                "client_id": client_id,
                "device_code": device["device_code"],
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
        )
        if "access_token" in resp:
            token = resp["access_token"]
            login = _request(USER_URL, token=token).get("login", "GitHub user")
            store_token(login, token)
            return login, token
        err = resp.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval = int(resp.get("interval", interval + 5))
            continue
        if err == "expired_token":
            break
        raise AuthError(resp.get("error_description") or err or "Sign-in failed.", err)
    raise AuthError("The sign-in code expired. Please try again.", "expired_token")
