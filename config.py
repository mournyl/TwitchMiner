"""
Shared settings + the single source of truth for Twitch client identity.

IMPORTANT — header discipline:
Twitch's GQL gateway validates that the client identity used to mint a
token (via the device-code login) matches the identity sent on every
later authenticated request. Mixing client types (e.g. logging in as one
client but sending GQL requests with another client's Client-ID/User-Agent)
is what produces `401 Unauthorized: The "Authorization" token is invalid`
even though the token itself is fine. This was confirmed the hard way
against a real Twitch deployment: ANDROID_APP got blocked on the
device-code endpoint, MOBILE_WEB logged in fine but 401'd on every GQL
call regardless of how carefully headers were kept in sync, and SMARTBOX
is the client type that currently works end-to-end (device-code login +
GQL + drop crediting). These values are taken directly from the reference
client's own constants.py, not guessed.

Everything in this app — auth.py's device-code login AND gql.py's GQL
client — must import CLIENT_ID / CLIENT_URL / USER_AGENT from here rather
than hardcoding their own, so there is exactly one place that can ever
drift out of sync.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


class ClientInfo:
    def __init__(self, client_url: str, client_id: str, user_agent: str) -> None:
        self.CLIENT_URL = client_url
        self.CLIENT_ID = client_id
        self.USER_AGENT = user_agent


class ClientType:
    WEB = ClientInfo(
        "https://www.twitch.tv",
        "kimne78kx3ncx6brgo4mv6wki5h1ko",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    )
    MOBILE_WEB = ClientInfo(
        "https://m.twitch.tv",
        "r8s4dac0uhzifbpu9sjdiwzctle17ff",
        "Mozilla/5.0 (Linux; Android 16) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/138.0.7204.158 Mobile Safari/537.36",
    )
    ANDROID_APP = ClientInfo(
        "https://www.twitch.tv",
        "kd1unb4b3q4t58fwlpcbzcbnm76a8fp",
        "Dalvik/2.1.0 (Linux; U; Android 12; tv.twitch.android.app)",
    )
    # Confirmed-working client for the device-code flow + GQL, as of
    # 2026-09-19. Keep the whole app pointed at this one via ACTIVE below.
    SMARTBOX = ClientInfo(
        "https://android.tv.twitch.tv",
        "ue6666qo983tsx6so1t0vnawi233wa",
        "Mozilla/5.0 (Linux; Android 7.1; Smart Box C1) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    )


# Single switch for the LOGIN/GQL identity. Change this one line to try
# a different client type there — never override CLIENT_ID/USER_AGENT
# ad-hoc in auth.py or gql.py, that's exactly how the login/GQL mismatch
# bug happens.
ACTIVE_CLIENT: ClientInfo = ClientType.SMARTBOX

# Back-compat aliases used by auth.py / gql.py — LOGIN + GQL identity only.
ANDROID_CLIENT_ID = ACTIVE_CLIENT.CLIENT_ID
ACTIVE_CLIENT_URL = ACTIVE_CLIENT.CLIENT_URL
ACTIVE_USER_AGENT = ACTIVE_CLIENT.USER_AGENT

# Confirmed against the reference client's core/client.py: self._client_type
# is hardcoded to ANDROID_APP, completely separate from LOGIN_CLIENT
# (ACTIVE_CLIENT above, SMARTBOX). ANDROID_APP is the session's actual
# default identity — used for page scrapes, the beacon POST, and the
# device-id cookie bootstrap. SMARTBOX is used ONLY for the OAuth
# device-code login and GQL requests (forced via an explicit per-request
# header override), because those two endpoints reject other clients.
# Do not collapse these into one identity — that was the bug: every
# non-GQL request was carrying SMARTBOX's headers/User-Agent instead of
# ANDROID_APP's.
BROWSE_CLIENT: ClientInfo = ClientType.ANDROID_APP

def _app_dir() -> Path:
    # When packaged with PyInstaller, __file__ points into a temp folder
    # that's wiped on exit — keep data/ next to the .exe instead so saved
    # accounts, cookies and settings survive restarts.
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


DATA_DIR = _app_dir() / "data"
ACCOUNTS_PATH = DATA_DIR / "accounts.json"

# Verbose [diag]-tagged request/response logging in gql.py / miner.py was
# essential while tracking down why drop progress wasn't crediting at all.
# That's fixed now, so it's off by default to keep the Node Log readable —
# flip to True to bring the full per-request diagnostics back.
DEBUG_LOG = False

# In-app updater: compared against the latest GitHub release.
APP_VERSION = "1.4"
GITHUB_REPO = "mournyl/TwitchMiner"


class Settings:
    def __init__(self, filename: str = "settings.json"):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._path = DATA_DIR / filename
        self.watch_interval: int = 59
        self.campaign_refresh_interval: int = 300
        # (watch_interval confirmed 2026-09-20 directly from the SimpliAj
        # fork's src/config/constants.py: WATCH_INTERVAL = timedelta(seconds=59).
        # An earlier "~20s" figure in this project was a misread of an
        # unrelated hardcoded asyncio.sleep(20) used for a mid-cycle
        # websocket progress check in watch_service.py, not the actual
        # heartbeat cadence. Sending heartbeats 3x faster than the real
        # client (every 20s instead of 59s) doesn't triple crediting speed —
        # Twitch's server doesn't credit more than ~1 minute per wall-clock
        # minute, and hammering the endpoint at 3x rate risks partial
        # throttling/dropped credits, which nets out SLOWER than the
        # reference client, matching what was observed.
        self.excluded_games: list[str] = []
        # Off = drops are only claimed when you press a Claim button.
        # On = claim as soon as Twitch reports a drop done (PubSub) plus
        # the periodic sweep every campaign_refresh_interval.
        self.auto_claim: bool = True
        # On = a node only ever watches a channel that is live in its target
        # game right now. If the stream ends or the streamer switches game,
        # the node leaves and re-picks; it never follows a raid/host and an
        # idle node won't follow a peer that is on a different game.
        self.strict_game: bool = True
        # Ordered list of games to mine, shared by every node (set on the
        # Home page). Empty = each node picks a game on its own.
        self.game_list: list[str] = []
        # Minimum minutes a node stays on a game from its list before it may
        # swap to the next one. Twitch only shows a drop's progress once it
        # has some, so a game needs time before "not earning" can be trusted.
        self.game_swap_minutes: int = 60
        self.load()

    def load(self) -> None:
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text("utf-8"))
                self.watch_interval = data.get("watch_interval", 59)
                self.campaign_refresh_interval = data.get("campaign_refresh_interval", 300)
                self.excluded_games = data.get("excluded_games", [])
                self.auto_claim = bool(data.get("auto_claim", True))
                self.strict_game = bool(data.get("strict_game", True))
                games = data.get("game_list")
                if not isinstance(games, list):
                    # Older saves kept one list per account; take the first non-empty one.
                    old = data.get("game_lists") or {}
                    games = next((v for v in old.values() if isinstance(v, list) and v), []) \
                        if isinstance(old, dict) else []
                self.game_list = [str(g) for g in games if g]
                self.game_swap_minutes = max(1, int(data.get("game_swap_minutes", 60)))
            except Exception:
                pass

    def save(self) -> None:
        data = {
            "watch_interval": self.watch_interval,
            "campaign_refresh_interval": self.campaign_refresh_interval,
            "excluded_games": self.excluded_games,
            "auto_claim": self.auto_claim,
            "strict_game": self.strict_game,
            "game_list": self.game_list,
            "game_swap_minutes": self.game_swap_minutes,
        }
        self._path.write_text(json.dumps(data, indent=2), "utf-8")