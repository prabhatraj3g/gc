# Signal — presence watcher (web dashboard)

A local web dashboard version of the GetCompanion watcher. Same scraping
engine as the original script (Playwright headless Chromium, scroll-and-scan
status detection), but instead of a terminal window and a CSV you hand-edit,
you get a live page in your browser with status cards, an add/remove list,
an activity log, and browser notifications.

This still runs **on your own PC** — it's not a hosted/cloud app. Playwright
needs a real local browser to drive, so `app.py` is a small local server
(Flask) that you start and leave running, same as the old script.

## Setup (once)

```
pip install -r requirements.txt --break-system-packages
playwright install chromium
```

## Run

```
python app.py
```

Then open **http://127.0.0.1:5000** in your browser and leave the terminal
running in the background. The page polls itself every few seconds — no
need to refresh manually.

## Using it

- **Add a name**: type it into the box at the top of the watch list and hit
  Add. It's saved to `watch_list.csv` immediately (same file format as
  before, so an existing CSV from the old script will just work).
- **Remove a name**: click the ✕ on its card.
- **Browser notifications**: the first time you click anywhere on the page
  or add a name, your browser will ask for notification permission. Allow
  it to get OS-level popups when someone comes online, in addition to the
  in-page toast that always shows.
- **Radar dial** (top center): shows how many watched people are online
  right now.
- **Activity panel** (right side): a running log of scans, additions,
  removals, and who came online, newest first.

## Notes

- Scans run back-to-back: as soon as one finishes, the next one starts
  after a `SCAN_COOLDOWN_SECONDS` (0.5s) gap. There's no fixed interval
  anymore — cadence is just however long one scan takes.
- `watch_list.csv` and `companion_watcher_state.json` are created
  automatically in the same folder as `app.py`.
- Closing the browser tab does **not** stop the watcher — only closing the
  terminal (or Ctrl+C) does, since the scanning happens server-side.
