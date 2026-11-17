#!/usr/bin/env python3
"""
SIH 2026 result watcher  (runs every ~10 min on GitHub Actions, free)

Two sources, checked on every run:

1. NEWS  - Google News + Bing News searches for "Smart India Hackathon 2026".
           When results come out, colleges and news sites post
           "team selected for SIH 2026 Grand Finale" within hours. New articles
           that mention result / shortlist / finale trigger an URGENT alert.

2. SITE  - https://sih.gov.in rendered in a headless browser. NOTE: the site's
           firewall currently answers GitHub's (overseas) servers with
           "403 Forbidden", so this part only works if they ever unblock it.
           Blocked responses are detected and never treated as a change.

Alerts go to ntfy (repo secret NTFY_TOPIC) and optionally Telegram
(secrets TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID).

Optional repo variables:
  WATCH_URLS     comma-separated pages to watch (default https://sih.gov.in/)
  ONLY_KEYWORDS  "true" = for the SITE, only alert when new text mentions results
"""

import email.utils
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

WATCH_URLS = [u.strip() for u in os.environ.get("WATCH_URLS", "").split(",") if u.strip()] or [
    "https://sih.gov.in/"
]
ONLY_KEYWORDS = os.environ.get("ONLY_KEYWORDS", "false").strip().lower() == "true"

NEWS_FEEDS = [
    "https://news.google.com/rss/search?q=%22Smart+India+Hackathon+2026%22&hl=en-IN&gl=IN&ceid=IN:en",
    "https://news.google.com/rss/search?q=%22SIH+2026%22+result+OR+shortlisted+OR+%22grand+finale%22&hl=en-IN&gl=IN&ceid=IN:en",
    "https://news.google.com/rss/search?q=%22Smart+India+Hackathon%22+2026+%22grand+finale%22&hl=en-IN&gl=IN&ceid=IN:en",
    "https://www.bing.com/news/search?q=%22Smart+India+Hackathon+2026%22&format=rss",
]

RESULT_WORDS = re.compile(
    r"result|shortlist|short-list|selected|qualif|grand\s*finale|finalist|declared|"
    r"announced|phase[\s\-]*(1|i)\b|screening|nodal\s*cent",
    re.IGNORECASE,
)
SIH_WORDS = re.compile(r"\bSIH\b|smart\s+india\s+hackathon", re.IGNORECASE)
# College-level rounds also use "shortlisted" - ignore those.
NOISE_WORDS = re.compile(r"internal|pre-?\s?qualifier|ideathon|in-house|campus\s+round", re.IGNORECASE)
NEWS_MAX_AGE_DAYS = 4

BLOCKED = re.compile(
    r"^\s*(40[0-9]|429|5\d\d)\b|forbidden|access denied|request blocked|"
    r"application-gateway|captcha|are you a robot",
    re.IGNORECASE,
)

STATE_DIR = Path("state")
STATE_FILE = STATE_DIR / "state.json"
FAIL_ALERT_AFTER = 6  # consecutive failed news checks (= ~1 hour) before warning

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
        text = f"{'🚨 ' if urgent else ''}{title}\n\n{body}" + (f"\n\n{click}" if click else "")
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
# 1. News feeds
# --------------------------------------------------------------------------- #
def fetch_feed(url: str) -> list[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    raw = urllib.request.urlopen(req, timeout=30).read()
    root = ET.fromstring(raw)
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        desc = re.sub(r"<[^>]+>", " ", it.findtext("description") or "")
        src = (it.findtext("source") or "").strip()
        pub = None
        try:
            pub = email.utils.parsedate_to_datetime(it.findtext("pubDate") or "").timestamp()
        except Exception:  # noqa: BLE001
            pass
        if title:
            items.append({"title": title, "link": link, "desc": desc, "source": src, "pub": pub})
    return items


def news_key(item: dict) -> str:
    # Google appends " - Source" to titles; strip it so the same story dedupes across feeds
    t = re.sub(r"\s+-\s+[^-]{2,60}$", "", item["title"])
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def is_result_news(item: dict) -> bool:
    text = f"{item['title']} {item['desc']}"
    if not SIH_WORDS.search(text) or not RESULT_WORDS.search(item["title"] + " " + item["desc"][:300]):
        return False
    if NOISE_WORDS.search(item["title"]):
        return False
    # Skip old editions unless 2026 is also mentioned
    if re.search(r"20(2[0-5])", text) and "2026" not in text:
        return False
    if item["pub"] and time.time() - item["pub"] > NEWS_MAX_AGE_DAYS * 86400:
        return False
    return True


def check_news(state: dict) -> str:
    seen = state.get("news_seen")
    baseline = seen is None
    seen = set(seen or [])

    items, ok = [], 0
    for url in NEWS_FEEDS:
        try:
            got = fetch_feed(url)
            items += got
            ok += 1
            print(f"[news] {len(got):3d} items  {url[:90]}")
        except Exception as e:  # noqa: BLE001
            print(f"[news] FAILED {url[:90]}: {e}", file=sys.stderr)

    if ok == 0:
        state["news_fail"] = state.get("news_fail", 0) + 1
        if state["news_fail"] == FAIL_ALERT_AFTER:
            notify(
                "SIH watcher: news feeds unreachable",
                "Google/Bing News could not be reached for ~1 hour. The watcher keeps trying.",
            )
        return "news feeds unreachable"
    state["news_fail"] = 0

    fresh = []
    for it in items:
        k = news_key(it)
        if k in seen:
            continue
        seen.add(k)
        if not baseline and is_result_news(it):
            fresh.append(it)

    state["news_seen"] = list(seen)[-3000:]

    if fresh:
        lines = [
            f"• {it['title'][:150]}" + (f"  ({it['source']})" if it["source"] and it["source"] not in it["title"] else "")
            for it in fresh[:6]
        ]
        notify(
            "🚨 SIH 2026: result news spotted!",
            "New articles about SIH 2026 results / finale:\n"
            + "\n".join(lines)
            + "\n\nVerify on sih.gov.in and with your college SPOC.",
            urgent=True,
            click=fresh[0]["link"] or "https://sih.gov.in/",
        )
    else:
        print("[news] nothing new about results")
    return f"news OK ({len(items)} articles from {ok}/{len(NEWS_FEEDS)} feeds)"


# --------------------------------------------------------------------------- #
# 2. SIH website (headless browser)
# --------------------------------------------------------------------------- #
def normalise(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return re.sub(r"\d+", "#", s)  # mask counters/clocks


def capture(page, url: str) -> dict:
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    try:
        page.wait_for_load_state("networkidle", timeout=20_000)
    except Exception:  # noqa: BLE001
        pass
    page.wait_for_timeout(3_000)

    title = page.title()
    text = page.inner_text("body")
    print(f"[site] {url} -> title={title!r}, {len(text)} chars")
    if len(text) < 2000 and (BLOCKED.search(title) or BLOCKED.search(text[:300])):
        raise RuntimeError(f"blocked by site firewall ({title or text[:60]!r})")

    links = page.eval_on_selector_all(
        "a[href]", "els => els.map(e => [(e.innerText || e.title || '').trim(), e.href])"
    )
    lines = {}
    for raw in text.splitlines():
        raw = re.sub(r"\s+", " ", raw).strip()
        if len(raw) >= 3:
            lines.setdefault(normalise(raw), raw)
    link_map = {}
    for label, href in links:
        label = re.sub(r"\s+", " ", label or "").strip()
        if href and not href.startswith("javascript"):
            link_map.setdefault(normalise(f"{label} | {href}"), [label, href])
    if not lines:
        raise RuntimeError("page loaded but had no text")
    return {"lines": lines, "links": link_map}


def check_site(state: dict) -> str:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "site check skipped (playwright missing)"

    pages = state.setdefault("pages_v2", {})
    snapshots, errors = {}, []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(user_agent=USER_AGENT, viewport={"width": 1366, "height": 900})
        page = ctx.new_page()
        for url in WATCH_URLS:
            try:
                snapshots[url] = capture(page, url)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{url}: {e}")
                print(f"[site] FAILED {url}: {e}", file=sys.stderr)
        browser.close()

    for url, snap in snapshots.items():
        prev = pages.get(url)
        pages[url] = snap
        if prev is None:
            print(f"[site] baseline saved for {url}")
            continue
        new_lines = [snap["lines"][k] for k in snap["lines"] if k not in prev["lines"]]
        new_links = [snap["links"][k] for k in snap["links"] if k not in prev["links"]]
        if not new_lines and not new_links:
            print(f"[site] no change: {url}")
            continue
        hot_lines = [l for l in new_lines if RESULT_WORDS.search(l)]
        hot_links = [l for l in new_links if RESULT_WORDS.search(f"{l[0]} {l[1]}")]
        urgent = bool(hot_lines or hot_links)
        if ONLY_KEYWORDS and not urgent:
            print(f"[site] changed (no keywords, suppressed): {url}")
            continue
        body = []
        if hot_lines or new_lines:
            body.append("New text:\n" + "\n".join(f"• {l[:160]}" for l in (hot_lines or new_lines)[:8]))
        if hot_links or new_links:
            body.append("New links:\n" + "\n".join(f"• {t or '(link)'} → {h}" for t, h in (hot_links or new_links)[:5]))
        click = (hot_links[0][1] if hot_links else url)
        notify(
            "🚨 SIH 2026: possible RESULT update on sih.gov.in!" if urgent else "sih.gov.in changed",
            "\n\n".join(body) or "The page changed.",
            urgent=urgent,
            click=click,
        )

    if snapshots and not errors:
        return "site OK"
    if snapshots:
        return "site partly reachable"
    return "site blocked/unreachable from GitHub (expected)"


# --------------------------------------------------------------------------- #
def main() -> int:
    STATE_DIR.mkdir(exist_ok=True)
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    first_v2 = "news_seen" not in state

    news_status = check_news(state)
    site_status = check_site(state)
    print(f"[summary] {news_status} | {site_status}")

    if first_v2:
        notify(
            "SIH watcher updated ✅",
            f"Now checking every ~10 min:\n• {news_status}\n• {site_status}\n\n"
            "You'll get an URGENT alert when SIH 2026 result / grand-finale news appears.",
            click="https://sih.gov.in/",
        )

    STATE_FILE.write_text(json.dumps(state))
    return 0


if __name__ == "__main__":
    sys.exit(main())
