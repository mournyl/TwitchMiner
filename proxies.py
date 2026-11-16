"""
User-supplied proxy list: import, store, assign one proxy per account.

The app ships with NO proxies — the user imports their own list.

Accepted line formats (one per line; blank lines and # comments ignored):
    host:port
    host:port:user:pass
    user:pass@host:port
    scheme://host:port
    scheme://user:pass@host:port
scheme = http | https | socks4 | socks5 | socks5h  (default: the import's default scheme, http)
SOCKS needs:  pip install "requests[socks]" python-socks

Stored in data/proxies.json:
    {"proxies": ["http://1.2.3.4:8080", ...],
     "assignments": {"account_name": "http://1.2.3.4:8080", ...}}

When an account has a proxy, ALL of its traffic (GQL, page scrapes, beacon,
HLS segments, PubSub websocket, token validation) goes through it. If the
proxy is dead, requests fail — there is deliberately no silent fallback to
a direct connection.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from config import DATA_DIR

PROXIES_PATH = DATA_DIR / "proxies.json"
# Proxies that passed their most recent test, one per line (full URL incl.
# login). Next launch tests only these instead of the whole list.
WORKING_PATH = DATA_DIR / "working_proxies.txt"
# Per-proxy consecutive failed tests, across launches.
STATS_PATH = DATA_DIR / "proxy_stats.json"
# Paid-list mode: proxies are NEVER dropped automatically. A failed proxy
# just sits out for RETRY_AFTER seconds, then the background re-check
# tests it again and puts it back in the pool if it passes.
RETRY_AFTER = 180
RECHECK_EVERY = 60
# After startup, keep testing the rest of the list in the background until
# at least this many proxies work (plus 2 per account, set by the app).
MIN_WORKING = 10
SCHEMES = ("http", "https", "socks4", "socks5", "socks5h")


def parse_proxy(line: str, default_scheme: str = "http") -> str | None:
    """Normalize one line to scheme://[user:pass@]host:port, or None if invalid."""
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    scheme = default_scheme
    is_url = "://" in s
    if is_url:
        scheme, s = s.split("://", 1)
        scheme = scheme.lower()
    if scheme not in SCHEMES:
        return None

    user = pwd = None
    parts = s.split(":", 3)
    if len(parts) == 4 and parts[1].strip().isdigit():
        # host:port:user:pass — checked first so a password containing
        # "@" or ":" isn't mistaken for the user:pass@host form.
        host, port, user, pwd = parts
    elif "@" in s:
        creds, hostport = s.rsplit("@", 1)
        if ":" not in creds:
            return None
        user, pwd = creds.split(":", 1)
        hp = hostport.split(":")
        if len(hp) != 2:
            return None
        host, port = hp
    elif len(parts) == 2:
        host, port = parts
    else:
        return None

    if is_url and user is not None:
        # scheme://user:pass@ form is already percent-encoded (e.g. our own
        # working_proxies.txt) — decode so it isn't encoded twice below.
        user, pwd = unquote(user), unquote(pwd)
    host = host.strip().strip("[]")
    port = port.strip()
    if not host or not port.isdigit() or not (0 < int(port) < 65536):
        return None
    auth = f"{quote(user, safe='')}:{quote(pwd, safe='')}@" if user is not None else ""
    return f"{scheme}://{auth}{host}:{port}"


def mask(url: str | None) -> str:
    """Display form with the password hidden."""
    if not url:
        return "direct (no proxy)"
    p = urlsplit(url)
    who = f"{unquote(p.username)}:***@" if p.username else ""
    return f"{p.scheme}://{who}{p.hostname}:{p.port}"


def requests_proxies(url: str | None) -> dict:
    return {"http": url, "https": url} if url else {}


def websocket_kwargs(url: str | None) -> dict:
    """kwargs for websocket.create_connection()."""
    if not url:
        return {}
    p = urlsplit(url)
    kw = {
        "http_proxy_host": p.hostname,
        "http_proxy_port": p.port,
        # websocket-client wants "http" for CONNECT proxies
        "proxy_type": "http" if p.scheme in ("http", "https") else p.scheme,
    }
    if p.username:
        kw["http_proxy_auth"] = (unquote(p.username), unquote(p.password or ""))
    return kw


def describe_error(e: BaseException) -> str:
    """Short, human explanation of a network failure through a proxy."""
    import requests

    text = str(e)
    if isinstance(e, requests.exceptions.SSLError) or "CERTIFICATE_VERIFY_FAILED" in text:
        if "self-signed" in text or "self signed" in text:
            return ("proxy is intercepting HTTPS (self-signed cert) — it can read your "
                    "account token; don't use it")
        return "TLS certificate check failed through this proxy — don't use it"
    if isinstance(e, requests.exceptions.ProxyError):
        return "proxy refused or unreachable (wrong type, host, port or login?)"
    if isinstance(e, requests.exceptions.ConnectTimeout) or isinstance(e, requests.exceptions.ReadTimeout):
        return "timed out through proxy"
    if "Missing dependencies for SOCKS" in text:
        return 'SOCKS support not installed: python -m pip install "requests[socks]" python-socks'
    return f"{type(e).__name__}: {text[:160]}"


# Test tuning. A dead proxy now costs at most CONNECT_TIMEOUT seconds
# (TCP connect to the proxy), a slow-but-alive one at most
# CONNECT_TIMEOUT + READ_TIMEOUT. Was: 2 sequential requests x 10s.
CONNECT_TIMEOUT = 4
READ_TIMEOUT = 6
TEST_WORKERS = 64
TEST_URL = "https://gql.twitch.tv/gql"  # the host the app actually needs


def test_proxy(url: str, connect_timeout: float = CONNECT_TIMEOUT,
               read_timeout: float = READ_TIMEOUT) -> tuple[bool, str]:
    """One HTTPS request to Twitch through the proxy (TLS certificate is
    verified, so intercepting proxies fail). Returns (ok, info)."""
    import time as _t

    import requests

    s = requests.Session()
    s.trust_env = False
    t0 = _t.monotonic()
    try:
        r = s.get(TEST_URL, proxies=requests_proxies(url), timeout=(connect_timeout, read_timeout))
        ms = int((_t.monotonic() - t0) * 1000)
        # Any HTTP answer from Twitch means the tunnel works (GET on /gql is a 4xx).
        if r.status_code >= 500:
            return False, f"twitch HTTP {r.status_code} via proxy"
        return True, f"{ms} ms"
    except Exception as e:
        return False, describe_error(e)
    finally:
        s.close()


class ProxyStore:
    """Thread-safe persisted proxy list + per-account assignments, plus
    in-memory health from this session's tests. Only proxies that PASSED a
    test this session are ever handed out (next_free / auto_assign /
    replacement of a failed proxy) — the list is never just walked in order."""

    def __init__(self, path: Path = PROXIES_PATH, working_path: Path | None = None):
        self._path = path
        self._working_path = working_path or path.with_name(WORKING_PATH.name)
        self._lock = threading.RLock()
        self.proxies: list[str] = []
        self.assignments: dict[str, str] = {}
        # url -> (ok, info). Not persisted: every launch re-tests.
        self.health: dict[str, tuple[bool, str]] = {}
        self.latency: dict[str, int] = {}  # url -> ms, for passing proxies
        self._stats_path = self._path.with_name(STATS_PATH.name)
        self.strikes: dict[str, int] = {}
        self.failed_at: dict[str, float] = {}  # url -> when it last failed
        try:
            self.strikes = {k: int(v) for k, v in json.loads(self._stats_path.read_text("utf-8")).items()}
        except Exception:
            pass
        # Progress of the current test batch (startup check, Test All, …).
        # Resets when a new batch starts after everything finished.
        self.batch_total = 0
        self.batch_done = 0
        self.batch_label = ""
        self.testing: set[str] = set()
        self.load()

    # -- persistence -------------------------------------------------------
    def load(self) -> None:
        with self._lock:
            try:
                data = json.loads(self._path.read_text("utf-8"))
                self.proxies = [p for p in data.get("proxies", []) if isinstance(p, str)]
                self.assignments = {
                    k: v for k, v in (data.get("assignments") or {}).items() if v in self.proxies
                }
            except Exception:
                self.proxies, self.assignments = [], {}

    def save(self) -> None:
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps({"proxies": self.proxies, "assignments": self.assignments}, indent=2), "utf-8"
            )

    # -- list management ---------------------------------------------------
    def import_text(self, text: str, default_scheme: str = "http") -> tuple[int, int, int, list[str]]:
        """Returns (added, duplicates, invalid, new_urls)."""
        added = dup = bad = 0
        new_urls: list[str] = []
        with self._lock:
            existing = set(self.proxies)
            for line in text.splitlines():
                if not line.strip() or line.strip().startswith("#"):
                    continue
                url = parse_proxy(line, default_scheme)
                if url is None:
                    bad += 1
                elif url in existing:
                    dup += 1
                else:
                    self.proxies.append(url)
                    existing.add(url)
                    new_urls.append(url)
                    added += 1
            self.save()
        return added, dup, bad, new_urls

    def import_file(self, path: str | Path, default_scheme: str = "http") -> tuple[int, int, int, list[str]]:
        text = Path(path).read_text("utf-8", errors="ignore")
        return self.import_text(text, default_scheme)

    def remove(self, urls: list[str]) -> None:
        with self._lock:
            gone = set(urls)
            self.proxies = [p for p in self.proxies if p not in gone]
            self.assignments = {k: v for k, v in self.assignments.items() if v not in gone}
            for u in gone:
                self.health.pop(u, None)
            self.save()
            self._write_working([p for p in self.load_working_quiet() if p not in gone])

    def clear(self) -> None:
        with self._lock:
            self.proxies, self.assignments, self.health = [], {}, {}
            self.save()
            self._write_working([])

    def _write_working(self, urls: list[str]) -> None:
        try:
            self._working_path.write_text(
                "# Proxies that passed their last test (auto-generated)\n"
                + "".join(p + "\n" for p in urls), "utf-8")
        except OSError:
            pass

    # -- working list file -------------------------------------------------
    def load_working(self) -> list[str]:
        """URLs from working_proxies.txt (added to the main list if missing)."""
        try:
            text = self._working_path.read_text("utf-8")
        except OSError:
            return []
        urls: list[str] = []
        with self._lock:
            for line in text.splitlines():
                u = parse_proxy(line)
                if u and u not in urls:
                    urls.append(u)
                    if u not in self.proxies:
                        self.proxies.append(u)
            self.save()
        return urls

    def save_working(self) -> None:
        """Rewrite working_proxies.txt: proxies that passed their latest test,
        plus previously-working ones not re-tested this session."""
        with self._lock:
            prev = set(self.load_working_quiet())
            # Never drop: everything already saved stays, new passes are added.
            # (Only Remove / Remove Failed / Clear in the UI take one out.)
            keep = [p for p in self.proxies if self.is_ok(p) or p in prev]
            try:
                self._stats_path.write_text(json.dumps(
                    {k: v for k, v in self.strikes.items() if v and k in self.proxies}), "utf-8")
            except OSError:
                pass
            try:
                self._working_path.parent.mkdir(parents=True, exist_ok=True)
                self._working_path.write_text(
                    "# Proxies that passed their last test (auto-generated)\n"
                    + "".join(p + "\n" for p in keep), "utf-8")
            except OSError:
                pass

    def load_working_quiet(self) -> list[str]:
        try:
            lines = self._working_path.read_text("utf-8").splitlines()
        except OSError:
            return []
        return [u for u in (parse_proxy(l) for l in lines) if u]

    def startup_test(self, on_result=None, on_phase=None) -> None:
        """Launch-time check. If working_proxies.txt exists, test only those
        (fast). If none of them still work — or there's no file yet — test
        the rest of the list. on_phase(text, count) announces each phase."""
        saved = self.load_working()
        if saved:
            if on_phase:
                on_phase("saved working", len(saved))
            self.test(saved, on_result=on_result, label="saved working proxies")
            if self.healthy():
                return
        rest = [p for p in self.proxies if p not in self.health]
        if rest:
            if on_phase:
                on_phase("full list" if not saved else "rest of list (saved ones all failed)", len(rest))
            self.test(rest, on_result=on_result,
                      label="full proxy list" if not saved else "rest of list (saved ones all failed)")

    def recheck_failed(self, retry_after: float = RETRY_AFTER) -> int:
        """Re-test proxies that failed at least `retry_after` seconds ago.
        Passing ones rejoin the pool. Returns how many came back."""
        now = time.time()
        with self._lock:
            due = [p for p in self.proxies
                   if self.status(p) == "failed" and now - self.failed_at.get(p, 0) >= retry_after]
        if not due:
            return 0
        self.test(due, label="re-checking failed proxies")
        return sum(1 for p in due if self.is_ok(p))

    def start_recheck_loop(self, every: float = RECHECK_EVERY, on_recovered=None) -> None:
        """Background thread: periodically give failed proxies another chance."""
        if getattr(self, "_recheck_started", False):
            return
        self._recheck_started = True

        def loop():
            while True:
                time.sleep(every)
                try:
                    n = self.recheck_failed()
                    if n and on_recovered:
                        on_recovered(n)
                except Exception:
                    pass

        threading.Thread(target=loop, daemon=True).start()

    def top_up(self, target: int, on_result=None, chunk: int = TEST_WORKERS) -> int:
        """Test not-yet-tested proxies in chunks until `target` work or the
        list runs out — refills the pool so working_proxies.txt grows back.
        Returns how many work afterwards."""
        while len(self.healthy()) < target:
            batch = [p for p in self.proxies if self.status(p) == "untested"][:chunk]
            if not batch:
                break
            self.test(batch, on_result=on_result, label="topping up the pool")
        return len(self.healthy())

    # -- health ------------------------------------------------------------
    def set_health(self, url: str, ok: bool, info: str) -> None:
        with self._lock:
            self.strikes[url] = 0 if ok else self.strikes.get(url, 0) + 1
            if ok:
                self.failed_at.pop(url, None)
            else:
                self.failed_at[url] = time.time()
            self.health[url] = (ok, info)
            self.testing.discard(url)
            if ok and info.endswith(" ms") and info[:-3].isdigit():
                self.latency[url] = int(info[:-3])
            else:
                self.latency.pop(url, None)

    def is_ok(self, url: str | None) -> bool:
        with self._lock:
            return bool(url) and self.health.get(url, (False, ""))[0]

    def status(self, url: str) -> str:
        """'ok' | 'failed' | 'testing' | 'untested'"""
        with self._lock:
            if url in self.testing:
                return "testing"
            if url not in self.health:
                return "untested"
            return "ok" if self.health[url][0] else "failed"

    def healthy(self) -> list[str]:
        with self._lock:
            return [p for p in self.proxies if self.is_ok(p)]

    def test(self, urls: list[str] | None = None, workers: int = TEST_WORKERS,
             on_result=None, label: str = "") -> None:
        """Test proxies in parallel (blocking). on_result(url, ok, info) per proxy."""
        from concurrent.futures import ThreadPoolExecutor

        with self._lock:
            todo = [u for u in (self.proxies if urls is None else urls)
                    if u in self.proxies and u not in self.testing]
            if not self.testing:  # nothing in flight -> this is a fresh batch
                self.batch_total = self.batch_done = 0
            self.batch_total += len(todo)
            if label:
                self.batch_label = label
            self.testing.update(todo)

        def one(u: str) -> None:
            ok, info = test_proxy(u)
            if not ok and ("timed out" in info or "unreachable" in info):
                ok, info = test_proxy(u)  # one retry — free proxies blip
            self.set_health(u, ok, info)
            with self._lock:
                self.batch_done += 1
            if on_result:
                on_result(u, ok, info)

        if todo:
            with ThreadPoolExecutor(max_workers=max(1, min(workers, len(todo)))) as ex:
                list(ex.map(one, todo))
            self.save_working()

    # -- assignment --------------------------------------------------------
    def get(self, account: str) -> str | None:
        with self._lock:
            return self.assignments.get(account)

    def assign(self, account: str, url: str | None) -> None:
        with self._lock:
            if url:
                if url not in self.proxies:
                    raise ValueError("proxy not in list")
                self.assignments[account] = url
            else:
                self.assignments.pop(account, None)
            self.save()

    def users_of(self, url: str) -> list[str]:
        with self._lock:
            return [a for a, p in self.assignments.items() if p == url]

    def next_free(self, exclude_account: str | None = None) -> str | None:
        """Least-used proxy among those that PASSED a test this session
        (unused first). None if none have passed."""
        with self._lock:
            pool = self.healthy()
            if not pool:
                return None
            counts = {p: 0 for p in pool}
            for a, p in self.assignments.items():
                if p in counts and a != exclude_account:
                    counts[p] += 1
            # Least-used first; among equals, the fastest one from its test.
            return min(pool, key=lambda p: (counts[p], self.latency.get(p, 10**6)))

    def auto_assign(self, accounts: list[str]) -> dict[str, str]:
        """Give every account that has no proxy, or whose proxy FAILED its
        test, the least-used working proxy. Untested proxies are left alone.
        Returns the new assignments."""
        new: dict[str, str] = {}
        with self._lock:
            for a in accounts:
                cur = self.assignments.get(a)
                if cur and self.status(cur) != "failed":
                    continue
                p = self.next_free(exclude_account=a)
                if p is None:
                    break
                self.assignments[a] = p
                new[a] = p
            self.save()
        return new


def make_failover(store: ProxyStore, on_change=None):
    """Build a MiningNode.on_proxy_trouble handler. When an account's proxy
    fails while running:
      1. mark it failed (never handed out again this session, dropped from
         working_proxies.txt)
      2. swap the account to the least-used working proxy, immediately
      3. if none is left, test the not-yet-tested part of the list in the
         background and swap as soon as one passes
    Never falls back to a direct connection."""
    refill_lock = threading.Lock()

    def swap(node, old: str | None, reason: str) -> bool:
        name = node.account_name
        rep = store.next_free(exclude_account=name)
        if rep is None or rep == old:
            return False
        store.assign(name, rep)
        try:
            node.state.proxy_swaps += 1
        except AttributeError:
            pass
        node._log(f"proxy {mask(old)} failed ({reason}) — swapped to {mask(rep)}")
        node.set_proxy(rep)
        if on_change:
            on_change(name, old, rep)
        return True

    def refill_then_swap(node, old: str, reason: str) -> None:
        # One background refill at a time; other failing accounts just wait
        # for it and then grab whatever passed.
        with refill_lock:
            if store.get(node.account_name) != old:
                return  # already moved by someone else
            if swap(node, old, reason):
                return
            untested = [p for p in store.proxies if store.status(p) == "untested"]
            if not untested:
                node._log(f"proxy {mask(old)} failed ({reason}) and every proxy in the list "
                          "has failed — retrying on it; import more proxies to recover")
                return
            node._log(f"proxy {mask(old)} failed and no working spare — testing "
                      f"{len(untested)} untested proxies to find one…")
            found = threading.Event()
            first = threading.Lock()

            def on_result(u, ok, info):
                if ok:
                    with first:
                        if not found.is_set():
                            found.set()
                            swap(node, old, reason)  # swap on the FIRST one that passes

            store.test(untested, on_result=on_result, label="replacement proxies")
            if not found.is_set():
                node._log("none of the untested proxies work — retrying on the old one")

    def handler(node, reason: str) -> None:
        cur = store.get(node.account_name)
        if not cur:
            return
        store.set_health(cur, False, f"failed in use: {reason}")
        store.save_working()
        if not swap(node, cur, reason):
            threading.Thread(target=refill_then_swap, args=(node, cur, reason), daemon=True).start()

    return handler
