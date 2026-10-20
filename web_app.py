"""Local browser interface for the Koruxa optimizer."""

import json
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
import subprocess
import sys
import os
import requests
from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parent
DASHBOARD = ROOT / "dashboard.html"
OPTIMIZER = ROOT / "optimizer.py"
REPORT = ROOT / "output" / "optimizer-results.json"
HOST = "127.0.0.1"
PORT = 8765
MAX_BODY = 16_384
GAME_BASE_URL = "https://koruxa.com"
JOB_LOCK = threading.Lock()
JOB = {"running": False, "state": "idle", "output": "", "returncode": None,
       "started_at": None, "finished_at": None}


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def fetch_characters(username, password):
    """Read the signed-in account's character choices from the game page."""
    game_session = requests.Session()
    game_session.get(f"{GAME_BASE_URL}/login", timeout=30)
    login_response = game_session.post(
        f"{GAME_BASE_URL}/login",
        data={"username": username, "password": password},
        allow_redirects=False,
        timeout=30,
    )
    if login_response.status_code not in (302, 303):
        raise ValueError("Koruxa login failed. Check your username and password.")

    page = game_session.get(f"{GAME_BASE_URL}/characters", timeout=30)
    if page.status_code != 200:
        raise ValueError("Could not load the character selection page from Koruxa.")
    soup = BeautifulSoup(page.text, "html.parser")
    characters = []
    seen_ids = set()
    for form in soup.select('form[action="/characters/select"]'):
        field = form.find("input", attrs={"name": "character_id"})
        character_id = str(field.get("value") or "").strip() if field else ""
        if not character_id.isdigit() or character_id in seen_ids:
            continue
        card = form.find_parent(class_="char-card")
        heading = card.find(["h2", "h3"]) if card else None
        name = heading.get_text(" ", strip=True) if heading else f"Character {len(characters) + 1}"
        characters.append({"id": character_id, "name": name})
        seen_ids.add(character_id)
    if not characters:
        raise ValueError("No characters were found on your Koruxa account.")
    return characters


class Handler(BaseHTTPRequestHandler):
    server_version = "KoruxaOptimizerLocal/1.0"

    def log_message(self, fmt, *args):
        # Avoid logging request bodies or account data; keep the local console quiet.
        return

    def _host_is_local(self):
        return self.headers.get("Host", "").lower() in {
            f"127.0.0.1:{PORT}", f"localhost:{PORT}"
        }

    def _send(self, status, body, content_type="application/json; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status, value):
        self._send(status, json_bytes(value))

    def do_GET(self):
        if not self._host_is_local():
            return self._send_json(403, {"error": "Local requests only."})
        path = urlsplit(self.path).path
        if path in ("/", "/dashboard.html"):
            try:
                body = DASHBOARD.read_bytes()
            except OSError:
                return self._send_json(500, {"error": "Dashboard file is unavailable."})
            return self._send(200, body, "text/html; charset=utf-8")
        if path == "/api/status":
            with JOB_LOCK:
                return self._send_json(200, dict(JOB))
        if path == "/api/report":
            try:
                return self._send(200, REPORT.read_bytes())
            except OSError:
                return self._send_json(404, {"error": "No optimizer report is available yet."})
        return self._send_json(404, {"error": "Not found."})

    def do_POST(self):
        if not self._host_is_local():
            return self._send_json(403, {"error": "Local requests only."})
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
            return self._send_json(403, {"error": "Cross-origin requests are not allowed."})
        path = urlsplit(self.path).path
        if path not in {"/api/run", "/api/characters"}:
            return self._send_json(404, {"error": "Not found."})
        if self.headers.get_content_type() != "application/json":
            return self._send_json(415, {"error": "Send the run options as JSON."})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size <= 0 or size > MAX_BODY:
                raise ValueError("Invalid request size.")
            payload = json.loads(self.rfile.read(size))
        except (ValueError, json.JSONDecodeError) as exc:
            return self._send_json(400, {"error": str(exc) or "Invalid JSON."})

        username = str(payload.get("username", "")).strip()
        password = str(payload.get("password", ""))
        character_id = str(payload.get("character_id", "")).strip()
        if not username or not password or (path == "/api/run" and not character_id.isdigit()):
            return self._send_json(400, {"error": "Enter your Koruxa username and password, then choose a character."})
        if len(username) > 200 or len(password) > 1024 or len(character_id) > 20:
            return self._send_json(400, {"error": "One or more login fields are too long."})

        if path == "/api/characters":
            try:
                characters = fetch_characters(username, password)
            except ValueError as exc:
                return self._send_json(401, {"error": str(exc)})
            except requests.RequestException:
                return self._send_json(502, {"error": "Could not reach Koruxa. Try again in a moment."})
            return self._send_json(200, {"characters": characters})

        with JOB_LOCK:
            if JOB["running"]:
                return self._send_json(409, {"error": "An optimizer run is already in progress."})
            JOB.update({"running": True, "state": "starting", "output": "",
                        "returncode": None, "started_at": time.time(), "finished_at": None})
        credentials = {"username": username, "password": password, "character_id": character_id}
        refresh_market = bool(payload.get("refresh_market"))
        threading.Thread(target=self._run_optimizer, args=(credentials, refresh_market), daemon=True).start()
        return self._send_json(202, {"started": True})

    @staticmethod
    def _run_optimizer(credentials, refresh_market):
        args = [sys.executable, "-u", str(OPTIMIZER), "--credentials-stdin",
                "--json-output", str(REPORT)]
        if refresh_market:
            args.append("--refresh-market")
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        process = None
        try:
            REPORT.parent.mkdir(parents=True, exist_ok=True)
            process = subprocess.Popen(
                args, cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                bufsize=1, env=env,
            )
            process.stdin.write(json.dumps(credentials) + "\n")
            process.stdin.close()
            credentials.clear()
            with JOB_LOCK:
                JOB["state"] = "running"
            for line in process.stdout:
                with JOB_LOCK:
                    JOB["output"] = (JOB["output"] + line)[-16000:]
            code = process.wait()
            with JOB_LOCK:
                JOB["returncode"] = code
                JOB["state"] = "complete" if code == 0 else "failed"
                JOB["running"] = False
                JOB["finished_at"] = time.time()
        except Exception as exc:
            credentials.clear()
            if process and process.poll() is None:
                process.kill()
            with JOB_LOCK:
                JOB["output"] = (JOB["output"] + f"\nLocal service error: {exc}\n")[-16000:]
                JOB["state"] = "failed"
                JOB["returncode"] = -1
                JOB["running"] = False
                JOB["finished_at"] = time.time()


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Koruxa Optimizer is running at http://{HOST}:{PORT}")
    print("Keep this window open while using the dashboard. Press Ctrl+C to stop it.")
    threading.Timer(0.7, lambda: webbrowser.open(f"http://{HOST}:{PORT}")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping the local optimizer service...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
