"""Jev arcade: twenty sites, one process. Each site lives at /<slug>/ and talks to Jev through /api/jev."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from jev_playground.arcade import lnm
from jev_playground.arcade.jev import MODEL_ID, JevError, _key, decide
from jev_playground.arcade.registry import SITES, SLUGS
from jev_playground.arcade.telemetry import TELEMETRY

HERE = Path(__file__).parent
WEB = HERE / "web"
SITES_DIR = HERE / "sites"
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8780"))
MAX_BODY = 768 * 1024
MAX_QUESTIONS = 300
UPSTREAM = threading.BoundedSemaphore(int(os.environ.get("JEV_MAX_INFLIGHT", "64")))

TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
}


class Bucket:
    """Per-client token bucket. Generous, because the fan-out demos burst on purpose."""

    def __init__(self, rate: float, burst: float) -> None:
        self.rate, self.burst = rate, burst
        self.state: dict[str, tuple[float, float]] = {}
        self.lock = threading.Lock()

    def take(self, client: str) -> bool:
        now = time.monotonic()
        with self.lock:
            tokens, last = self.state.get(client, (self.burst, now))
            tokens = min(self.burst, tokens + (now - last) * self.rate)
            if tokens < 1:
                self.state[client] = (tokens, now)
                return False
            self.state[client] = (tokens - 1, now)
            if len(self.state) > 5000:
                self.state.clear()
            return True


LIMIT = Bucket(float(os.environ.get("JEV_RATE_PER_S", "6")), float(os.environ.get("JEV_BURST", "180")))


class HackerNews:
    TTL = 300

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.cached: tuple[float, list[dict]] | None = None

    @staticmethod
    def _get(url: str):
        with urllib.request.urlopen(url, timeout=10) as response:
            return json.load(response)

    def top(self) -> list[dict]:
        with self.lock:
            if self.cached and time.time() - self.cached[0] < self.TTL:
                return self.cached[1]
            ids = self._get("https://hacker-news.firebaseio.com/v0/topstories.json")[:30]
            with ThreadPoolExecutor(10) as pool:
                items = list(pool.map(lambda i: self._get(f"https://hacker-news.firebaseio.com/v0/item/{i}.json"), ids))
            stories = [
                {
                    "id": item["id"],
                    "title": item.get("title", ""),
                    "url": item.get("url") or f"https://news.ycombinator.com/item?id={item['id']}",
                    "points": item.get("score", 0),
                    "comments": item.get("descendants", 0),
                    "by": item.get("by", ""),
                    "time": item.get("time", 0),
                }
                for item in items
                if item and item.get("title")
            ]
            self.cached = (time.time(), stories)
            return stories


HN = HackerNews()


def _log(**fields) -> None:
    print(json.dumps({"ts": round(time.time(), 3), **fields}), flush=True)


def _count_kinds(questions: dict) -> dict:
    kinds: dict[str, int] = {}
    for question in questions.values():
        kind = question.get("type", "?") if isinstance(question, dict) else "?"
        kinds[kind] = kinds.get(kind, 0) + 1
    return kinds


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "jev-arcade"

    def log_message(self, fmt: str, *args) -> None:
        return

    def _client(self) -> str:
        for header in ("CF-Connecting-IP", "X-Real-IP"):
            value = self.headers.get(header)
            if value:
                return value.strip()
        forwarded = self.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return self.client_address[0]

    def _send(self, code: int, body: bytes, content_type: str, cache: str = "no-store", extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, payload) -> None:
        self._send(code, json.dumps(payload).encode(), "application/json")

    def _file(self, target: Path, root: Path) -> None:
        target = target.resolve()
        if not target.is_relative_to(root.resolve()) or not target.is_file():
            self._send(404, b"Not found.", "text/plain; charset=utf-8")
            return
        cache = "no-cache" if target.suffix == ".html" else "public, max-age=300"
        self._send(200, target.read_bytes(), TYPES.get(target.suffix, "application/octet-stream"), cache)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/healthz", "/readyz"):
            self._json(200, {"ok": True})
            return
        if path == "/metrics":
            self._send(200, TELEMETRY.prometheus().encode(), "text/plain; version=0.0.4")
            return
        if path == "/api/sites":
            self._json(200, {"model": MODEL_ID, "sites": SITES})
            return
        if path == "/api/stats":
            self._json(200, TELEMETRY.snapshot())
            return
        if path == "/api/feed":
            self._feed()
            return
        if path == "/api/hn":
            try:
                self._json(200, {"stories": HN.top()})
            except Exception as exc:  # noqa: BLE001 — the page shows the fetch error
                self._json(502, {"error": f"Hacker News fetch failed: {exc}"})
            return
        if path.startswith("/api/btc/"):
            self._btc_get(path)
            return
        if path in ("/", "/index.html"):
            self._file(WEB / "index.html", WEB)
            return
        if path.startswith("/shared/"):
            self._file(WEB / path.lstrip("/"), WEB)
            return
        if path == "/favicon.ico":
            self._file(WEB / "shared" / "favicon.svg", WEB)
            return
        parts = path.strip("/").split("/", 1)
        slug = parts[0]
        if slug in SLUGS or (SITES_DIR / slug).is_dir():
            if not path.startswith(f"/{slug}/"):
                self._send(301, b"", "text/plain", extra={"Location": f"/{slug}/"})
                return
            rest = parts[1] if len(parts) > 1 and parts[1] else "index.html"
            self._file(SITES_DIR / slug / rest, SITES_DIR / slug)
            return
        self._send(404, b"Not found.", "text/plain; charset=utf-8")

    def _feed(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        seq = max(0, TELEMETRY.seq - 25)
        deadline = time.monotonic() + 600
        try:
            while time.monotonic() < deadline:
                events = TELEMETRY.since(seq, timeout=15)
                if events:
                    seq = events[-1]["seq"]
                    for event in events:
                        self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                else:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return

    def _btc_guard(self) -> bool:
        if not lnm.live_enabled():
            self._json(403, {"error": "Live trading is off on this server. Paper trading still works."})
            return False
        if not lnm.token_ok(self.headers.get("X-Trade-Token")):
            self._json(401, {"error": "Wrong or missing trade token."})
            return False
        if not LIMIT.take("btc:" + self._client()):
            self._json(429, {"error": "Slow down."})
            return False
        return True

    def _btc_get(self, path: str) -> None:
        try:
            if path == "/api/btc/market":
                self._json(200, lnm.MARKET.snapshot())
            elif path == "/api/btc/status":
                self._json(200, lnm.status())
            elif path == "/api/btc/account" and self._btc_guard():
                account = lnm.account()
                self._json(200, {"account": {k: account.get(k) for k in ("username", "balance", "syntheticUsdBalance")}, "running": lnm.running()})
            elif path not in ("/api/btc/account",):
                self._json(404, {"error": "Unknown path."})
        except lnm.LnmError as exc:
            self._json(exc.status if 400 <= exc.status < 600 else 502, {"error": str(exc)})

    def _btc_post(self, path: str, body: dict) -> None:
        if not self._btc_guard():
            return
        try:
            if path == "/api/btc/trade":
                result = lnm.open_trade(str(body.get("side")), int(body.get("quantity") or 1), int(body.get("leverage") or 1), body.get("stoploss"), body.get("takeprofit"))
                _log(event="btc_trade", network=lnm.NETWORK, side=body.get("side"), quantity=body.get("quantity"), leverage=body.get("leverage"))
                self._json(200, result)
            elif path == "/api/btc/close":
                result = lnm.close_trade(str(body.get("id")))
                _log(event="btc_close", network=lnm.NETWORK, id=body.get("id"))
                self._json(200, result)
            else:
                self._json(404, {"error": "Unknown path."})
        except lnm.LnmError as exc:
            self._json(exc.status if 400 <= exc.status < 600 else 502, {"error": str(exc)})
        except (TypeError, ValueError) as exc:
            self._json(400, {"error": str(exc)})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/btc/"):
            length = int(self.headers.get("Content-Length", "0") or 0)
            try:
                body = json.loads(self.rfile.read(min(length, 8192)) or b"{}")
            except json.JSONDecodeError:
                self._json(400, {"error": "Body was not JSON."})
                return
            self._btc_post(path, body if isinstance(body, dict) else {})
            return
        if path != "/api/jev":
            self._json(404, {"error": "Unknown path."})
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > MAX_BODY:
            self._json(413, {"error": "That request is too large for the arcade."})
            return
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "Body was not JSON."})
            return
        site = str(body.get("site") or "")
        state = body.get("state")
        questions = body.get("questions")
        if site not in SLUGS and site != "hub" and not (SITES_DIR / site).is_dir():
            self._json(400, {"error": "Unknown site."})
            return
        if state in (None, "", [], {}):
            self._json(400, {"error": "State is empty."})
            return
        if not isinstance(questions, dict) or not questions:
            self._json(400, {"error": "Ask at least one question."})
            return
        if len(questions) > MAX_QUESTIONS:
            self._json(400, {"error": f"At most {MAX_QUESTIONS} questions per call here."})
            return
        client = self._client()
        if not LIMIT.take(client):
            self._json(429, {"error": "Easy there. The arcade rate limit is refilling."})
            return
        kinds = _count_kinds(questions)
        received = time.perf_counter()
        with UPSTREAM:
            try:
                result = decide(site, state, questions)
            except JevError as exc:
                ms = round((time.perf_counter() - received) * 1000, 1)
                TELEMETRY.record(site, False, ms, len(questions), kinds=kinds)
                _log(event="jev", site=site, ok=False, ms=ms, q=len(questions), status=exc.status, error=str(exc)[:300])
                self._json(exc.status if 400 <= exc.status < 600 else 502, {"error": str(exc)})
                return
            except Exception as exc:  # noqa: BLE001
                ms = round((time.perf_counter() - received) * 1000, 1)
                TELEMETRY.record(site, False, ms, len(questions), kinds=kinds)
                _log(event="jev", site=site, ok=False, ms=ms, q=len(questions), error=str(exc)[:300])
                self._json(502, {"error": str(exc)})
                return
        usage = result["usage"]
        TELEMETRY.record(site, True, result["elapsed_ms"], len(questions), usage["input_tokens"], float(usage["cost"] or 0), kinds)
        _log(event="jev", site=site, ok=True, ms=result["elapsed_ms"], q=len(questions), tokens=usage["input_tokens"], cost=usage["cost"])
        result["questions"] = len(questions)
        result["server_ms"] = round((time.perf_counter() - received) * 1000, 1)
        self._json(200, result)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 256


def main() -> None:
    try:
        _key()
        key = "present"
    except RuntimeError:
        key = "MISSING"
    server = Server((HOST, PORT), Handler)
    _log(event="start", url=f"http://{HOST}:{PORT}", model=MODEL_ID, sites=len(SITES), key=key)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
