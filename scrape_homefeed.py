import argparse
import json
import os
import re
from pathlib import Path
import sys
import time
from datetime import datetime
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


HOMEFEED_URL = "https://in.pinterest.com/homefeed/"
STATE_FILE = Path(__file__).resolve().parent / ".frame-state.json"


def cloud_api_request(path, token, api_url, payload=None):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        api_url.rstrip("/") + path,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="GET" if body is None else "POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"PinFrames API returned HTTP {error.code}: {detail}") from error
    except URLError as error:
        raise RuntimeError(f"Could not reach the PinFrames API: {error.reason}") from error


def cloud_scrape_is_due(settings, force=False):
    if force:
        return True
    last_scraped_at = settings.get("last_scraped_at")
    if not isinstance(last_scraped_at, str) or not last_scraped_at:
        return True
    try:
        timestamp = datetime.fromisoformat(last_scraped_at.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return True
    interval = settings.get("refresh_after_seconds", 86400)
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or interval < 60:
        interval = 86400
    return time.time() - timestamp >= interval


def record_successful_scrape():
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            state = {}
    except (OSError, ValueError):
        state = {}
    state["last_scrape_at"] = time.time()
    state["pinterest_signed_in"] = True
    temporary_file = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    temporary_file.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temporary_file.replace(STATE_FILE)


def sign_out_pinterest():
    user_data_dir = Path(".pinterest-browser-profile")
    if not user_data_dir.is_dir():
        return

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=True,
            viewport={"width": 1280, "height": 900},
        )
        context.clear_cookies()
        page = context.pages[0] if context.pages else context.new_page()
        for url in ("https://in.pinterest.com/", "https://www.pinterest.com/"):
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.evaluate("localStorage.clear(); sessionStorage.clear();")
            except PlaywrightTimeoutError:
                continue
        context.close()


def collect_image_urls(page):
    urls = page.evaluate(
        """() => {
          const values = [];
          for (const image of document.images) {
            values.push(image.currentSrc, image.src, image.getAttribute('data-src'));
            const srcset = image.getAttribute('srcset');
            if (srcset) {
              values.push(...srcset.split(',').map(candidate => candidate.trim().split(/\\s+/)[0]));
            }
          }
          return values.filter(Boolean);
        }"""
    )
    return {
        url.split("?")[0]
        for url in urls
        if urlsplit(url).hostname == "i.pinimg.com"
    }


def select_best_image_urls(urls):
    best_by_image = {}
    for url in urls:
        parts = urlsplit(url)
        path_parts = parts.path.strip("/").split("/")
        if len(path_parts) < 2:
            continue

        size = path_parts[0]
        if size == "originals":
            rank = float("inf")
        else:
            match = re.fullmatch(r"(\d+)x(?:\d+)?(?:_RS)?", size)
            if not match:
                continue
            rank = int(match.group(1))

        image_path = "/".join(path_parts[1:])
        candidate = parts._replace(query="", fragment="").geturl()
        previous = best_by_image.get(image_path)
        if previous is None or rank > previous[0]:
            best_by_image[image_path] = (rank, candidate)

    return {candidate for _, candidate in best_by_image.values()}


def main():
    parser = argparse.ArgumentParser(
        description="Collect image URLs visible in your Pinterest home feed."
    )
    parser.add_argument("--url", default=HOMEFEED_URL, help="Pinterest feed URL")
    parser.add_argument("--scrolls", type=int, default=10, help="Feed scrolls to load")
    parser.add_argument("--output", default="image-links.txt", help="Output text file")
    parser.add_argument(
        "--headed", action="store_true", help="Show the browser window (for signing in)"
    )
    parser.add_argument("--signout", action="store_true", help="Clear the saved Pinterest session")
    parser.add_argument(
        "--pause", type=float, default=1.5, help="Seconds to wait after each scroll"
    )
    parser.add_argument("--force", action="store_true", help="Ignore the cloud refresh age and scrape now")
    args = parser.parse_args()

    if args.scrolls < 0 or args.pause < 0:
        parser.error("--scrolls and --pause must be non-negative")

    if args.signout:
        sign_out_pinterest()
        return 0

    api_url = os.getenv("PINFRAMES_API_URL", "").strip()
    api_token = os.getenv("PINFRAMES_FEED_TOKEN", "").strip()
    cloud_enabled = bool(api_url or api_token)
    if cloud_enabled and not (api_url and api_token):
        print("Set both PINFRAMES_API_URL and PINFRAMES_FEED_TOKEN to sync with PinFrames.", file=sys.stderr)
        return 2

    if cloud_enabled and not args.headed:
        try:
            settings = cloud_api_request("/api/settings", api_token, api_url)
        except RuntimeError as error:
            print(error, file=sys.stderr)
            return 1
        if not cloud_scrape_is_due(settings, args.force):
            print("The saved PinFrames feed is still fresh; no scrape was started.")
            return 0

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image_urls = set()

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=".pinterest-browser-profile",
            headless=not args.headed,
            viewport={"width": 1440, "height": 1000},
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_function(
                    """() => Array.from(document.images).some(image => {
                      const sources = [image.currentSrc, image.src, image.getAttribute('srcset')];
                      return sources.some(source => source && source.includes('i.pinimg.com/'));
                    })""",
                    timeout=120000 if args.headed else 30000,
                )
            except PlaywrightTimeoutError:
                pass

            image_urls.update(collect_image_urls(page))
            for _ in range(args.scrolls):
                page.evaluate("window.scrollBy(0, Math.max(window.innerHeight * 0.85, 600))")
                page.wait_for_timeout(int(args.pause * 1000))
                image_urls.update(collect_image_urls(page))

            page_url = page.url
            page_title = page.title()
        except PlaywrightError as error:
            try:
                context.close()
            except PlaywrightError:
                pass
            if "closed" in str(error).lower():
                print(
                    "Pinterest window closed before the scrape finished. "
                    "Click Log in again and keep the window open until completion.",
                    file=sys.stderr,
                )
            else:
                print(f"Pinterest scrape failed: {error}", file=sys.stderr)
            return 1
        context.close()

    best_image_urls = select_best_image_urls(image_urls)
    if not best_image_urls:
        print("No supported Pinterest image URLs found; output file was not changed.")
        print(f"Current page: {page_title} ({page_url})")
        if image_urls:
            print("Pinterest images were detected, but their URL sizes were not recognized.")
        else:
            print("Make sure you are signed in and the home feed finished loading before continuing.")
        return 1

    ordered_images = sorted(best_image_urls)
    if cloud_enabled:
        try:
            cloud_api_request("/api/collect", api_token, api_url, {"images": ordered_images})
        except RuntimeError as error:
            print(error, file=sys.stderr)
            return 1

    output_path.write_text("\n".join(ordered_images) + "\n", encoding="utf-8")
    record_successful_scrape()
    print(f"Saved {len(best_image_urls)} highest-resolution image links to {output_path}")
    if cloud_enabled:
        print("Synced image links to the PinFrames cloud feed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())