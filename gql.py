from __future__ import annotations
# GQL_VERSION_MARKER: stream-info-v1 (has get_stream_info, get_available_drop_campaigns, persisted-query support)


import base64
import json
import pickle
import re
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from urllib.parse import urljoin
from pathlib import Path
from urllib.parse import urlparse

import requests

from config import ANDROID_CLIENT_ID, ACTIVE_CLIENT_URL, ACTIVE_USER_AGENT, BROWSE_CLIENT, DATA_DIR, DEBUG_LOG

# Confirmed against the reference's core/client.py + auth/auth_state.py:
# TWO separate client identities are in play simultaneously, never one:
#   - BROWSE_CLIENT (ANDROID_APP): self._client_type, hardcoded, is the
#     session's actual default identity — its User-Agent is the session-
#     level default, and it's what page scrapes, the beacon POST, and the
#     device-id cookie bootstrap all use, with NO header override at all.
#   - config.ACTIVE_CLIENT (SMARTBOX, aliased below): used ONLY for the
#     OAuth device-code login and for GQL requests, via an explicit
#     per-request header override (_gql_headers()) — because those two
#     endpoints are the ones that reject other clients.
# Collapsing these into one identity (what this file used to do) meant
# every beacon POST and page scrape carried SMARTBOX's headers instead of
# ANDROID_APP's — a real, structural mismatch from the reference.
_BROWSE_HOST = urlparse(BROWSE_CLIENT.CLIENT_URL).hostname or ""

GQL_URL = "https://gql.twitch.tv/gql"
# Must stay the same client identity that minted the token in auth.py
# (config.ACTIVE_CLIENT) — see the comment there for why a mismatch here
# silently 401s every GQL call regardless of the token being valid.
CLIENT_ID = ANDROID_CLIENT_ID

_COOKIES_DIR = DATA_DIR / "cookies"


def _isonow() -> str:
    # matches the reference client's utils.isonow(): ISO-8601 UTC with
    # millisecond precision, e.g. 2026-08-26T20:11:03.451Z
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


class GQLError(Exception):
    pass


class GQLClient:
    def __init__(self, access_token: str, account_name: str = "default", on_log=None, proxy: str | None = None):
        self._log = on_log or (lambda msg: None)
        self.access_token = access_token
        self.device_id: str | None = None
        # Matches the reference's create_nonce(CHARS_HEX_LOWER, 16) for
        # session_id — generated once per process, used as Client-Session-Id.
        self._client_session_id = uuid.uuid4().hex[:16]

        self.session = requests.Session()
        self.proxy: str | None = None
        # Consecutive network-level failures (timeouts, refused, proxy
        # errors) — reset by any successful request. MiningNode uses this to
        # fail over to another working proxy.
        self.net_failures = 0
        self.last_net_error = ""
        # Must be applied before the first request below (device-id
        # bootstrap, user-id fetch) so no traffic ever leaves direct.
        self.set_proxy(proxy)
        # Session-level default is ONLY User-Agent, and it's BROWSE_CLIENT's
        # (ANDROID_APP) — matches the reference's get_session():
        # headers={"User-Agent": self._client_type.USER_AGENT}. GQL calls
        # override this per-request via _gql_headers(); page scrapes and
        # the beacon POST use this bare default with no override at all,
        # exactly like the reference's channel.py.
        self.session.headers.update({"User-Agent": BROWSE_CLIENT.USER_AGENT})

        # The reference client persists its cookie jar to disk and reloads
        # it on every startup — cookies Twitch sets (anti-bot/session
        # tracking, not auth) build up trust over repeated visits rather
        # than looking like a brand-new client every single run. Mirror
        # that here, keyed by account name.
        self._cookies_path = _COOKIES_DIR / f"{account_name}.pkl"
        self._load_cookies()
        self._bootstrap_device_id()

        self.user_id: str | None = self._fetch_user_id()
        # login -> discovered Spade/beacon URL. Each live channel serves
        # its own beacon endpoint (not a fixed global spade.twitch.tv URL);
        # scraped once per channel and cached.
        self._spade_url_cache: dict[str, str] = {}
        # HLS segment watching state (see poll_segments). Keyed by login.
        self._stream_url_cache: dict[str, tuple[str, str]] = {}  # login -> (broadcast_id, media playlist url)
        self._seen_segments: dict[str, OrderedDict] = {}         # login -> recent segment urls
        self._seen_broadcast: dict[str, str] = {}                # login -> broadcast_id the seen-set belongs to

    def set_proxy(self, proxy: str | None) -> None:
        """Route every request on this account's session through `proxy`
        (None = direct). Safe to call live; the next request uses it."""
        from proxies import mask, requests_proxies

        self.proxy = proxy or None
        self.session.proxies.clear()
        self.session.proxies.update(requests_proxies(self.proxy))
        # requests lets HTTP(S)_PROXY env vars override session.proxies, so
        # turn env lookup off whenever an explicit proxy is set.
        self.session.trust_env = self.proxy is None
        # Signed playlist URLs are tied to the IP that requested them.
        if hasattr(self, "_stream_url_cache"):
            self._stream_url_cache.clear()
        self._log(f"network: {mask(self.proxy)}")

    def _net_ok(self) -> None:
        self.net_failures = 0

    def _net_fail(self, e: BaseException) -> None:
        if isinstance(e, requests.RequestException):
            from proxies import describe_error
            self.net_failures += 1
            self.last_net_error = describe_error(e) if self.proxy else str(e)

    def _gql_headers(self) -> dict:
        """
        Full per-request header override for GQL calls only. Matches the
        reference's _AuthState.headers(gql=True), which forces Client-Id,
        User-Agent, Origin, Referer and Authorization to the LOGIN client
        (SMARTBOX / config.ACTIVE_CLIENT) regardless of the session's own
        default identity (BROWSE_CLIENT / ANDROID_APP).
        """
        headers = {
            "Client-ID": ANDROID_CLIENT_ID,
            "Authorization": f"OAuth {self.access_token}",
            "User-Agent": ACTIVE_USER_AGENT,
            "Origin": ACTIVE_CLIENT_URL,
            "Referer": ACTIVE_CLIENT_URL + "/",
            "Client-Session-Id": self._client_session_id,
        }
        if self.device_id:
            headers["X-Device-Id"] = self.device_id
        return headers

    def _load_cookies(self) -> None:
        try:
            if self._cookies_path.exists():
                with open(self._cookies_path, "rb") as f:
                    self.session.cookies.update(pickle.load(f))
        except Exception:
            pass  # corrupt/old-format jar — just start fresh

    def save_cookies(self) -> None:
        try:
            _COOKIES_DIR.mkdir(parents=True, exist_ok=True)
            with open(self._cookies_path, "wb") as f:
                pickle.dump(self.session.cookies, f)
        except Exception:
            pass  # best-effort — never let this break mining

    def _bootstrap_device_id(self) -> None:
        """
        Confirmed against the reference client's _validate(): before doing
        anything else, it GETs client_info.CLIENT_URL where
        client_info = self._twitch._client_type — which is BROWSE_CLIENT
        (ANDROID_APP, i.e. https://www.twitch.tv), NOT the login client.
        It reads the `unique_id` cookie Twitch sets in the response and
        keeps it as device_id for the rest of the session — riding along
        via the shared cookie jar on every later request (page scrapes,
        the beacon POST), and also sent explicitly as X-Device-Id on GQL
        calls via _gql_headers().

        An earlier version of this bootstrapped against SMARTBOX's URL
        instead (android.tv.twitch.tv), based on a wrong assumption that
        self._client_type == the login client. It doesn't — confirmed by
        finding `self._client_type: ClientInfo = ClientType.ANDROID_APP`
        hardcoded in core/client.py, completely separate from
        LOGIN_CLIENT (SMARTBOX). Empirically, android.tv.twitch.tv/<login>
        also doesn't even serve the same page markup (no beacon_url /
        settings.js match) — www.twitch.tv is the correct target.

        The "do we already have one" check is domain-aware (matches the
        cookie's actual Domain attribute against the target host) rather
        than a bare session.cookies.get("unique_id"), which doesn't
        distinguish WHICH domain set it and raises CookieConflictError
        once two different domains have both set a same-named cookie.
        """
        existing = self._cookie_for_host("unique_id", _BROWSE_HOST)
        if existing:
            if DEBUG_LOG:
                self._log(f"[diag] device_id already present for {_BROWSE_HOST} from saved cookies: {existing}")
            self.device_id = existing
            return
        # Matches the reference's headers(gql=False): Client-Id set to the
        # BROWSE client's own id, no Authorization/Origin/Referer, and
        # User-Agent left to fall back to the session default (also
        # BROWSE_CLIENT's, so it already matches here).
        headers = {
            "Accept": "*/*",
            "Accept-Encoding": "gzip",
            "Accept-Language": "en-US",
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
            "Client-Id": BROWSE_CLIENT.CLIENT_ID,
            "Client-Session-Id": self._client_session_id,
        }
        try:
            resp = self.session.get(BROWSE_CLIENT.CLIENT_URL, headers=headers, timeout=15)
            resp.raise_for_status()
        except Exception as e:
            self._log(f"device-id bootstrap GET to {BROWSE_CLIENT.CLIENT_URL} failed: {type(e).__name__}: {e}")
            return
        cookie_val = self._cookie_for_host("unique_id", _BROWSE_HOST)
        if cookie_val:
            self.device_id = cookie_val
            if DEBUG_LOG:
                self._log(f"[diag] device_id bootstrapped from {BROWSE_CLIENT.CLIENT_URL}: {cookie_val}")
        else:
            self._log(
                f"{BROWSE_CLIENT.CLIENT_URL} (HTTP {resp.status_code}) did not set a unique_id "
                f"cookie for {_BROWSE_HOST}"
            )

    def _cookie_for_host(self, name: str, host: str) -> str | None:
        """
        Domain-aware cookie lookup: returns the value of the cookie called
        `name` whose Domain attribute matches (or is a parent of) `host`,
        or None. Iterates the jar directly instead of using
        session.cookies.get(name), which ignores domain entirely and
        raises CookieConflictError once two different domains have both
        set a same-named cookie.
        """
        match = None
        for c in self.session.cookies:
            if c.name != name:
                continue
            dom = (c.domain or "").lstrip(".")
            if dom and (host == dom or host.endswith("." + dom)):
                match = c.value
        return match

    def _fetch_user_id(self) -> str | None:
        query = """
        query {
            currentUser {
                id
            }
        }
        """
        try:
            res = self._post({"query": query})
            if isinstance(res, dict):
                return res.get("data", {}).get("currentUser", {}).get("id")
        except Exception:
            pass
        return None

    def _post(self, payload: list | dict) -> list | dict:
        try:
            resp = self.session.post(GQL_URL, json=payload, headers=self._gql_headers(), timeout=15)
            self._net_ok()
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            self._net_fail(e)
            if self.proxy:
                from proxies import describe_error
                raise GQLError(describe_error(e))
            raise GQLError(str(e))

    def _post_persisted(self, operation_name: str, sha256_hash: str, variables: dict) -> dict:
        """
        Many of Twitch's real GQL operations are persisted queries — the
        client sends only an operationName + sha256Hash + variables, never
        the actual query text (which is pre-registered server-side). Ad-hoc
        raw query text for these can be silently accepted but return
        incomplete/empty data instead of erroring, which is what caused
        AvailableDrops to look like "checked and confirmed empty" instead
        of "wrong request shape". These hashes are taken directly from the
        reference client's source (GQL_QUERIES in constants.py), not
        guessed.
        """
        payload = {
            "operationName": operation_name,
            "variables": variables,
            "extensions": {"persistedQuery": {"version": 1, "sha256Hash": sha256_hash}},
        }
        res = self._post(payload)
        if isinstance(res, dict) and res.get("errors"):
            raise GQLError(str(res["errors"]))
        return res if isinstance(res, dict) else {}

    def get_viewer_drop_campaigns(self) -> list[dict]:
        query = """
        query ViewerDrops {
            currentUser {
                dropCampaigns {
                    id
                    name
                    status
                    game { displayName }
                    allow {
                        isEnabled
                        channels { id name }
                    }
                    timeBasedDrops {
                        id
                        name
                        requiredMinutesWatched
                        self {
                            currentMinutesWatched
                            isClaimed
                            dropInstanceID
                        }
                    }
                }
            }
        }
        """
        res = self._post({"query": query})
        if isinstance(res, dict):
            return res.get("data", {}).get("currentUser", {}).get("dropCampaigns") or []
        return []

    def get_drop_images(self) -> dict[str, str]:
        """{drop_id: reward image URL} for every drop Twitch lists for this
        account. Cosmetic only, so it is isolated from the mining queries:
        any failure (including Twitch rejecting a field) returns {} and
        never counts as a network/proxy failure."""
        drop_fields = "id benefitEdges { benefit { id name imageAssetURL } }"
        query = """
        query DropImages {
            currentUser {
                dropCampaigns { id timeBasedDrops { %s } }
                inventory { dropCampaignsInProgress { id timeBasedDrops { %s } } }
            }
        }
        """ % (drop_fields, drop_fields)
        try:
            resp = self.session.post(GQL_URL, json={"query": query}, headers=self._gql_headers(), timeout=15)
            res = resp.json()
        except Exception as e:
            raise GQLError(f"drop images: {e}")
        if not isinstance(res, dict):
            return {}
        user = (res.get("data") or {}).get("currentUser") or {}
        campaigns = list(user.get("dropCampaigns") or [])
        campaigns += (user.get("inventory") or {}).get("dropCampaignsInProgress") or []
        if not campaigns and res.get("errors"):
            raise GQLError(f"drop images: {res['errors']}")
        out: dict[str, str] = {}
        for c in campaigns:
            for drop in c.get("timeBasedDrops") or []:
                for edge in drop.get("benefitEdges") or []:
                    url = ((edge or {}).get("benefit") or {}).get("imageAssetURL")
                    if url and drop.get("id"):
                        out.setdefault(drop["id"], url)
                        break
        return out

    def get_drops_inventory(self) -> list[dict]:
        """
        Separate from get_viewer_drop_campaigns(): the flat campaign list
        is effectively catalog metadata and its "self" field doesn't
        reliably resolve. Actual per-user progress lives under
        currentUser.inventory.dropCampaignsInProgress, which is what
        Twitch's own site queries for the drops-progress UI. Keyed by
        campaign id here so it can be merged into the campaign list.
        """
        query = """
        query Inventory {
            currentUser {
                inventory {
                    dropCampaignsInProgress {
                        id
                        game { displayName }
                        timeBasedDrops {
                            id
                            name
                            requiredMinutesWatched
                            self {
                                currentMinutesWatched
                                isClaimed
                                dropInstanceID
                            }
                        }
                    }
                }
            }
        }
        """
        res = self._post({"query": query})
        if isinstance(res, dict):
            inv = res.get("data", {}).get("currentUser", {}).get("inventory") or {}
            return inv.get("dropCampaignsInProgress") or []
        return []

    def get_stream_playback_access_token(self, channel_login: str) -> dict | None:
        """
        Requests a playback access token for a channel — the same call
        the real Twitch player makes the instant you open a stream. This
        establishes the actual viewing session server-side; sending
        minute-watched heartbeats without ever calling this first means
        there's no session for Twitch to attribute that watch time to,
        which is why the counter can stay flat even while heartbeats are
        being accepted.
        """
        query = """
        query PlaybackAccessToken($login: String!) {
            streamPlaybackAccessToken(
                channelName: $login
                params: {platform: "web", playerBackend: "mediaplayer", playerType: "site"}
            ) {
                value
                signature
            }
        }
        """
        res = self._post({"query": query, "variables": {"login": channel_login}})
        if isinstance(res, dict):
            return res.get("data", {}).get("streamPlaybackAccessToken")
        return None

    def get_stream_info(self, channel_login: str) -> dict:
        """
        Real persisted query (VideoPlayerStreamInfoOverlayChannel) the
        client uses to get authoritative current stream info. Our
        broadcast_id has so far come from a guessed field in an ad-hoc
        directory query, never verified — this is the actual source. We
        log the raw response so the real field shape is visible even if
        our parsing guess below turns out wrong.
        """
        res = self._post_persisted(
            "VideoPlayerStreamInfoOverlayChannel",
            "198492e0857f6aedead9665c81c5a06d67b25b58034649687124083ff288597d",
            {"channel": channel_login},
        )
        return res.get("data", {}) or {}

    def get_live_channels_for_game(self, game_name: str) -> tuple[str | None, list[dict]]:
        """Returns (game_id, channels) — game_id is needed for the real
        watch-minute mutation, not just cosmetic."""
        query = """
        query LiveStreams($name: String!) {
            game(name: $name) {
                id
                name
                streams(first: 30) {
                    edges {
                        node {
                            id
                            viewersCount
                            broadcaster {
                                id
                                login
                                displayName
                            }
                        }
                    }
                }
            }
        }
        """
        res = self._post({"query": query, "variables": {"name": game_name}})
        if isinstance(res, dict):
            game_data = res.get("data", {}).get("game")
            if game_data and game_data.get("streams"):
                edges = game_data["streams"].get("edges", [])
                nodes = [e["node"] for e in edges if e.get("node")]
                if nodes:
                    return game_data.get("id"), nodes

        # Fallback query via category search if direct name match fails
        search_query = """
        query SearchGame($query: String!) {
            searchCategories(query: $query, first: 1) {
                edges {
                    node {
                        id
                        name
                        streams(first: 30) {
                            edges {
                                node {
                                    id
                                    viewersCount
                                    broadcaster {
                                        id
                                        login
                                        displayName
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
        """
        try:
            search_res = self._post({"query": search_query, "variables": {"query": game_name}})
            if isinstance(search_res, dict):
                cat_edges = search_res.get("data", {}).get("searchCategories", {}).get("edges", [])
                if cat_edges and cat_edges[0].get("node"):
                    node = cat_edges[0]["node"]
                    streams = node.get("streams", {}).get("edges", [])
                    return node.get("id"), [s["node"] for s in streams if s.get("node")]
        except Exception:
            pass

        return None, []

    _SETTINGS_PATTERN = re.compile(r'src="(https://[\w.]+/config/settings\.[0-9a-f]{32}\.js)"', re.I)
    _SPADE_PATTERN = re.compile(r'"beacon_?url": ?"(https://[^"]+)"', re.I)

    def _get_spade_url(self, channel_login: str) -> str:
        """
        Confirmed against the currently-working reference client: there is
        no fixed global spade.twitch.tv beacon endpoint anymore. Each
        channel serves its own beacon URL, discovered by walking:
          streamer page HTML --(regex)--> settings.<hash>.js --(regex)--> beacon url
        (mobile-style pages sometimes have the beacon url directly in the
        page HTML, skipping the settings.js hop.) Cached per channel login
        since it doesn't change mid-stream.
        """
        cached = self._spade_url_cache.get(channel_login)
        if cached:
            return cached

        # Use self.session, not a bare/throwaway request — the reference
        # client runs literally every request through ONE session with ONE
        # persistent cookie jar. NO header override here at all: the
        # reference's get_spade_url() passes no headers kwarg, so this
        # gets only the bare session-level default (BROWSE_CLIENT's
        # User-Agent, set in __init__).
        #
        # The page URL is BROWSE_CLIENT.CLIENT_URL/<login> (self.url in
        # the reference — f"{self._twitch._client_type.CLIENT_URL}/{login}",
        # and _client_type is hardcoded to ANDROID_APP, i.e.
        # https://www.twitch.tv), NOT the SMARTBOX login identity's URL.
        # An earlier version of this pointed at SMARTBOX's CLIENT_URL
        # (android.tv.twitch.tv) based on a wrong assumption that
        # _client_type == the login client — confirmed wrong (see
        # _bootstrap_device_id's docstring), and confirmed empirically:
        # android.tv.twitch.tv/<login> doesn't even serve a page with a
        # beacon_url/settings.js reference in it.
        resp = self.session.get(
            f"{BROWSE_CLIENT.CLIENT_URL}/{channel_login}",
            timeout=15,
        )
        resp.raise_for_status()
        html = resp.text
        if DEBUG_LOG:
            self._log(f"[diag] channel page for {channel_login}: HTTP {resp.status_code}, {len(html)} bytes, cookies now: {list(self.session.cookies.keys())}")
        match = self._SPADE_PATTERN.search(html)
        if not match:
            settings_match = self._SETTINGS_PATTERN.search(html)
            if not settings_match:
                self._log(f"no beacon_url or settings.js reference found in page HTML for {channel_login} — Twitch may have changed the page markup")
                raise GQLError(f"couldn't find beacon url for {channel_login} (step 1: no settings.js reference)")
            if DEBUG_LOG:
                self._log(f"[diag] beacon_url not directly in page HTML, following settings.js: {settings_match.group(1)}")
            settings_resp = self.session.get(settings_match.group(1), timeout=15)
            settings_resp.raise_for_status()
            match = self._SPADE_PATTERN.search(settings_resp.text)
            if not match:
                self._log(f"settings.js fetched ({len(settings_resp.text)} bytes) but no beacon_url pattern matched inside it")
                raise GQLError(f"couldn't find beacon url for {channel_login} (step 2: not in settings.js)")

        url = match.group(1)
        if DEBUG_LOG:
            self._log(f"[diag] resolved beacon url for {channel_login}: {url}")
        self._spade_url_cache[channel_login] = url
        return url

    def send_watch_minute(
        self,
        *,
        broadcast_id: str,
        channel_id: str,
        channel_login: str,
        game_name: str,
        game_id: str | None,
    ) -> bool:
        """
        This is the call that actually credits watched minutes. Confirmed
        against the currently-working reference client's own send_watch():
        it is a raw HTTP POST of a minified + base64 (NOT gzipped) JSON
        payload, as form data, to the per-channel beacon URL discovered by
        _get_spade_url() — not a sendSpadeEvents GQL mutation, and not a
        fixed spade.twitch.tv/track URL. Success is a bare HTTP 204, not a
        GQL response body.
        """
        try:
            spade_url = self._get_spade_url(channel_login)
        except Exception as e:
            # Beacon is telemetry only — never let it break a watch cycle.
            self._net_fail(e)
            if not isinstance(e, GQLError) and self.proxy:
                from proxies import describe_error
                e = describe_error(e)
            self._log(f"beacon skipped (non-fatal): {e}")
            return False

        if not self.user_id:
            self._log("WARNING: user_id is empty — heartbeat will send user_id=null, which likely won't get credited")

        payload = [
            {
                "event": "minute-watched",
                "properties": {
                    "broadcast_id": str(broadcast_id),
                    "channel_id": str(channel_id),
                    "channel": channel_login,
                    "client_time": _isonow(),
                    "game": game_name or "",
                    "game_id": str(game_id) if game_id else "",
                    "hidden": False,
                    "is_live": True,
                    "live": True,
                    "location": "channel",
                    "logged_in": True,
                    "minutes_logged": 1,
                    "muted": False,
                    "player": "site",
                    "user_id": int(self.user_id) if self.user_id else None,
                },
            }
        ]
        minified = json.dumps(payload, separators=(",", ":"))
        data_blob = base64.b64encode(minified.encode("utf-8")).decode("utf-8")

        try:
            # self.session again, same reasoning as _get_spade_url: keep
            # the one persistent cookie jar flowing, and NO header
            # override — the reference's send_watch() passes zero custom
            # headers to this call, only the bare session-level default
            # (BROWSE_CLIENT's User-Agent). requests' `data=` dict sends
            # application/x-www-form-urlencoded, same as the reference
            # client's aiohttp `data=` usage.
            resp = self.session.post(
                spade_url,
                data={"data": data_blob},
                timeout=15,
            )
            ok = resp.status_code == 204
            if ok:
                if DEBUG_LOG:
                    self._log(f"[diag] beacon POST to {spade_url}: HTTP {resp.status_code}")
                self.save_cookies()
            else:
                body_preview = (resp.text or "")[:200]
                self._log(
                    f"beacon POST to {channel_login} failed: HTTP {resp.status_code}"
                    + (f", body: {body_preview!r}" if body_preview else "")
                )
            return ok
        except Exception as e:
            self._net_fail(e)
            self._log(f"beacon POST raised {type(e).__name__}: {e}")
            return False

    # ------------------------------------------------------------------
    # HLS segment watching — the part that actually earns watch time now.
    #
    # Ported from SimpliAj's TwitchDropsMiner fork (src/models/channel.py
    # _watch_playlist / Stream.get_stream_url, and watch_service.py's loop):
    # Twitch credits watch time (drops, channel points) only for real
    # segment traffic — the Spade minute-watched event alone no longer
    # advances anything.
    #
    #   1. PlaybackAccessToken (persisted GQL) -> value + signature
    #   2. GET usher master .m3u8 with sig/token -> quality variants
    #   3. Take the LAST variant (audio_only / lowest bandwidth)
    #   4. Every ~10s: GET that media playlist, HEAD every segment URL not
    #      seen before. HEAD = no audio/video body is downloaded, so a poll
    #      costs a few KB — fine for a very old machine.
    #   10s fits inside Twitch's rolling playlist window (~2s segments,
    #   ~6 per playlist), so no segment is skipped.
    # ------------------------------------------------------------------

    _PLAYBACK_TOKEN_HASH = "ed230aa1e33e07eebb8928504583da78a5173989fadfb1ac94be06a04f3cdbe9"
    _CURRENT_DROP_HASH = "4d06b702d25d652afb9ef835d2a550031f1cf762b193523a92166f40ea3d142b"

    def _playback_token(self, channel_login: str) -> dict | None:
        try:
            res = self._post_persisted(
                "PlaybackAccessToken",
                self._PLAYBACK_TOKEN_HASH,
                {
                    "isLive": True,
                    "isVod": False,
                    "login": channel_login,
                    "platform": "web",
                    "playerType": "site",
                    "vodID": "",
                },
            )
            tok = (res.get("data") or {}).get("streamPlaybackAccessToken")
            if isinstance(tok, dict) and tok.get("value") and tok.get("signature"):
                return tok
        except GQLError as e:
            self._log(f"persisted PlaybackAccessToken failed ({e}), trying raw query")
        try:
            tok = self.get_stream_playback_access_token(channel_login)
            if isinstance(tok, dict) and tok.get("value") and tok.get("signature"):
                return tok
        except GQLError as e:
            self._log(f"raw PlaybackAccessToken failed: {e}")
        return None

    @staticmethod
    def _playlist_urls(playlist: str, base: str, *, master: bool) -> list[str]:
        """URI lines that follow #EXT-X-STREAM-INF (master) or #EXTINF (media)."""
        if not playlist.lstrip().startswith("#EXTM3U"):
            return []
        tag = "#EXT-X-STREAM-INF:" if master else "#EXTINF:"
        urls: list[str] = []
        pending = False
        for raw in playlist.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#"):
                if line.startswith(tag):
                    pending = True
                continue
            if pending:
                url = urljoin(base, line)
                if url.startswith(("http://", "https://")):
                    urls.append(url)
                pending = False
        return urls

    def _get_media_playlist_url(self, channel_login: str, broadcast_id: str) -> str | None:
        cached = self._stream_url_cache.get(channel_login)
        if cached and cached[0] == str(broadcast_id):
            return cached[1]
        tok = self._playback_token(channel_login)
        if tok is None:
            self._log(f"no playback token for {channel_login}")
            return None
        resp = self.session.get(
            f"https://usher.ttvnw.net/api/channel/hls/{channel_login}.m3u8",
            params={"sig": tok["signature"], "token": tok["value"]},
            timeout=10,
        )
        if resp.status_code != 200:
            self._log(f"usher master playlist for {channel_login}: HTTP {resp.status_code}")
            if resp.status_code == 403 and self.proxy:
                # Fresh token + 403 = Twitch won't serve video to this IP
                # (flagged proxy/VPN or geo-block). Everything else through
                # the proxy may still "work", so count it as proxy trouble
                # explicitly — otherwise it never fails over and earns 0.
                self.net_failures += 1
                self.last_net_error = "Twitch refused the stream through this proxy (HTTP 403)"
            return None
        variants = self._playlist_urls(resp.text, resp.url, master=True)
        if not variants:
            self._log(f"usher master playlist for {channel_login} had no variants")
            return None
        url = variants[-1]  # audio_only / lowest bandwidth
        self._stream_url_cache[channel_login] = (str(broadcast_id), url)
        if DEBUG_LOG:
            self._log(f"[diag] media playlist for {channel_login}: {len(variants)} variants, using lowest")
        return url

    def poll_segments(self, channel_login: str, broadcast_id: str) -> bool:
        """
        One watch tick: fetch the media playlist and HEAD every new segment.
        True if the playlist loaded and every new segment answered 200.
        Call every ~10s while watching.
        """
        bid = str(broadcast_id)
        if self._seen_broadcast.get(channel_login) != bid:
            self._seen_broadcast[channel_login] = bid
            self._seen_segments[channel_login] = OrderedDict()
        seen = self._seen_segments[channel_login]
        try:
            url = self._get_media_playlist_url(channel_login, bid)
            if url is None:
                return False
            resp = self.session.get(url, headers={"Connection": "close"}, timeout=5)
            self._net_ok()
            if resp.status_code != 200:
                if resp.status_code in (401, 403, 404):
                    self._stream_url_cache.pop(channel_login, None)  # token expired -> refetch next tick
                self._log(f"media playlist for {channel_login}: HTTP {resp.status_code}")
                return False
            segments = self._playlist_urls(resp.text, url, master=False)
            if not segments:
                return False
            ok = True
            new = 0
            for seg in segments:
                if seg in seen:
                    continue
                try:
                    r = self.session.head(seg, timeout=3)
                except requests.RequestException:
                    ok = False
                    continue
                if r.status_code == 200:
                    seen[seg] = None
                    new += 1
                    if len(seen) > 256:
                        seen.popitem(last=False)
                else:
                    ok = False
                    if r.status_code in (401, 403, 404):
                        self._stream_url_cache.pop(channel_login, None)
            if DEBUG_LOG:
                self._log(f"[diag] segments {channel_login}: {new} new HEAD'd, ok={ok}")
            return ok
        except requests.RequestException as e:
            self._net_fail(e)
            self._log(f"segment poll for {channel_login} failed: {type(e).__name__}: {e}")
            return False

    def get_current_drop(self, channel_id: str) -> dict | None:
        """
        DropCurrentSessionContext — SimpliAj's progress source when the
        websocket is quiet. Returns {"dropID", "currentMinutesWatched", ...}
        or None.
        """
        try:
            res = self._post_persisted(
                "DropCurrentSessionContext",
                self._CURRENT_DROP_HASH,
                {"channelID": str(channel_id), "channelLogin": ""},
            )
        except GQLError:
            return None
        user = (res.get("data") or {}).get("currentUser")
        if not user:
            return None
        return user.get("dropCurrentSession")

    def get_available_drop_campaigns(self, channel_id: str) -> set[str]:
        """
        Returns the set of campaign IDs currently drop-eligible on this
        SPECIFIC channel right now. This is a persisted-query-only
        operation on Twitch's end (DropsHighlightService_AvailableDrops) —
        sending it as raw query text, as we did before, was accepted but
        returned empty results regardless of the channel's real state.
        """
        res = self._post_persisted(
            "DropsHighlightService_AvailableDrops",
            "782dad0f032942260171d2d80a654f88bdd0c5a9dddc392e9bc92218a0f42d20",
            {"channelID": str(channel_id)},
        )
        channel = res.get("data", {}).get("channel") or {}
        campaigns = channel.get("viewerDropCampaigns") or []
        return {c.get("id") for c in campaigns if c.get("id")}

    def claim_drop(self, drop_instance_id: str) -> bool:
        res = self._post_persisted(
            "DropsPage_ClaimDropRewards",
            "a455deea71bdc9015b78eb49f4acfbce8baa7ccbedd28e549bb025bd0f751930",
            {"input": {"dropInstanceID": drop_instance_id}},
        )
        status = res.get("data", {}).get("claimDropRewards", {}).get("status")
        return status in ("ELIGIBLE_FOR_ALL", "DROP_INSTANCE_ALREADY_CLAIMED")