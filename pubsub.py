"""
Minimal client for Twitch's PubSub WebSocket protocol.

This is the same real-time channel the actual Twitch web client keeps
open while a tab has a stream loaded — it's what pushes chat, channel
point, and drop-progress notifications to the browser instantly instead
of polling. It's long-documented across many independent, legitimate
third-party Twitch tools (channel-point bots, chat overlays, etc.), not
something exclusive to drop miners.

We use it here because the HTTP-only heartbeat loop wasn't enough to
move drop progress — the working hypothesis (backed by the real
DevilXD/TwitchDropsMiner source, which keeps this connection open the
entire time it mines) is that Twitch's backend treats an open PubSub
session as part of what counts as "actively present," separate from
the periodic Spade beacon.
"""
from __future__ import annotations

import json
import logging
import random
import string
import threading
import time
from typing import Callable

import websocket  # pip install websocket-client

logger = logging.getLogger(__name__)

PUBSUB_URL = "wss://pubsub-edge.twitch.tv/v1"
PING_INTERVAL = 180  # seconds — Twitch expects a PING roughly every 3-5 min


def _nonce(n: int = 16) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=n))


class PubSubClient:
    """
    Runs its own background thread, keeps a WebSocket connection open,
    (re)subscribes to whatever topics have been requested, reconnects
    with backoff on failure, and forwards MESSAGE frames to on_message.
    """

    def __init__(
        self,
        access_token: str,
        on_message: Callable[[str, dict], None] | None = None,
        on_log: Callable[[str], None] | None = None,
        proxy: str | None = None,
    ):
        self.access_token = access_token
        self.proxy = proxy
        self.on_message = on_message
        self.on_log = on_log or (lambda msg: logger.info(msg))
        self._ws: websocket.WebSocket | None = None
        self._topics: set[str] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_ping = 0.0
        self.connected = False

    def add_topic(self, topic: str) -> None:
        if topic in self._topics:
            return
        self._topics.add(topic)
        if self._ws is not None:
            self._subscribe([topic])

    def set_proxy(self, proxy: str | None) -> None:
        """Switch proxy live: drop the current socket so _run reconnects through the new one."""
        self.proxy = proxy or None
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass

    def _subscribe(self, topics: list[str]) -> None:
        if not self._ws:
            return
        msg = {
            "type": "LISTEN",
            "nonce": _nonce(),
            "data": {"topics": topics, "auth_token": self.access_token},
        }
        try:
            self._ws.send(json.dumps(msg))
        except Exception as e:
            self.on_log(f"pubsub subscribe send failed: {e}")

    def _run(self) -> None:
        backoff = 5
        while not self._stop.is_set():
            try:
                from proxies import websocket_kwargs
                self._ws = websocket.create_connection(
                    PUBSUB_URL, timeout=15, **websocket_kwargs(self.proxy)
                )
                self.connected = True
                self.on_log("pubsub: connected")
                if self._topics:
                    self._subscribe(list(self._topics))
                self._last_ping = time.time()
                backoff = 5

                self._ws.settimeout(10)
                while not self._stop.is_set():
                    now = time.time()
                    if now - self._last_ping > PING_INTERVAL:
                        self._ws.send(json.dumps({"type": "PING"}))
                        self._last_ping = now
                    try:
                        raw = self._ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not raw:
                        break
                    self._handle_frame(raw)

            except Exception as e:
                self.on_log(f"pubsub: connection error: {e}")
            finally:
                self.connected = False
                try:
                    if self._ws:
                        self._ws.close()
                except Exception:
                    pass
                self._ws = None

            if not self._stop.is_set():
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _handle_frame(self, raw: str) -> None:
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            return
        ftype = frame.get("type")
        if ftype == "PING":
            if self._ws:
                self._ws.send(json.dumps({"type": "PONG"}))
        elif ftype == "RESPONSE":
            err = frame.get("error")
            if err:
                self.on_log(f"pubsub: topic subscribe error: {err}")
            else:
                self.on_log("pubsub: topic subscribed")
        elif ftype == "MESSAGE":
            data = frame.get("data", {})
            topic = data.get("topic", "")
            try:
                message = json.loads(data.get("message", "{}"))
            except json.JSONDecodeError:
                message = {}
            if self.on_message:
                self.on_message(topic, message)
        elif ftype == "RECONNECT":
            self.on_log("pubsub: server requested reconnect")
            if self._ws:
                self._ws.close()
