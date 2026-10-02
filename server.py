import argparse
import json
import subprocess
import sys
from threading import Lock
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parent
IMAGE_LIST = ROOT / "image-links.txt"
STATE_FILE = ROOT / ".frame-state.json"
MISSING_PATH = ROOT / "__not_found__"
DEFAULT_REFRESH_AFTER = 86400
MAX_REFRESH_AFTER = 365 * 86400
IMAGE_LIST_LOCK = Lock()


def load_state():
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(updates):
    state = load_state()
    state.update(updates)
    temporary_file = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    temporary_file.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temporary_file.replace(STATE_FILE)


def refresh_is_due(state, now=None):
    last_scrape_at = state.get("last_scrape_at")
    if not isinstance(last_scrape_at, (int, float)):
        return True
    now = time.time() if now is None else now
    interval = state.get("refresh_after_seconds", DEFAULT_REFRESH_AFTER)
    if not isinstance(interval, (int, float)) or interval < 60:
        interval = DEFAULT_REFRESH_AFTER
    return now - last_scrape_at >= interval


def refresh_at_startup():
    state = load_state()
    if state.get("pinterest_signed_in") is False:
        print("Pinterest is signed out; skipping the startup scrape.")
        return
    if not refresh_is_due(state):
        print("Last scrape is still within the selected age; using saved image links.")
        return

    profile = ROOT / ".pinterest-browser-profile"
    if not profile.is_dir():
        print("No saved Pinterest session yet. Run `python scrape_homefeed.py --headed` once to sign in.")
        return

    print("The last scrape is older than the selected age. Scraping before the frame starts...")
    result = subprocess.run([sys.executable, str(ROOT / "scrape_homefeed.py")], cwd=ROOT)
    if result.returncode:
        print("Scrape failed; keeping the existing image list.")


def consume_image(image_url, image_list=IMAGE_LIST):
    parsed = urlsplit(image_url)
    if parsed.scheme != "https" or parsed.hostname != "i.pinimg.com":
        raise ValueError("Only Pinterest image URLs can be consumed")

    with IMAGE_LIST_LOCK:
        lines = image_list.read_text(encoding="utf-8").splitlines()
        try:
            lines.remove(image_url)
        except ValueError:
            return False, len(lines)

        temporary_file = image_list.with_name(image_list.name + ".tmp")
        temporary_file.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        temporary_file.replace(image_list)
        return True, len(lines)


def clear_images(image_list=IMAGE_LIST):
    with IMAGE_LIST_LOCK:
        temporary_file = image_list.with_name(image_list.name + ".tmp")
        temporary_file.write_text("", encoding="utf-8")
        temporary_file.replace(image_list)


def run_scraper(headed=False):
    profile = ROOT / ".pinterest-browser-profile"
    state = load_state()
    if not headed and state.get("pinterest_signed_in") is False:
        return False, "Pinterest is signed out. Use Log in first.", 0
    if not headed and not profile.is_dir():
        return False, "No saved Pinterest session. Run `python scrape_homefeed.py --headed` first.", 0

    command = [sys.executable, str(ROOT / "scrape_homefeed.py")]
    if headed:
        command.append("--headed")
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=900,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, str(error), 0

    if result.returncode:
        error = result.stderr or result.stdout or "Scrape failed."
        if "TargetClosedError" in error or "closed before the scrape finished" in error:
            error = "Pinterest window closed early. Log in again and keep the window open until the scrape completes."
        else:
            error = error.strip().splitlines()[-1][-300:]
        return False, error, 0
    count = len([line for line in IMAGE_LIST.read_text(encoding="utf-8").splitlines() if line.strip()])
    return True, "Scrape complete.", count


class FrameHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def translate_path(self, path):
        request_path = unquote(urlsplit(path).path).lstrip("/")
        parts = Path(request_path).parts
        if any(part in {".", ".."} for part in parts):
            return str(MISSING_PATH)
        if parts == ("image-links.txt",):
            return str(IMAGE_LIST)
        if parts and parts[0] == "display":
            candidate = ROOT.joinpath(*parts).resolve()
            if candidate == ROOT / "display" or ROOT / "display" in candidate.parents:
                return str(candidate)
        return str(MISSING_PATH)

    def do_GET(self):
        if urlsplit(self.path).path == "/api/config":
            self.send_json({"requires_feed_token": False})
            return
        if urlsplit(self.path).path == "/api/auth":
            state = load_state()
            signed_in = state.get("pinterest_signed_in")
            if signed_in is None:
                signed_in = bool(state.get("last_scrape_at")) and (ROOT / ".pinterest-browser-profile").is_dir()
            self.send_json({"signed_in": bool(signed_in)})
            return
        if urlsplit(self.path).path == "/api/images":
            state = load_state()
            images = [line for line in IMAGE_LIST.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.send_json({"images": images, "last_scraped_at": state.get("last_scrape_at")})
            return
        if urlsplit(self.path).path == "/api/settings":
            state = load_state()
            self.send_json({
                "refresh_after_seconds": state.get("refresh_after_seconds", DEFAULT_REFRESH_AFTER)
            })
            return
        if urlsplit(self.path).path == "/":
            self.send_response(302)
            self.send_header("Location", "/display/")
            self.end_headers()
            return
        super().do_GET()

    def do_POST(self):
        request_path = urlsplit(self.path).path
        if request_path not in {"/api/consume", "/api/settings", "/api/clear", "/api/scrape", "/api/login", "/api/logout"}:
            self.send_error(404)
            return

        if request_path == "/api/clear":
            try:
                clear_images(IMAGE_LIST)
                save_state({"last_scrape_at": None})
            except OSError as error:
                self.send_json({"ok": False, "error": str(error)}, status=500)
                return
            self.send_json({"ok": True, "remaining": 0})
            return

        if request_path == "/api/scrape":
            ok, result, count = run_scraper()
            self.send_json(
                {"ok": ok, "message": result, "count": count},
                status=200 if ok else 409 if "No saved Pinterest session" in result else 500,
            )
            return

        if request_path == "/api/login":
            ok, result, count = run_scraper(headed=True)
            self.send_json(
                {"ok": ok, "message": result, "count": count},
                status=200 if ok else 500,
            )
            return

        if request_path == "/api/logout":
            try:
                result = subprocess.run(
                    [sys.executable, str(ROOT / "scrape_homefeed.py"), "--signout"],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                if result.returncode:
                    raise RuntimeError(result.stderr or result.stdout or "Could not sign out")
                save_state({"pinterest_signed_in": False})
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
                self.send_json({"ok": False, "error": str(error)}, status=500)
                return
            self.send_json({"ok": True})
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0 or content_length > 4096:
                raise ValueError("Invalid request size")
            payload = json.loads(self.rfile.read(content_length))
            if not isinstance(payload, dict):
                raise ValueError("Request body must be a JSON object")
            if request_path == "/api/settings":
                refresh_after = payload.get("refresh_after_seconds")
                if isinstance(refresh_after, bool) or not isinstance(refresh_after, int):
                    raise ValueError("Refresh age must be an integer number of seconds")
                if not 60 <= refresh_after <= MAX_REFRESH_AFTER:
                    raise ValueError("Refresh age must be between 1 minute and 365 days")
                save_state({"refresh_after_seconds": refresh_after})
                self.send_json({"ok": True, "refresh_after_seconds": refresh_after})
                return

            image_url = payload.get("image")
            if not isinstance(image_url, str):
                raise ValueError("Missing image URL")
            removed, remaining = consume_image(image_url, IMAGE_LIST)
        except (ValueError, json.JSONDecodeError) as error:
            self.send_error(400, str(error))
            return
        except OSError as error:
            self.send_error(500, str(error))
            return

        self.send_json({"removed": removed, "remaining": remaining})

    def send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description="Serve the Pinterest image frame locally.")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    refresh_at_startup()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), FrameHandler)
    print(f"Serving FRAME at http://127.0.0.1:{args.port}/")
    server.serve_forever()


if __name__ == "__main__":
    main()