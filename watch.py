#!/usr/bin/env python3
"""
SIH 2026 result watcher.

Loads the SIH website in a real (headless) browser, compares it with the
previous check, and sends a push notification when something new appears.
Notifications that mention results / shortlist / finale are sent as URGENT.

Runs every 10 minutes on GitHub Actions (see .github/workflows/watch.yml).

Settings (GitHub repo -> Settings -> Secrets and variables -> Actions):
  NTFY_TOPIC         (secret)  your private ntfy topic name, e.g. sih-jay-8f3k2q9x
  TELEGRAM_BOT_TOKEN (secret)  optional, if you also want Telegram messages
  TELEGRAM_CHAT_ID   (secret)  optional, used with TELEGRAM_BOT_TOKEN
  WATCH_URLS         (variable) optional, comma-separated pages to watch
                               (default: https://sih.gov.in/)
  ONLY_KEYWORDS      (variable) optional, "true" = only notify when the new text
                               mentions result/shortlist/finale etc.
"""

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

WATCH_URLS = [
    u.strip()
    for u in os.environ.get("WATCH_URLS", "").split(",")
    if u.strip()
] or ["https://sih.gov.in/"]

ONLY_KEYWORDS = os.environ.get("ONLY_KEYWORDS", "false").strip().lower() == "true"

KEYWORDS = re.compile(
    r"result|shortlist|short-list|selected|qualified|grand\s*finale|"
    r"phase[\s\-]*(1|i)\b|screening|nodal\s*cent|finalist",
    re.IGNORECASE,
)

STATE_DIR = Path("state")
STATE_FILE = STATE_DIR / "state.json"
FAIL_ALERT_AFTER = 6  # consecutive failed checks (= 1 hour) before warning you

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #
def notify(title: str, body: str, urgent: bool = False, click: str = "") -> None:
    sent = False

    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if topic:
        headers = {
            # ASCII-only header; the emoji comes from Tags instead
            "Title": title.encode("ascii", "ignore").decode().strip(),
            "Priority": "5" if urgent else "3",
            "Tags": "rotating_light" if urgent else "eyes",
        }
        if click:
            headers["Click"] = click
        req = urllib.request.Request(
            f"https://ntfy.sh/{urllib.parse.quote(topic)}",
            data=body.encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=20).read()
            sent = True
        except Exception as e:  # noqa: BLE001
            print(f"ntfy failed: {e}", file=sys.stderr)

    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if tg_token and tg_chat:
        text = f"{'🚨 ' if urgent else ''}{title}\n\n{body}"
        if click:
            text += f"\n\n{click}"
        data = urllib.parse.urlencode(
            {"chat_id": tg_chat, "text": text[:4000], "disable_web_page_preview": "true"}
        ).encode()
        try:
            urllib.request.urlopen(
                f"https://api.telegram.org/bot{tg_token}/sendMessage", data=data, timeout=20
            ).read()
            sent = True
        except Exception as e:  # noqa: BLE001
            print(f"telegram failed: {e}", file=sys.stderr)

    if not sent:
        print("WARNING: no notification channel worked (set NTFY_TOPIC).", file=sys.stderr)
    print(f"[notify] {title}\n{body}")


# --------------------------------------------------------------------------- #
# Page capture
# --------------------------------------------------------------------------- #
def normalise(s: str) -> str:
    """Collapse whitespace and mask digits so counters/clocks don't trigger alerts."""
    s = re.sub(r"\s+", " ", s).strip()
    return re.sub(r"\d+", "#", s)


def capture(page, url: str) -> dict:
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    try:
        page.wait_for_load_state("networkidle", timeout=20_000)
    except Exception:  # noqa: BLE001  (busy pages may never go fully idle)
        pass
    page.wait_for_timeout(3_000)

    text = page.inner_text("body")
    print(f"[capture] {url} -> title={page.title()!r}, {len(text)} chars")
    print("[capture] first 400 chars: " + text[:400].replace("\n", " / "))
    links = page.eval_on_selector_all(
        "a[href]",
        "els => els.map(e => [(e.innerText || e.title || '').trim(), e.href])",
    )

    lines = {}
    for raw in text.splitlines():
        raw = re.sub(r"\s+", " ", raw).strip()
        if len(raw) >= 3:
            lines.setdefault(normalise(raw), raw)

    link_map = {}
    for label, href in links:
        label = re.sub(r"\s+", " ", label or "").strip()
        if not href or href.startswith("javascript"):
            continue
        link_map.setdefault(normalise(f"{label} | {href}"), [label, href])

    return {"lines": lines, "links": link_map}


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    STATE_DIR.mkdir(exist_ok=True)
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    pages = state.setdefault("pages", {})
    first_run = not pages

    snapshots, errors = {}, []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(user_agent=USER_AGENT, viewport={"width": 1366, "height": 900})
        page = ctx.new_page()
        for url in WATCH_URLS:
            try:
                snapshots[url] = capture(page, url)
                if not snapshots[url]["lines"]:
                    raise RuntimeError("page loaded but had no text")
            except Exception as e:  # noqa: BLE001
                errors.append(f"{url}: {e}")
        browser.close()

    # ---- failures: warn once after an hour of consecutive failures -------- #
    if errors and not snapshots:
        state["fail_count"] = state.get("fail_count", 0) + 1
        print("Check failed:\n" + "\n".join(errors), file=sys.stderr)
        if state["fail_count"] == FAIL_ALERT_AFTER:
            notify(
                "SIH watcher: site unreachable for 1 hour",
                "Could not load the SIH website for the last 6 checks. "
                "It may be down or blocking GitHub. The watcher keeps trying.\n\n"
                + "\n".join(errors)[:500],
            )
        STATE_FILE.write_text(json.dumps(state))
        return 0
    if state.get("fail_count", 0) >= FAIL_ALERT_AFTER:
        notify("SIH watcher: site reachable again", "Checks are working normally again.")
    state["fail_count"] = 0

    # ---- compare with previous snapshot ----------------------------------- #
    for url, snap in snapshots.items():
        prev = pages.get(url)
        pages[url] = snap
        if prev is None:
            continue

        new_lines = [snap["lines"][k] for k in snap["lines"] if k not in prev["lines"]]
        new_links = [snap["links"][k] for k in snap["links"] if k not in prev["links"]]
        if not new_lines and not new_links:
            print(f"No change: {url}")
            continue

        hot_lines = [l for l in new_lines if KEYWORDS.search(l)]
        hot_links = [l for l in new_links if KEYWORDS.search(f"{l[0]} {l[1]}")]
        urgent = bool(hot_lines or hot_links)

        if ONLY_KEYWORDS and not urgent:
            print(f"Changed (no keywords, suppressed): {url}")
            continue

        show_lines = (hot_lines or new_lines)[:8]
        show_links = (hot_links or new_links)[:5]
        body = []
        if show_lines:
            body.append("New text:\n" + "\n".join(f"• {l[:160]}" for l in show_lines))
        if show_links:
            body.append("New links:\n" + "\n".join(f"• {t or '(link)'} → {h}" for t, h in show_links))

        click = show_links[0][1] if (urgent and show_links) else url
        title = (
            "🚨 SIH 2026: possible RESULT update!" if urgent else "SIH website changed"
        )
        notify(title, "\n\n".join(body) or "The page changed.", urgent=urgent, click=click)

    if first_run:
        n = sum(len(s["lines"]) for s in snapshots.values())
        notify(
            "SIH watcher is running ✅",
            f"Baseline saved ({n} lines of text). You'll be notified when the "
            f"SIH website changes. Checking every ~10 minutes.",
            click=WATCH_URLS[0],
        )

    if errors:
        print("Some pages failed:\n" + "\n".join(errors), file=sys.stderr)

    STATE_FILE.write_text(json.dumps(state))
    return 0


if __name__ == "__main__":
    sys.exit(main())
