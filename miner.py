"""
Core watch loop and progress tracker for drops mining.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from config import DEBUG_LOG
from gql import GQLClient, GQLError
from pubsub import PubSubClient

logger = logging.getLogger(__name__)


@dataclass
class DropProgress:
    drop_id: str
    drop_name: str
    game_name: str
    current_minutes: int
    required_minutes: int
    is_claimed: bool = False
    campaign_id: str = ""
    # Needed to claim. Twitch only issues one once progress has started,
    # so not-started drops will have this empty.
    drop_instance_id: str = ""
    # Reward picture from Twitch ("" when unknown). Display only.
    image_url: str = ""

    @property
    def is_ready(self) -> bool:
        return (
            not self.is_claimed
            and self.required_minutes > 0
            and self.current_minutes >= self.required_minutes
        )

    @property
    def percentage(self) -> float:
        if self.required_minutes <= 0:
            return 0.0
        return min(100.0, (self.current_minutes / self.required_minutes) * 100)


@dataclass
class AccountNodeState:
    account_name: str = ""
    status: str = "Idle"
    connected: bool = False
    current_game: str = ""
    selected_game: str = ""
    # Ordered target-game list for this node (set in the UI, saved in
    # settings.json). The miner works through it; see Miner._rotate_game.
    game_list: list[str] = field(default_factory=list)
    # When the node started on selected_game, and minutes Twitch has
    # credited for it since then.
    game_since: float = 0.0
    game_credited: int = 0
    current_channel: str = ""
    # Manual override: when set (via the Live Streamers list in the UI),
    # the miner watches this exact channel login directly instead of
    # auto-picking one — skips the game search, ACL restriction check, and
    # per-channel AvailableDrops eligibility check entirely.
    manual_channel: str = ""
    # True when manual_channel was set automatically to follow a running
    # peer node (this node had nothing of its own to watch). Cleared when
    # the user picks a channel, or periodically to retry its own drops.
    following: bool = False
    drops: list[DropProgress] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    last_error: str = ""
    # Live channels currently streaming the selected game, refreshed by
    # the UI (background thread) so the manual-channel picker can offer a
    # real list instead of requiring the streamer's login typed blind.
    # Each entry: {"login": str, "display_name": str, "viewers": int}.
    live_streamers: list[dict] = field(default_factory=list)
    live_streamers_game: str = ""
    live_streamers_loading: bool = False
    # Smooth-progress tracking: the confirmed minute count for the active
    # drop as of the last real inventory refresh, plus how many heartbeats
    # have landed since then. The UI interpolates between real refreshes
    # (every campaign_refresh_interval, default 5 min) using these so the
    # progress bar visibly creeps every second instead of jumping in
    # 5-minute steps.
    watch_baseline_drop_id: str = ""
    watch_baseline_minutes: int = 0
    heartbeats_since_baseline: int = 0
    last_heartbeat_time: float = 0.0
    # Proxy this account's traffic goes through ("" = direct).
    proxy: str = ""
    # Set by MiningNode.set_proxy so the Miner drops its current channel
    # and reconnects through the new route.
    reconnect_requested: bool = False
    # Credit tracking — "is Twitch actually counting it?"
    # credit_samples: (timestamp, watched_seconds, credited_minutes) per cycle
    credit_samples: list = field(default_factory=list)
    proxy_swaps: int = 0
    # Since the current proxy was put in place:
    route_watched_s: float = 0.0
    route_credited: int = 0
    route_session_seen: bool = False  # CurrentDrop reported a drop session

    def credit_last_hour(self) -> tuple[float, int]:
        """(minutes watched with segments accepted, minutes Twitch credited) in the last 60 min."""
        cutoff = time.time() - 3600
        self.credit_samples = [x for x in self.credit_samples if x[0] >= cutoff]
        watched = sum(x[1] for x in self.credit_samples) / 60
        credited = sum(x[2] for x in self.credit_samples)
        return watched, credited

    @property
    def active_drop(self) -> Optional[DropProgress]:
        game = self.selected_game or self.current_game
        candidates = self.drops
        if game:
            game_drops = [d for d in candidates if d.game_name == game]
            if game_drops:
                candidates = game_drops
        for d in candidates:
            if not d.is_claimed and d.current_minutes < d.required_minutes:
                return d
        return None


class MiningNode:
    def __init__(self, account_name: str, access_token: str, proxy: str | None = None):
        self.account_name = account_name
        self.state = AccountNodeState(account_name=account_name)
        self.state.proxy = proxy or ""
        self.gql = GQLClient(access_token, account_name=account_name, on_log=self._log, proxy=proxy)
        self._stop_requested = False
        self.pubsub = PubSubClient(
            access_token=access_token,
            on_message=self._on_pubsub_message,
            on_log=self._log,
            proxy=proxy,
        )
        self._pubsub_started = False
        # Called as on_proxy_trouble(node, reason) from the miner thread when
        # this account's proxy keeps failing; the app swaps in another
        # working proxy. None = no failover (direct, or not wired up).
        self.on_proxy_trouble = None
        # Called as peer_provider(node) -> channel login of a running peer
        # (or None). Set by the app; used when this node goes idle.
        self.peer_provider = None
        # Set from Settings.auto_claim by Miner. When False, nothing is
        # claimed unless the user presses a Claim button.
        self.auto_claim = True
        # game_name -> {"acl_logins": set[str], "acl_enabled": bool}
        # populated from each campaign's `allow` field on refresh_drops().
        # Many drop campaigns only credit progress on specific opted-in
        # channels — watching a random channel playing the right game
        # doesn't count if that channel isn't on this list.
        self.game_acl: dict[str, dict] = {}
        # drop_id -> reward image URL ("" = looked up, none found).
        self._drop_images: dict[str, str] = {}
        self._drop_images_off = False
        # Only the desktop app shows pictures; headless leaves this off.
        self.want_images = False

    def _attach_drop_images(self, drops: list) -> None:
        """Fill DropProgress.image_url. One extra query only when a drop we
        haven't looked up yet appears; switched off for the session if
        Twitch rejects it. Never affects mining."""
        if not self.want_images:
            return
        missing = [d for d in drops if d.drop_id not in self._drop_images]
        if missing and not self._drop_images_off:
            try:
                found = self.gql.get_drop_images()
                self._drop_images.update(found)
                if DEBUG_LOG:
                    self._log(f"drop images: {len(found)} found")
            except Exception as e:
                self._drop_images_off = True
                self._log(f"drop images unavailable ({e}) — continuing without pictures")
            for d in missing:
                self._drop_images.setdefault(d.drop_id, "")
        for d in drops:
            d.image_url = self._drop_images.get(d.drop_id, "")

    def stop(self):
        self._stop_requested = True
        self.pubsub.stop()

    # Consecutive network failures (timeouts/refused/proxy errors, any
    # request) before swapping to another working proxy. 2 = swap fast but
    # not on a single blip.
    PROXY_FAILURE_LIMIT = 2

    def check_proxy_health(self) -> None:
        g = self.gql
        if g.proxy and g.net_failures >= self.PROXY_FAILURE_LIMIT and self.on_proxy_trouble:
            reason = g.last_net_error or "repeated network failures"
            g.net_failures = 0
            try:
                self.on_proxy_trouble(self, reason)
            except Exception as e:
                self._log(f"proxy failover failed: {e}")

    # A proxy that keeps segments flowing but gets no minutes credited while
    # Twitch reports an active drop session is treated like a failed proxy.
    NO_CREDIT_SWAP_MINUTES = 15

    def check_route_crediting(self) -> None:
        st = self.state
        if not self.gql.proxy or not st.route_session_seen:
            return
        if st.active_drop is None:
            # Nothing to mine, so zero credit is expected — not the proxy's fault.
            st.route_watched_s = 0.0
            return
        if st.route_credited == 0 and st.route_watched_s >= self.NO_CREDIT_SWAP_MINUTES * 60 \
                and self.on_proxy_trouble:
            mins = int(st.route_watched_s // 60)
            st.route_watched_s = 0.0  # don't re-fire before the swap lands
            self._log(f"{mins} min watched through this proxy with 0 min credited "
                      "while a drop session is active — Twitch isn't counting it")
            self.on_proxy_trouble(self, "not crediting watch time")

    def set_proxy(self, proxy: str | None) -> None:
        """Move this account onto a different proxy (None = direct), live.
        GQL/HLS/beacon switch on their next request; PubSub reconnects."""
        if (proxy or "") != self.state.proxy:
            self.state.route_watched_s = 0.0
            self.state.route_credited = 0
            self.state.route_session_seen = False
        self.state.proxy = proxy or ""
        self.gql.set_proxy(proxy)
        self.pubsub.set_proxy(proxy)
        # Signed stream URLs were minted for the old IP — force a re-pick
        # on the next loop so the playback token is fetched via the new one.
        self.state.reconnect_requested = True

    def ensure_presence(self) -> None:
        """Start the PubSub connection and subscribe to our drops topic,
        once. Safe to call every loop iteration — it no-ops after the
        first successful start."""
        if self._pubsub_started:
            return
        self.pubsub.start()
        if self.gql.user_id:
            self.pubsub.add_topic(f"user-drop-events.{self.gql.user_id}")
            self._log(f"pubsub: subscribing to drop events for user {self.gql.user_id}")
        else:
            self._log("pubsub: no user_id available yet, skipping drop-events subscription")
        self._pubsub_started = True

    def _on_pubsub_message(self, topic: str, message: dict) -> None:
        # Confirmed against DevilXD/TwitchDropsMiner: the real client
        # claims a completed drop the instant Twitch pushes a
        # "drop-claim" event on this websocket topic — it doesn't wait
        # for the next poll. The periodic check in claim_ready_drops()
        # (run every campaign_refresh_interval) stays as a fallback for
        # if this event is ever missed.
        msg_type = message.get("type", "")
        if msg_type == "drop-claim":
            data = message.get("data") or {}
            drop_instance_id = data.get("drop_instance_id")
            if drop_instance_id and not self.auto_claim:
                self._log("pubsub: a drop is ready to claim — use a Claim button (auto-claim is off)")
                return
            if drop_instance_id:
                self._log("pubsub: drop ready to claim — claiming now")
                try:
                    ok = self.gql.claim_drop(drop_instance_id)
                    self._log("claimed via pubsub" if ok else "pubsub claim attempt did not confirm success")
                except GQLError as e:
                    self._log(f"pubsub-triggered claim failed: {e}")
                return
        if DEBUG_LOG:
            self._log(f"pubsub message on {topic}: {msg_type or message}")

    def _log(self, msg: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.state.logs.append(f"[{stamp}] {msg}")
        self.state.logs = self.state.logs[-200:]
        logger.info(f"[{self.account_name}] {msg}")
        self._log_to_file(f"{time.strftime('%Y-%m-%d')} {stamp} {msg}")

    def _log_to_file(self, line: str) -> None:
        """data/logs/<account>.log, ~1 MB then rotated to .1 — so a session
        can be reviewed after the fact."""
        try:
            from config import DATA_DIR
            d = DATA_DIR / "logs"
            d.mkdir(parents=True, exist_ok=True)
            f = d / f"{self.account_name}.log"
            if f.exists() and f.stat().st_size > 1_000_000:
                f.replace(d / f"{self.account_name}.log.1")
            with open(f, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass

    def refresh_drops(self):
        try:
            campaigns = self.gql.get_viewer_drop_campaigns()
            parsed_drops: list[DropProgress] = []
            game_acl: dict[str, dict] = {}
            for c in campaigns:
                game_name = c.get("game", {}).get("displayName", "Unknown")
                allow = c.get("allow") or {}
                acl_channels = allow.get("channels") or []
                acl_logins = {ch.get("name", "").lower() for ch in acl_channels if ch.get("name")}
                acl_enabled = bool(allow.get("isEnabled")) and bool(acl_logins)
                entry = game_acl.setdefault(game_name, {"acl_logins": set(), "acl_enabled": False})
                if acl_enabled:
                    entry["acl_logins"] |= acl_logins
                    entry["acl_enabled"] = True

                for drop in c.get("timeBasedDrops", []):
                    self_data = drop.get("self") or {}
                    parsed_drops.append(
                        DropProgress(
                            drop_id=drop.get("id", ""),
                            drop_name=drop.get("name", "Unnamed Drop"),
                            game_name=game_name,
                            current_minutes=self_data.get("currentMinutesWatched", 0),
                            required_minutes=drop.get("requiredMinutesWatched", 0),
                            is_claimed=self_data.get("isClaimed", False),
                            campaign_id=c.get("id", ""),
                            drop_instance_id=self_data.get("dropInstanceID") or "",
                        )
                    )
            self.game_acl = game_acl
            for game_name, entry in game_acl.items():
                if entry["acl_enabled"]:
                    sample = ", ".join(sorted(entry["acl_logins"])[:8])
                    self._log(f"ACL restriction for {game_name}: only {len(entry['acl_logins'])} channel(s) count — {sample}")

            # The flat campaign list above doesn't reliably resolve "self"
            # progress. Overlay real progress from the inventory endpoint,
            # which is what actually tracks in-progress campaigns.
            by_drop_id = {d.drop_id: d for d in parsed_drops}
            try:
                in_progress = self.gql.get_drops_inventory()
                overlaid = 0
                for c in in_progress:
                    for drop in c.get("timeBasedDrops", []):
                        self_data = drop.get("self") or {}
                        drop_id = drop.get("id", "")
                        target = by_drop_id.get(drop_id)
                        if target is None:
                            # Campaign wasn't in the flat list for some reason —
                            # still worth showing since it has real progress.
                            game_name = c.get("game", {}).get("displayName", "Unknown")
                            target = DropProgress(
                                drop_id=drop_id,
                                drop_name=drop.get("name", "Unnamed Drop"),
                                game_name=game_name,
                                current_minutes=0,
                                required_minutes=drop.get("requiredMinutesWatched", 0),
                                campaign_id=c.get("id", ""),
                            )
                            parsed_drops.append(target)
                            by_drop_id[drop_id] = target
                        target.current_minutes = self_data.get("currentMinutesWatched", target.current_minutes)
                        target.is_claimed = self_data.get("isClaimed", target.is_claimed)
                        target.drop_instance_id = self_data.get("dropInstanceID") or target.drop_instance_id
                        overlaid += 1
                if overlaid:
                    self._log(f"inventory overlay applied real progress to {overlaid} drop(s)")
            except GQLError as e:
                self._log(f"inventory fetch failed (progress may be stale): {e}")

            self._attach_drop_images(parsed_drops)
            self.state.drops = parsed_drops
            self.state.connected = True
            self.state.last_error = ""  # clear stale errors on success

            # Re-baseline the smooth-progress estimate against the real,
            # just-confirmed number for whichever drop is now active.
            active = self.state.active_drop
            if active:
                self.state.watch_baseline_drop_id = active.drop_id
                self.state.watch_baseline_minutes = active.current_minutes
            else:
                self.state.watch_baseline_drop_id = ""
                self.state.watch_baseline_minutes = 0
            self.state.heartbeats_since_baseline = 0

            # Diagnostic: surface exactly what Twitch reported for anything
            # with nonzero progress, so a "my progress isn't showing" report
            # can be checked against real API output instead of guessed at.
            if DEBUG_LOG:
                with_progress = [d for d in parsed_drops if d.current_minutes > 0]
                self._log(f"refreshed {len(parsed_drops)} drops, {len(campaigns)} campaigns")
                if with_progress:
                    for d in with_progress[:10]:
                        self._log(f"  progress seen: {d.game_name} — {d.drop_name}: {d.current_minutes}/{d.required_minutes} min")
                else:
                    self._log("  still no nonzero progress after inventory overlay")

        except GQLError as e:
            self.state.last_error = str(e)
            self._log(f"refresh failed: {e}")

    def claim(self, drop: DropProgress) -> bool:
        """Claim one specific drop (used by the UI's Claim Selected button)."""
        if drop.is_claimed:
            self._log(f"{drop.drop_name}: already claimed")
            return True
        if not drop.is_ready:
            self._log(
                f"{drop.drop_name}: not finished yet "
                f"({drop.current_minutes}/{drop.required_minutes} min) — can't claim"
            )
            return False
        if not drop.drop_instance_id:
            self._log(f"{drop.drop_name}: no drop instance id from Twitch yet — refresh and try again")
            return False

        try:
            ok = self.gql.claim_drop(drop.drop_instance_id)
        except GQLError as e:
            self._log(f"claim call failed for {drop.drop_name}: {e}")
            return False
        self._log(f"claimed {drop.drop_name}" if ok else f"claim for {drop.drop_name} did not confirm success")
        if ok:
            drop.is_claimed = True
        return ok

    def claim_ready_drops(self) -> int:
        """
        Fallback poll for anything the pubsub "drop-claim" event missed.
        Uses the inventory endpoint (dropCampaignsInProgress), not the
        flat currentUser.dropCampaigns list — the flat list's "self"
        field doesn't reliably resolve currentMinutesWatched (same
        reason refresh_drops() overlays inventory data on top of it), so
        checking it for claim-readiness could just never fire.
        Returns how many drops were successfully claimed.
        """
        claimed = 0
        seen: set[str] = set()
        # 1) Claim exactly what this node already shows as ready — the same
        #    rows the UI marks "Ready to claim". They carry the
        #    drop_instance_id resolved by the inventory overlay in
        #    refresh_drops(), so this works even for a finished campaign that
        #    has since dropped out of dropCampaignsInProgress.
        for d in list(self.state.drops):
            if d.is_ready and d.drop_instance_id:
                seen.add(d.drop_instance_id)
                if self.claim(d):
                    claimed += 1
        # 2) Inventory sweep as a fallback: catch anything completed since the
        #    last refresh that isn't represented above.
        try:
            in_progress = self.gql.get_drops_inventory()
            for c in in_progress:
                for drop in c.get("timeBasedDrops", []):
                    self_data = drop.get("self") or {}
                    drop_instance_id = self_data.get("dropInstanceID")
                    current = self_data.get("currentMinutesWatched", 0)
                    required = drop.get("requiredMinutesWatched", 0)
                    is_claimed = self_data.get("isClaimed", False)
                    if (drop_instance_id and drop_instance_id not in seen
                            and required > 0 and current >= required and not is_claimed):
                        self._log(f"claiming completed drop: {drop.get('name')}")
                        try:
                            if self.gql.claim_drop(drop_instance_id):
                                claimed += 1
                        except GQLError as e:
                            self._log(f"claim call failed for {drop.get('name')}: {e}")
        except GQLError as e:
            self._log(f"failed to check for claimable drops: {e}")
        return claimed


class Miner:
    def __init__(self, node: MiningNode, settings=None):
        self.node = node
        # settings is optional; falls back to sane defaults if not passed.
        # The real client's WATCH_INTERVAL is 59s, not 20s — matching that.
        self.watch_interval = getattr(settings, "watch_interval", 59) if settings else 59
        self.campaign_refresh_interval = getattr(settings, "campaign_refresh_interval", 300) if settings else 300
        self.node.auto_claim = bool(getattr(settings, "auto_claim", True)) if settings else True
        # Only watch channels that are live in the target game (see config.py).
        self.strict_game = bool(getattr(settings, "strict_game", True)) if settings else True
        self.game_swap_seconds = 60 * (int(getattr(settings, "game_swap_minutes", 60)) if settings else 60)
        self._dwell_game = ""
        self._watching_channel: dict | None = None  # {"login","id","broadcast_id"}
        self._watching_game_id: str | None = None
        self._watching_game: str | None = None
        # Consecutive watch cycles where no segment poll succeeded. Only
        # re-pick the channel after a couple in a row, so one flaky poll
        # doesn't throw away a good channel.
        self._failed_cycles = 0

    # Segment poll cadence — SimpliAj's watch_service: "A ten-second poll
    # fits inside Twitch's rolling segment window."
    SEGMENT_POLL_SECONDS = 10
    MAX_FAILED_CYCLES = 2

    # How long a node may sit with nothing eligible live before following a
    # peer, and how often a follower drops back to retry its own drops.
    FOLLOW_AFTER_STALL = 120
    FOLLOW_RETRY_OWN = 600

    def _rotate_game(self) -> None:
        """Work through the node's target-game list.

        A game is held for at least game_swap_seconds (default 1 hour) before
        the node may move on: Twitch only reports a drop's progress once it
        has some, so less time than that can't tell "no drops here" from
        "not counted yet". After the hold, the node stays while the game is
        still earning and swaps to the next game in the list when it isn't
        (all drops done, nothing credited, or nothing live)."""
        st = self.node.state
        games = [g for g in st.game_list if g]
        now = time.time()
        if games and st.selected_game not in games:
            st.selected_game = games[0]
            self.node._log(f"game list: starting on {games[0]}")
        if st.selected_game != self._dwell_game:
            # New game (list start, rotation, or the user picked one): restart the clock.
            self._dwell_game = st.selected_game
            st.game_since = now
            st.game_credited = 0
            self._credited_mark = (now, 0)
            return
        if len(games) < 2 or now - st.game_since < self.game_swap_seconds:
            return
        cur = st.selected_game
        unfinished = any(d.game_name == cur and not d.is_claimed and d.current_minutes < d.required_minutes
                         for d in st.drops)
        # Earning = credited minutes went up within the last hold window.
        mark_t, mark_c = getattr(self, "_credited_mark", (st.game_since, 0))
        if st.game_credited > mark_c:
            self._credited_mark = (now, st.game_credited)
            mark_t = now
        earning = now - mark_t < self.game_swap_seconds
        if unfinished and earning:
            return

        def finished(game: str) -> bool:
            ds = [d for d in st.drops if d.game_name == game]
            return bool(ds) and all(d.is_claimed or d.current_minutes >= d.required_minutes for d in ds)

        i = games.index(cur)
        # Next game in order that isn't already known to be fully done.
        nxt = next((g for g in games[i + 1:] + games[:i] if not finished(g)), None)
        if nxt is None:
            return  # nowhere better to go
        mins = int((now - st.game_since) // 60)
        why = "all its drops are done" if not unfinished else "no minutes credited in the last hour"
        self.node._log(f"game list: {mins} min on {cur}, {why} — swapping to {nxt}")
        st.selected_game = nxt

    @staticmethod
    def _stream_and_games(info: dict) -> tuple[dict, set[str]]:
        """(stream dict or {}, lower-cased names of the game being streamed).
        The game may sit on stream.game or on user.broadcastSettings.game
        depending on the query shape, so both are read."""
        user = info.get("user") or {}
        stream = user.get("stream") or info.get("stream") or {}
        names: set[str] = set()
        for g in (stream.get("game"), (user.get("broadcastSettings") or {}).get("game")):
            if isinstance(g, dict):
                for k in ("displayName", "name"):
                    if g.get(k):
                        names.add(str(g[k]).strip().lower())
        return stream, names

    def _off_target_reason(self, target_game: str) -> str | None:
        """Strict mode: why the current channel must be dropped, or None to
        stay. Checks the ORIGINAL channel only — a raid/host is never
        followed, the node just leaves when the stream ends."""
        ch = self._watching_channel
        if not ch or not self.strict_game:
            return None
        try:
            info = self.node.gql.get_stream_info(ch["login"])
        except GQLError as e:
            if DEBUG_LOG:
                self.node._log(f"target-game check skipped ({e})")
            return None
        stream, games = self._stream_and_games(info)
        if not stream.get("id"):
            return "stream ended"
        if ch.get("game_names_ok") and games and target_game.strip().lower() not in games:
            return f"streamer switched to {sorted(games)[0]}"
        if stream["id"] != ch.get("broadcast_id"):
            ch["broadcast_id"] = stream["id"]  # same channel, new broadcast
        return None

    def _maybe_follow_peer(self) -> None:
        st = self.node.state
        provider = self.node.peer_provider
        if provider is None:
            return
        now = time.time()
        has_own = any(not d.is_claimed and d.current_minutes < d.required_minutes for d in st.drops)

        if st.following:
            # Periodically stop following to see if our own drops are watchable now.
            if has_own and now - getattr(self, "_follow_started", now) >= self.FOLLOW_RETRY_OWN:
                st.following = False
                st.manual_channel = ""
                self._stall_since = None
                self.node._log("follow: retrying this node's own drops")
                return
            # Track the peer if it moved to another channel.
            peer = provider(self.node)
            if peer and peer != st.manual_channel:
                self.node._log(f"follow: peer moved — now following {peer}")
                st.manual_channel = peer
            elif not peer:
                st.following = False
                st.manual_channel = ""
                self.node._log("follow: no running peer left — back to own selection")
            return

        if st.manual_channel:
            return  # user's own override wins

        stalled = st.status.startswith("No ") or st.status.startswith("Idle")
        if stalled:
            if getattr(self, "_stall_since", None) is None:
                self._stall_since = now
        else:
            self._stall_since = None

        if now < getattr(self, "_follow_block_until", 0):
            return  # strict mode just refused an off-game peer; don't loop on it

        nothing_to_do = not has_own
        stuck = self._stall_since is not None and now - self._stall_since >= self.FOLLOW_AFTER_STALL
        if not (nothing_to_do or stuck):
            return
        peer = provider(self.node)
        if not peer:
            return
        st.manual_channel = peer
        st.following = True
        self._follow_started = now
        why = "no unfinished drops" if nothing_to_do else "nothing eligible live for its drops"
        self.node._log(f"follow: {why} — following running node's channel {peer}")

    def _sync_progress_from_gql(self) -> None:
        """Pull real minutes for the drop being mined (DropCurrentSessionContext)
        and re-baseline the UI estimator — same fallback SimpliAj uses when
        the websocket progress push goes quiet."""
        ch = self._watching_channel
        if not ch or not ch.get("id"):
            return
        cur = self.node.gql.get_current_drop(ch["id"])
        if not cur or not cur.get("dropID"):
            return
        state = self.node.state
        minutes = int(cur.get("currentMinutesWatched") or 0)
        state.route_session_seen = True
        # Credited delta since the last sample for the same drop.
        last = getattr(self, "_credit_last", None)
        if last and last[0] == cur["dropID"] and minutes > last[1]:
            gained = minutes - last[1]
            state.route_credited += gained
            state.game_credited += gained
            if state.credit_samples:
                t, w, c = state.credit_samples[-1]
                state.credit_samples[-1] = (t, w, c + gained)
        self._credit_last = (cur["dropID"], minutes)
        for d in state.drops:
            if d.drop_id == cur["dropID"]:
                if minutes != d.current_minutes:
                    d.current_minutes = minutes
                    if DEBUG_LOG:
                        self.node._log(f"progress (CurrentDrop): {d.drop_name} {minutes}/{d.required_minutes}")
                state.watch_baseline_drop_id = d.drop_id
                state.watch_baseline_minutes = minutes
                state.heartbeats_since_baseline = 0
                break

    def _watch_cycle(self, game_name: str, keep_watching: Callable[[], bool]) -> bool:
        """
        One ~watch_interval cycle on the current channel:
          - HEAD new HLS segments now and every SEGMENT_POLL_SECONDS (earns time)
          - one Spade minute-watched beacon (auxiliary telemetry)
          - one CurrentDrop query at the end (real progress)
        Returns True if at least one segment poll succeeded this cycle.
        Exits early if stopped or keep_watching() turns False.
        """
        ch = self._watching_channel
        gql = self.node.gql
        login, bid = ch["login"], ch["broadcast_id"]

        earned = gql.poll_segments(login, bid)
        spade_ok = gql.send_watch_minute(
            broadcast_id=bid,
            channel_id=ch["id"],
            channel_login=login,
            game_name=game_name,
            game_id=self._watching_game_id,
        )
        if not spade_ok and DEBUG_LOG:
            self.node._log(f"beacon failed for {login} (non-fatal, segments are what count)")

        start = time.time()
        next_poll = start + self.SEGMENT_POLL_SECONDS
        while time.time() - start < self.watch_interval:
            if self.node._stop_requested or self.node.state.reconnect_requested or not keep_watching():
                return earned
            if time.time() >= next_poll:
                t0 = time.time()
                if gql.poll_segments(login, bid):
                    earned = True
                # schedule from poll start so slow polls don't drift the cadence
                next_poll = t0 + self.SEGMENT_POLL_SECONDS
            time.sleep(1)

        if earned:
            st = self.node.state
            dur = time.time() - start
            st.route_watched_s += dur
            st.credit_samples.append((time.time(), dur, 0))
            self._sync_progress_from_gql()
            self.node.check_route_crediting()
        return earned

    def _after_cycle(self, earned: bool) -> bool:
        """Book-keeping after a cycle. Returns True if the channel should be dropped."""
        self._record_heartbeat(earned)
        if earned:
            self._failed_cycles = 0
            return False
        self._failed_cycles += 1
        return self._failed_cycles >= self.MAX_FAILED_CYCLES

    def _record_heartbeat(self, success: bool) -> None:
        """Feed the smooth-progress estimator used by the UI. Every
        heartbeat attempt resets the "time since last heartbeat" clock
        (for the per-second fractional fill animation); a successful one
        also bumps the confirmed-minute estimate for whichever drop is
        currently active, as long as the active drop hasn't changed since
        the last real inventory refresh set the baseline."""
        state = self.node.state
        state.last_heartbeat_time = time.time()
        if not success:
            return
        active = state.active_drop
        if active and active.drop_id == state.watch_baseline_drop_id:
            state.heartbeats_since_baseline += 1

    def run_forever(self, state_callback: Optional[Callable[[AccountNodeState], None]] = None):
        self.node.state.status = "Starting..."
        if state_callback:
            state_callback(self.node.state)

        self.node.ensure_presence()

        last_campaign_refresh = 0.0

        while not self.node._stop_requested:
            try:
                if self.node.state.reconnect_requested:
                    # Proxy changed: drop the channel so the stream token and
                    # playlist are re-fetched through the new route.
                    self.node.state.reconnect_requested = False
                    self._watching_channel = None
                    self._watching_game = None
                    self._failed_cycles = 0
                    self.node.state.current_channel = ""
                    self.node._log("reconnecting through new network route")
                now = time.time()
                # Campaign progress + claiming is a heavier call — do it on
                # its own cadence, not every single heartbeat cycle.
                if (
                    now - last_campaign_refresh >= self.campaign_refresh_interval
                    or not self.node.state.drops
                ):
                    self.node.refresh_drops()
                    if self.node.auto_claim:
                        self.node.claim_ready_drops()
                    last_campaign_refresh = now

                self._maybe_follow_peer()
                manual_channel = (self.node.state.manual_channel or "").strip().lower()

                if manual_channel:
                    # Manual pick from the Live Streamers list: watch this
                    # exact channel, no auto game-matching, no ACL check,
                    # no AvailableDrops check.
                    target_game = self.node.state.selected_game or self.node.state.current_game or "manual pick"
                    need_new_channel = (
                        self._watching_channel is None
                        or self._watching_channel.get("login", "").lower() != manual_channel
                    )
                    if need_new_channel:
                        self.node.state.status = f"Resolving {manual_channel}..."
                        if state_callback:
                            state_callback(self.node.state)
                        try:
                            info = self.node.gql.get_stream_info(manual_channel)
                        except GQLError as e:
                            self.node._log(f"manual channel lookup failed: {e}")
                            self.node.state.status = f"{manual_channel} lookup failed"
                            self.node.state.current_channel = ""
                            self._watching_channel = None
                            if state_callback:
                                state_callback(self.node.state)
                            time.sleep(5)
                            continue

                        user = info.get("user") or {}
                        stream = user.get("stream") or info.get("stream") or {}
                        broadcaster_id = user.get("id")
                        broadcast_id = stream.get("id")
                        if not broadcaster_id or not broadcast_id:
                            self.node._log(f"manual channel {manual_channel}: not live or missing id in response")
                            self.node.state.status = f"{manual_channel} is not live"
                            self.node.state.current_channel = ""
                            self._watching_channel = None
                            if state_callback:
                                state_callback(self.node.state)
                            time.sleep(5)
                            continue

                        # Use the game the channel is actually streaming
                        # (if stream info includes it) rather than whatever
                        # game happens to be selected in the dropdown.
                        game_info = stream.get("game") or (user.get("broadcastSettings") or {}).get("game") or {}
                        resolved_game = game_info.get("displayName") or game_info.get("name")
                        st = self.node.state
                        if (self.strict_game and st.following and st.selected_game and resolved_game
                                and st.selected_game.strip().lower() not in self._stream_and_games(info)[1]):
                            # Idle node was about to follow a peer onto another game.
                            self.node._log(
                                f"follow: {manual_channel} is on {resolved_game}, not {st.selected_game} "
                                f"— staying idle (target-game lock)"
                            )
                            st.following = False
                            st.manual_channel = ""
                            st.current_channel = ""
                            st.status = f"No live streams for {st.selected_game}"
                            self._watching_channel = None
                            self._follow_block_until = time.time() + self.FOLLOW_RETRY_OWN
                            if state_callback:
                                state_callback(st)
                            continue
                        if resolved_game:
                            target_game = resolved_game

                        self._watching_channel = {
                            "login": manual_channel,
                            "id": broadcaster_id,
                            "broadcast_id": broadcast_id,
                        }
                        self._watching_game_id = game_info.get("id")
                        self._watching_game = target_game
                        self.node.state.current_game = target_game
                        self.node.state.current_channel = manual_channel
                        self.node.state.status = (
                            f"Following {manual_channel} (idle node, watching with a running node)"
                            if self.node.state.following else
                            f"Manually watching {manual_channel} (bypassing game/ACL checks)")
                        self.node._log(
                            f"manual override active: watching {manual_channel} directly, "
                            f"skipping game search + ACL/eligibility checks"
                        )
                        if state_callback:
                            state_callback(self.node.state)

                    # Watch cycle: segment polls every 10s + beacon + progress sync.
                    earned = self._watch_cycle(
                        self._watching_game or target_game,
                        lambda: (self.node.state.manual_channel or "").strip().lower() == manual_channel,
                    )
                    self.node.check_proxy_health()
                    if self._after_cycle(earned):
                        self.node._log(f"no segment traffic accepted for {self._watching_channel['login']} (manual channel) — re-resolving")
                        self._watching_channel = None
                        self._failed_cycles = 0
                        self.node.state.current_channel = ""

                    if state_callback:
                        state_callback(self.node.state)
                    continue

                self._rotate_game()
                target_game = self.node.state.selected_game
                if not target_game:
                    active_drop = self.node.state.active_drop
                    if active_drop:
                        target_game = active_drop.game_name
                        self.node.state.selected_game = target_game

                if not target_game:
                    self.node.state.status = "Idle - No target game selected"
                    self.node.state.current_channel = ""
                    self._watching_channel = None
                    self._watching_game = None
                    if state_callback:
                        state_callback(self.node.state)
                    time.sleep(3)
                    continue

                need_new_channel = self._watching_channel is None or self._watching_game != target_game
                if need_new_channel:
                    self.node.state.status = f"Searching channels for {target_game}..."
                    if state_callback:
                        state_callback(self.node.state)

                    game_id, channels = self.node.gql.get_live_channels_for_game(target_game)
                    if not channels:
                        self.node.state.status = f"No live streams for {target_game}"
                        self.node.state.current_channel = ""
                        self._watching_channel = None
                        self._watching_game = None
                        if state_callback:
                            state_callback(self.node.state)
                        time.sleep(5)
                        continue

                    # If this campaign is ACL-restricted, only a channel on
                    # that list actually credits progress. Either way, a
                    # channel must ALSO have Drops toggled on for THIS
                    # session — that's independent of ACL/game and has to
                    # be checked per-channel.
                    acl = self.node.game_acl.get(target_game, {})
                    active_drop = self.node.state.active_drop
                    target_campaign_id = active_drop.campaign_id if active_drop else ""

                    candidates = channels
                    if acl.get("acl_enabled"):
                        acl_logins = acl["acl_logins"]
                        candidates = [
                            ch for ch in channels
                            if (ch.get("broadcaster", {}).get("login") or "").lower() in acl_logins
                        ]
                        if not candidates:
                            self.node._log(
                                f"none of the {len(acl_logins)} ACL channel(s) for {target_game} "
                                f"are in the top-30 live results — can't confirm an eligible channel"
                            )
                            self.node.state.status = f"No eligible (ACL) channel live for {target_game}"
                            self.node.state.current_channel = ""
                            self._watching_channel = None
                            self._watching_game = None
                            if state_callback:
                                state_callback(self.node.state)
                            time.sleep(10)
                            continue

                    target_channel = None
                    checked = 0
                    for ch in candidates[:10]:
                        login = ch.get("broadcaster", {}).get("login", "")
                        ch_id = ch.get("broadcaster", {}).get("id", "")
                        if not ch_id:
                            continue
                        checked += 1
                        try:
                            available = self.node.gql.get_available_drop_campaigns(ch_id)
                        except GQLError as e:
                            self.node._log(f"AvailableDrops query failed: {e}")
                            self.node._log("  can't verify per-channel eligibility right now — falling back to unverified pick")
                            target_channel = ch
                            break
                        if not target_campaign_id or target_campaign_id in available:
                            target_channel = ch
                            break
                        else:
                            self.node._log(f"  {login}: drops not currently enabled for this campaign, skipping")

                    if target_channel is None:
                        self.node._log(
                            f"checked {checked} live channel(s) for {target_game} — none currently have "
                            f"Drops enabled for this campaign right now"
                        )
                        self.node.state.status = f"No Drops-enabled channel live for {target_game}"
                        self.node.state.current_channel = ""
                        self._watching_channel = None
                        self._watching_game = None
                        if state_callback:
                            state_callback(self.node.state)
                        time.sleep(10)
                        continue

                    broadcaster = target_channel.get("broadcaster", {})
                    self._watching_channel = {
                        "login": broadcaster.get("login", ""),
                        "id": broadcaster.get("id", ""),
                        "broadcast_id": target_channel.get("id", ""),
                    }

                    # Verify/replace broadcast_id with the real stream-info
                    # query instead of trusting the directory listing's
                    # guessed field — log the raw shape either way.
                    try:
                        info = self.node.gql.get_stream_info(self._watching_channel["login"])
                        if DEBUG_LOG:
                            self.node._log(f"stream info raw: {info}")
                        user = info.get("user") or {}
                        stream = user.get("stream") or info.get("stream") or {}
                        real_broadcast_id = stream.get("id")
                        if real_broadcast_id:
                            if DEBUG_LOG:
                                self.node._log(
                                    f"broadcast_id: directory gave {self._watching_channel['broadcast_id']!r}, "
                                    f"stream-info gave {real_broadcast_id!r} — using stream-info value"
                                )
                            self._watching_channel["broadcast_id"] = real_broadcast_id
                        # Only enforce the game lock on this channel if Twitch's
                        # game name matched the target at pick time — otherwise a
                        # naming mismatch would make the node re-pick forever.
                        self._watching_channel["game_names_ok"] = (
                            target_game.strip().lower() in self._stream_and_games(info)[1]
                        )
                    except GQLError as e:
                        self.node._log(f"get_stream_info failed: {e}")

                    self._watching_game_id = game_id
                    self._watching_game = target_game
                    self.node.state.current_game = target_game
                    self.node.state.current_channel = self._watching_channel["login"]
                    self.node.state.status = f"Mining {target_game} on {self._watching_channel['login']}"
                    self.node._log(f"watching {self._watching_channel['login']} for {target_game}")
                    if state_callback:
                        state_callback(self.node.state)

                # Watch cycle. HLS segment HEADs every 10s are what actually
                # credit minutes now (ported from SimpliAj's fork); the beacon
                # POST is still sent once per cycle as telemetry. See gql.py.
                earned = self._watch_cycle(
                    target_game,
                    lambda: self.node.state.selected_game == target_game,
                )
                self.node.check_proxy_health()
                off_reason = self._off_target_reason(target_game)
                if off_reason:
                    self.node._log(
                        f"leaving {self._watching_channel['login']}: {off_reason} "
                        f"— re-picking a {target_game} channel"
                    )
                    self._watching_channel = None
                    self._watching_game = None
                    self._failed_cycles = 0
                    self.node.state.current_channel = ""
                    self.node.state.status = f"Searching channels for {target_game}..."
                    if state_callback:
                        state_callback(self.node.state)
                    continue
                if self._after_cycle(earned):
                    self.node._log(f"no segment traffic accepted for {self._watching_channel['login']} — will re-pick a channel")
                    self._watching_channel = None
                    self._watching_game = None
                    self._failed_cycles = 0
                    self.node.state.current_channel = ""

                if state_callback:
                    state_callback(self.node.state)

            except Exception as e:
                logger.exception(f"[{self.node.account_name}] Error in miner loop: {e}")
                self.node.check_proxy_health()
                self.node.state.last_error = str(e)
                self.node.state.status = "Error - Retrying in 5s"
                self._watching_channel = None
                self._watching_game = None
                if state_callback:
                    state_callback(self.node.state)
                time.sleep(5)

        self.node.state.status = "Stopped"
        if state_callback:
            state_callback(self.node.state)