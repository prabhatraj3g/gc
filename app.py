"""
GetCompanion Watcher — local web dashboard
--------------------------------------------
Same scraping engine as the original CLI script (companion_watcher / gc.py),
wrapped in a small Flask server so you get a live browser dashboard instead
of a terminal window: status cards per watched name, an add/remove list you
edit from the page itself, a recent-activity log, and browser push
notifications when someone comes online.

This still has to run ON YOUR PC (not a hosted/cloud app) because Playwright
drives a real headless Chromium browser locally. Think of it as swapping the
terminal UI for a browser UI — the checking logic underneath is unchanged.

SETUP (run once):
    pip install flask playwright --break-system-packages
    playwright install chromium

RUN:
    python app.py

Then open http://127.0.0.1:5000 in your browser and leave the terminal
running. The page updates itself every few seconds.
"""

import sys
import time
import json
import os
import csv
import threading
from collections import deque
from datetime import datetime

from flask import Flask, jsonify, request, render_template
from playwright.sync_api import sync_playwright, Error as PlaywrightError

URL = "https://www.getcompanion.in/services"

WATCH_LIST_CSV = "watch_list.csv"
STATE_FILE = "companion_watcher_state.json"
STATUS_WORDS = ("Online", "Offline", "Away", "Busy")
LINES_AFTER = 6
LINES_BEFORE = 2
SCAN_COOLDOWN_SECONDS = 0.5  # gap between back-to-back scans, just enough to avoid hammering the loop
GOTO_MAX_ATTEMPTS = 3
GOTO_RETRY_DELAY_SECONDS = 5
LOG_MAX_ENTRIES = 200

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Shared in-memory state — the background watcher thread writes to this,
# the Flask routes read from it. Guarded by STATE_LOCK throughout.
# ---------------------------------------------------------------------------
STATE_LOCK = threading.Lock()
STATE = {
    "people": {},          # name -> {"status": str|None, "matched": bool, "changed_at": iso str|None}
    "last_check_started": None,
    "last_check_finished": None,
    "is_checking": False,
    "is_running": True,
    "last_error": None,
}
LOG = deque(maxlen=LOG_MAX_ENTRIES)  # list of {"time": iso, "message": str}
RUNNING_EVENT = threading.Event()    # cleared = paused; the loop blocks on this until Start is pressed
RUNNING_EVENT.set()


def log_event(message):
    entry = {"time": datetime.now().isoformat(timespec="seconds"), "message": message}
    LOG.appendleft(entry)
    print(f"[{entry['time']}] {message}")


# ---------------------------------------------------------------------------
# Watch list persistence (same CSV format as the original script, so existing
# watch_list.csv files just work)
# ---------------------------------------------------------------------------

def ensure_watch_list_csv_exists():
    if not os.path.exists(WATCH_LIST_CSV):
        with open(WATCH_LIST_CSV, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["name"])


def load_watch_names():
    ensure_watch_list_csv_exists()
    names = []
    try:
        with open(WATCH_LIST_CSV, "r", newline="") as f:
            reader = csv.DictReader(f)
            name_field = None
            for field in reader.fieldnames or []:
                if field.strip().lower() == "name":
                    name_field = field
                    break
            if name_field is None:
                return []
            for row in reader:
                value = (row.get(name_field) or "").strip()
                if value:
                    names.append(value)
    except Exception as e:
        log_event(f"Error reading {WATCH_LIST_CSV}: {e}")
    return names


def save_watch_names(names):
    with open(WATCH_LIST_CSV, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["name"])
        for name in names:
            writer.writerow([name])


def load_previous_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state_to_disk(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# Scraping engine (same approach as the CLI version: scroll + search as you
# go, stop early once every watched name is resolved)
# ---------------------------------------------------------------------------

def get_visible_lines(page):
    text = page.inner_text("body")
    return [line.strip() for line in text.split("\n") if line.strip()]


def find_status_in_lines(lines, name):
    indices = [i for i, line in enumerate(lines) if line == name]
    if not indices:
        return None, False
    for idx in indices:
        window = lines[max(0, idx - LINES_BEFORE): idx + LINES_AFTER + 1]
        for w in window:
            for word in STATUS_WORDS:
                if w.lower() == word.lower():
                    return word, True
    return None, True


def scan_for_statuses(page, watch_names, max_scrolls=60, step_px=1400, pause_ms=500):
    results = {name: {"status": None, "matched": False} for name in watch_names}
    remaining = set(watch_names)
    scroll_y = 0
    stable_count = 0
    viewport_height = page.viewport_size["height"]

    for _ in range(max_scrolls):
        page.evaluate(f"window.scrollTo(0, {scroll_y})")
        page.wait_for_timeout(pause_ms)
        lines = get_visible_lines(page)

        for name in list(remaining):
            status, matched = find_status_in_lines(lines, name)
            if matched:
                results[name]["matched"] = True
            if status is not None:
                results[name]["status"] = status
                remaining.discard(name)

        if not remaining:
            break

        total_height = page.evaluate("document.body.scrollHeight")
        current_scroll = page.evaluate("window.scrollY")
        scroll_y += step_px

        if current_scroll + viewport_height >= total_height:
            stable_count += 1
            if stable_count >= 2:
                break
        else:
            stable_count = 0

    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(300)
    return results


def goto_with_retry(page, url, attempts=GOTO_MAX_ATTEMPTS, delay=GOTO_RETRY_DELAY_SECONDS):
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            page.goto(url, wait_until="load", timeout=60000)
            page.wait_for_timeout(3000)
            return
        except PlaywrightError as e:
            last_error = e
            if attempt < attempts:
                time.sleep(delay)
    raise last_error


def run_check(browser, previous_state, watch_names):
    page = browser.new_page(viewport={"width": 1440, "height": 2400})
    try:
        goto_with_retry(page, URL)
        scan_results = scan_for_statuses(page, watch_names)
    finally:
        page.close()

    current_state = {}
    newly_online = []
    newly_offline = []
    online_now = []

    with STATE_LOCK:
        for name in watch_names:
            result = scan_results.get(name, {"status": None, "matched": False})
            status, matched = result["status"], result["matched"]
            current_state[name] = status
            prev = previous_state.get(name)

            existing = STATE["people"].get(name, {})
            changed_at = existing.get("changed_at")
            if status != existing.get("status"):
                changed_at = datetime.now().isoformat(timespec="seconds")

            STATE["people"][name] = {
                "status": status,
                "matched": matched,
                "changed_at": changed_at,
            }

            if status == "Online":
                online_now.append(name)
                if prev != "Online":
                    newly_online.append(name)
            elif prev == "Online" and status != "Online":
                newly_offline.append(name)

        # drop names no longer on the watch list
        for stale in list(STATE["people"].keys()):
            if stale not in watch_names:
                del STATE["people"][stale]

    if newly_online:
        if len(newly_online) == 1:
            log_event(f"🟢 {newly_online[0]} came online")
        else:
            log_event(f"🟢 {len(newly_online)} people came online: {', '.join(newly_online)}")

    if newly_offline:
        if len(newly_offline) == 1:
            log_event(f"⚪ {newly_offline[0]} went offline")
        else:
            log_event(f"⚪ {len(newly_offline)} people went offline: {', '.join(newly_offline)}")

    # Always log a one-line summary so the activity panel reflects every
    # scan, not just the ones where someone's status changed.
    if not watch_names:
        log_event("Scan complete — watch list is empty.")
    elif online_now:
        log_event(f"Scan complete — {len(online_now)} online: {', '.join(online_now)}")
    else:
        log_event(f"Scan complete — nobody online ({len(watch_names)} watched).")

    return current_state


# ---------------------------------------------------------------------------
# Background watcher thread
# ---------------------------------------------------------------------------

def watcher_loop():
    previous_state = load_previous_state()
    log_event("Watcher started.")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            while True:
                if not RUNNING_EVENT.is_set():
                    # Paused — block here (no CPU spin) until Start sets the event again.
                    RUNNING_EVENT.wait()
                    continue

                try:
                    try:
                        connected = browser.is_connected()
                    except Exception:
                        connected = False
                    if not connected:
                        log_event("Browser disconnected, relaunching...")
                        browser = playwright.chromium.launch(headless=True)

                    watch_names = load_watch_names()
                    with STATE_LOCK:
                        STATE["is_checking"] = True
                        STATE["last_check_started"] = datetime.now().isoformat(timespec="seconds")
                        STATE["last_error"] = None

                    previous_state = run_check(browser, previous_state, watch_names)
                    save_state_to_disk(previous_state)

                    with STATE_LOCK:
                        STATE["is_checking"] = False
                        STATE["last_check_finished"] = datetime.now().isoformat(timespec="seconds")
                except Exception as e:
                    with STATE_LOCK:
                        STATE["is_checking"] = False
                        STATE["last_error"] = str(e)
                    log_event(f"Error during check: {e}")

                time.sleep(SCAN_COOLDOWN_SECONDS)  # then straight back into the next scan
        finally:
            try:
                if browser.is_connected():
                    browser.close()
            except Exception:
                pass  # driver may already be gone (e.g. process shutting down)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    with STATE_LOCK:
        people = [
            {"name": name, **info}
            for name, info in STATE["people"].items()
        ]
        payload = {
            "people": people,
            "online_count": sum(1 for p in people if p["status"] == "Online"),
            "last_check_started": STATE["last_check_started"],
            "last_check_finished": STATE["last_check_finished"],
            "is_checking": STATE["is_checking"],
            "is_running": STATE["is_running"],
            "last_error": STATE["last_error"],
        }
    return jsonify(payload)


@app.route("/api/log")
def api_log():
    return jsonify(list(LOG))


@app.route("/api/check-now", methods=["POST"])
def api_check_now():
    with STATE_LOCK:
        if not STATE["is_running"]:
            return jsonify({"error": "Watcher is stopped. Start it first."}), 400
        if STATE["is_checking"]:
            return jsonify({"error": "A scan is already in progress."}), 409
    # Scans already run back-to-back, so there's nothing to "kick" — the
    # next one starts within SCAN_COOLDOWN_SECONDS on its own.
    return jsonify({"ok": True})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    with STATE_LOCK:
        if not STATE["is_running"]:
            return jsonify({"error": "Watcher is already stopped."}), 400
        STATE["is_running"] = False
    RUNNING_EVENT.clear()
    log_event("⏹ Watcher stopped.")
    return jsonify({"ok": True})


@app.route("/api/start", methods=["POST"])
def api_start():
    with STATE_LOCK:
        if STATE["is_running"]:
            return jsonify({"error": "Watcher is already running."}), 400
        STATE["is_running"] = True
    RUNNING_EVENT.set()
    log_event("▶ Watcher resumed.")
    return jsonify({"ok": True})


@app.route("/api/watchlist", methods=["GET"])
def api_watchlist_get():
    return jsonify(load_watch_names())


@app.route("/api/watchlist", methods=["POST"])
def api_watchlist_add():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name is required."}), 400

    names = load_watch_names()
    if name in names:
        return jsonify({"error": f"{name} is already on the watch list."}), 400

    names.append(name)
    save_watch_names(names)
    log_event(f"Added \"{name}\" to the watch list.")
    return jsonify({"ok": True, "names": names})


@app.route("/api/watchlist/<path:name>", methods=["DELETE"])
def api_watchlist_remove(name):
    names = load_watch_names()
    if name not in names:
        return jsonify({"error": f"{name} isn't on the watch list."}), 404

    names = [n for n in names if n != name]
    save_watch_names(names)
    with STATE_LOCK:
        STATE["people"].pop(name, None)
    log_event(f"Removed \"{name}\" from the watch list.")
    return jsonify({"ok": True, "names": names})


if __name__ == "__main__":
    threading.Thread(target=watcher_loop, daemon=True).start()
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000")),
        debug=False,
    )
