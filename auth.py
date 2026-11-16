"""
Real OAuth device-code login against Twitch's auth servers.
Always requests a fresh login session without saving or checking previous tokens.
"""
from __future__ import annotations

import time
import webbrowser

import requests

from config import ANDROID_CLIENT_ID, ACTIVE_CLIENT_URL, ACTIVE_USER_AGENT

DEVICE_CODE_URL = "https://id.twitch.tv/oauth2/device"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"

SCOPES = ""  # "channel_read"/"user_read" are deprecated v5-API scope names —
              # not valid modern Twitch scopes. The private GQL endpoint
              # doesn't need any extra scope beyond a valid logged-in
              # session, so leaving this blank matches what actually works.

# Every request in the login flow carries the SAME client identity
# (config.ACTIVE_CLIENT) that gql.py uses afterwards. This is the fix for
# the "Authorization token is invalid" 401 class of bug: a token minted
# under one client's identity gets rejected by GQL if later requests show
# up as a different client, even if the Client-ID header alone matches.
_IDENTITY_HEADERS = {
    "User-Agent": ACTIVE_USER_AGENT,
    "Origin": ACTIVE_CLIENT_URL,
    "Referer": ACTIVE_CLIENT_URL + "/",
}


class AuthError(RuntimeError):
    pass


def _session(proxy: str | None) -> requests.Session:
    """Session routed through `proxy` (None = direct, env proxies allowed)."""
    from proxies import requests_proxies

    s = requests.Session()
    if proxy:
        s.trust_env = False  # env HTTP(S)_PROXY would otherwise override ours
        s.proxies.update(requests_proxies(proxy))
    return s


def _request_device_code(proxy: str | None = None) -> dict:
    resp = _session(proxy).post(
        DEVICE_CODE_URL,
        data={"client_id": ANDROID_CLIENT_ID, "scope": SCOPES},
        headers=_IDENTITY_HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def _poll_for_token(device_code: str, interval: int, proxy: str | None = None) -> dict:
    sess = _session(proxy)
    while True:
        time.sleep(interval)
        resp = sess.post(
            TOKEN_URL,
            data={
                "client_id": ANDROID_CLIENT_ID,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "scope": SCOPES,
            },
            headers=_IDENTITY_HEADERS,
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json()
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        msg = body.get("message", "")
        if "authorization_pending" in msg:
            continue
        if "expired" in msg:
            raise AuthError("Device code expired before approval — run login again.")
        raise AuthError(f"Login failed: {msg or resp.text}")


def validate_token(access_token: str, proxy: str | None = None) -> dict | None:
    resp = _session(proxy).get(
        VALIDATE_URL,
        headers={"Authorization": f"OAuth {access_token}", **_IDENTITY_HEADERS},
        timeout=15,
    )
    if resp.status_code == 200:
        return resp.json()
    return None


def login(account_name: str = "default", open_browser: bool = True, proxy: str | None = None) -> tuple[str, str]:
    """
    Triggers a fresh device-code authorization flow every time.
    Does not read from or write to disk.
    """
    device = _request_device_code(proxy)
    # Open the activate page with the code already filled in.
    verify_url = f"https://www.twitch.tv/activate?device-code={device['user_code']}"
    user_code = device["user_code"]

    print(f"\nLogging in account [{account_name}]:")
    print(f"  URL: {verify_url}")
    print(f"  Code: {user_code}\n")
    if open_browser:
        try:
            webbrowser.open(verify_url)
        except Exception:
            pass

    token_data = _poll_for_token(device["device_code"], device.get("interval", 5), proxy)
    val = validate_token(token_data["access_token"], proxy)
    user_login = val.get("login", account_name) if val else account_name

    print("Logged in successfully.")
    if val:
        print(f"  Validated as: {val.get('login')}  |  granted scopes: {val.get('scopes')}\n")
    return token_data["access_token"], user_login