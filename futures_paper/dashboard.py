from __future__ import annotations

import hmac
import http.cookies
import http.server
import ipaddress
import json
import logging
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any

ROOT = Path(__file__).resolve().parent
BOT_PATH = ROOT / "bot.py"
STATE_PATH = ROOT / "paper_futures_state.json"
CONTROL_PATH = ROOT / "futures_control.json"
BOT_LOG_PATH = ROOT / "bot.log"
BOT_LOCK_PATH = ROOT / ".futures_paper_bot.lock"
TOKEN_PATH = (
    Path(os.environ.get("LOCALAPPDATA", Path.home()))
    / "CryptoDumpHunter"
    / "futures_control_token.txt"
)
PORT = int(os.environ.get("FUTURES_DASHBOARD_PORT", "8766"))
SESSION_COOKIE = "futures_paper_session"
SESSION_MAX_AGE_SECONDS = 12 * 60 * 60
HEARTBEAT_SECONDS = 5
HEARTBEAT_TIMEOUT_SECONDS = 20

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("futures_dashboard")

CONTROL_LOCK = threading.Lock()
RUNNER_LOCK = threading.Lock()
SESSIONS: dict[str, float] = {}
LOGIN_ATTEMPTS: dict[str, list[float]] = {}
CONTROL_ENABLED = False
BOT_PROCESS: subprocess.Popen | None = None


def load_or_create_token() -> tuple[str, bool]:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    if TOKEN_PATH.exists():
        token = TOKEN_PATH.read_text(encoding="utf-8").strip()
        if len(token) < 32:
            raise RuntimeError(f"Control token is invalid; remove {TOKEN_PATH} to generate a new one.")
        return token, False
    token = secrets.token_urlsafe(32)
    TOKEN_PATH.write_text(token + "\n", encoding="utf-8")
    return token, True


def write_control(enabled: bool | None = None) -> None:
    global CONTROL_ENABLED
    with CONTROL_LOCK:
        if enabled is not None:
            CONTROL_ENABLED = enabled
        document = {
            "accept_entries": CONTROL_ENABLED,
            "heartbeat_epoch": time.time(),
        }
        temporary = CONTROL_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(document), encoding="utf-8")
        temporary.replace(CONTROL_PATH)


def heartbeat_loop(stop_event: threading.Event) -> None:
    while not stop_event.wait(HEARTBEAT_SECONDS):
        try:
            write_control()
        except OSError:
            log.exception("Could not update the simulator control heartbeat; new entries will pause.")


def start_runner() -> None:
    global BOT_PROCESS
    with RUNNER_LOCK:
        if BOT_PROCESS is not None and BOT_PROCESS.poll() is None:
            return
        if runner_lock_is_held():
            log.info("The futures paper simulator is already running.")
            return
        with BOT_LOG_PATH.open("a", encoding="utf-8") as log_file:
            BOT_PROCESS = subprocess.Popen(
                [sys.executable, "-u", str(BOT_PATH)],
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
        log.info("Started futures paper simulator (pid=%d).", BOT_PROCESS.pid)


def runner_lock_is_held() -> bool:
    if not BOT_LOCK_PATH.exists():
        return False
    try:
        with BOT_LOCK_PATH.open("r+b") as lock_file:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    except OSError:
        return True
    return False


def local_addresses() -> set[str]:
    addresses = {"127.0.0.1", "::1"}
    try:
        for result in socket.getaddrinfo(socket.gethostname(), None):
            address = result[4][0].split("%", 1)[0]
            try:
                parsed = ipaddress.ip_address(address)
            except ValueError:
                continue
            if parsed.is_private or parsed.is_loopback or parsed.is_link_local:
                addresses.add(address)
    except OSError:
        log.exception("Could not enumerate local network addresses.")
    return addresses


def read_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"cash_usdt": 0.0, "positions": {}}
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or not isinstance(state.get("positions"), dict):
        raise ValueError("Paper state has an unexpected structure.")
    return state


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    server_version = "FuturesPaperDashboard"

    def log_message(self, fmt: str, *args: Any) -> None:
        log.info("%s - %s", self.client_address[0], fmt % args)

    def _send(self, status: int, body: bytes, content_type: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data: dict[str, Any], headers: dict[str, str] | None = None) -> None:
        self._send(
            status,
            json.dumps(data, separators=(",", ":")).encode("utf-8"),
            "application/json; charset=utf-8",
            headers,
        )

    def _is_allowed_client(self) -> bool:
        address = self.client_address[0].split("%", 1)[0]
        try:
            client = ipaddress.ip_address(address)
        except ValueError:
            return False
        return client.is_private or client.is_loopback or client.is_link_local

    def _is_allowed_host(self) -> bool:
        host_header = self.headers.get("Host", "").strip()
        hostname = host_header.rsplit(":", 1)[0].strip("[]").lower()
        if hostname == "localhost":
            return True
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            return False
        return str(address) in local_addresses()

    def _is_same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return False
        parsed = urlsplit(origin)
        return (
            parsed.scheme == "http"
            and parsed.netloc.lower() == self.headers.get("Host", "").lower()
        )

    def _session_id(self) -> str | None:
        cookie = http.cookies.SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except http.cookies.CookieError:
            return None
        morsel = cookie.get(SESSION_COOKIE)
        if morsel is None:
            return None
        session_id = morsel.value
        expires = SESSIONS.get(session_id)
        if expires is None or expires < time.time():
            SESSIONS.pop(session_id, None)
            return None
        SESSIONS[session_id] = time.time() + SESSION_MAX_AGE_SECONDS
        return session_id

    def _authorized(self) -> bool:
        return self._session_id() is not None

    def _request_json(self) -> dict[str, Any]:
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise ValueError("Expected application/json.")
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 4096:
            raise ValueError("Invalid request size.")
        data = json.loads(self.rfile.read(length))
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object.")
        return data

    def _login_rate_limited(self) -> bool:
        now = time.time()
        attempts = [stamp for stamp in LOGIN_ATTEMPTS.get(self.client_address[0], []) if now - stamp < 60]
        LOGIN_ATTEMPTS[self.client_address[0]] = attempts
        return len(attempts) >= 5

    def do_GET(self) -> None:
        if not self._is_allowed_client() or not self._is_allowed_host():
            self._json(403, {"error": "Local network access only."})
            return
        if self.path == "/":
            self._send(200, (ROOT / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/app.js":
            self._send(200, (ROOT / "dashboard.js").read_bytes(), "text/javascript; charset=utf-8")
        elif self.path == "/dashboard.css":
            self._send(200, (ROOT / "dashboard.css").read_bytes(), "text/css; charset=utf-8")
        elif self.path == "/api/status":
            if not self._authorized():
                self._json(401, {"error": "Login required."})
                return
            try:
                state = read_state()
                positions = [
                    {
                        "market": key,
                        "entry_price": position.get("entry_price"),
                        "last_price": position.get("last_price"),
                        "margin_usdt": position.get("margin_usdt"),
                        "leverage": position.get("leverage"),
                    }
                    for key, position in state["positions"].items()
                ]
                with CONTROL_LOCK:
                    enabled = CONTROL_ENABLED
                running = (
                    BOT_PROCESS is not None and BOT_PROCESS.poll() is None
                ) or runner_lock_is_held()
                self._json(
                    200,
                    {
                        "runner_running": running,
                        "accept_entries": enabled,
                        "cash_usdt": state.get("cash_usdt", 0.0),
                        "open_positions": positions,
                        "last_scan": state.get("last_scan"),
                    },
                )
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                self._json(500, {"error": f"Could not read paper state: {exc}"})
        else:
            self._json(404, {"error": "Not found."})

    def do_POST(self) -> None:
        if not self._is_allowed_client() or not self._is_allowed_host() or not self._is_same_origin():
            self._json(403, {"error": "Request rejected."})
            return
        try:
            data = self._request_json()
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._json(400, {"error": str(exc)})
            return
        if self.path == "/api/login":
            if self._login_rate_limited():
                self._json(429, {"error": "Too many login attempts. Wait one minute."})
                return
            LOGIN_ATTEMPTS.setdefault(self.client_address[0], []).append(time.time())
            if not hmac.compare_digest(str(data.get("token", "")), self.server.token):
                self._json(401, {"error": "Incorrect access password."})
                return
            session_id = secrets.token_urlsafe(32)
            SESSIONS[session_id] = time.time() + SESSION_MAX_AGE_SECONDS
            self._json(
                200,
                {"ok": True},
                {
                    "Set-Cookie": (
                        f"{SESSION_COOKIE}={session_id}; HttpOnly; SameSite=Strict; "
                        f"Path=/; Max-Age={SESSION_MAX_AGE_SECONDS}"
                    )
                },
            )
            return
        if not self._authorized():
            self._json(401, {"error": "Login required."})
            return
        if self.path == "/api/logout":
            session_id = self._session_id()
            if session_id:
                SESSIONS.pop(session_id, None)
            self._json(
                200,
                {"ok": True},
                {"Set-Cookie": f"{SESSION_COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"},
            )
        elif self.path == "/api/control":
            enabled = data.get("accept_entries")
            if not isinstance(enabled, bool):
                self._json(400, {"error": "accept_entries must be true or false."})
                return
            try:
                write_control(enabled)
                if enabled:
                    start_runner()
            except OSError as exc:
                write_control(False)
                self._json(500, {"error": f"Could not start the paper simulator: {exc}"})
                return
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "Not found."})

    def address_string(self) -> str:
        return self.client_address[0]


class DashboardServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], token: str):
        super().__init__(address, DashboardHandler)
        self.token = token


def main() -> None:
    if not BOT_PATH.is_file() or not (ROOT / "dashboard.html").is_file():
        raise RuntimeError("The futures dashboard files are incomplete.")
    token, new_token = load_or_create_token()
    write_control(False)
    stop_event = threading.Event()
    heartbeat = threading.Thread(target=heartbeat_loop, args=(stop_event,), daemon=True)
    heartbeat.start()
    server = DashboardServer(("0.0.0.0", PORT), token)
    addresses = sorted(address for address in local_addresses() if address not in {"127.0.0.1", "::1"})
    log.info("Futures paper dashboard listening on port %d (private networks only).", PORT)
    for address in addresses:
        log.info("Phone URL: http://%s:%d", address, PORT)
    log.info("PC URL: http://127.0.0.1:%d", PORT)
    if new_token:
        log.warning("One-time phone dashboard access password: %s", token)
        log.warning("Save this password securely; it is stored outside the repository at %s.", TOKEN_PATH)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        write_control(False)
        stop_event.set()
        server.server_close()
        log.info("Dashboard closed; new futures paper entries are paused.")


if __name__ == "__main__":
    main()
