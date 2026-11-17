#!/usr/bin/env python3
"""
Reddit watcher for SIH 2026 results  (runs every ~10 min on GitHub Actions, free)

Scans NEW POSTS and NEW COMMENTS in Indian student subreddits, plus a
Reddit-wide search, and sends an URGENT ntfy alert when someone mentions
SIH + result / shortlisted / selected / finale etc.

Access methods, tried in order:
  1. Official Reddit API (only if repo secrets REDDIT_CLIENT_ID and
     REDDIT_CLIENT_SECRET are set; free "script" app from reddit.com/prefs/apps)
  2. Public JSON  (www.reddit.com/...json)
  3. Public RSS   (www.reddit.com/....rss)
Reddit sometimes blocks cloud servers for 2 and 3; method 1 always works.
"""

import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

# Subreddits where SIH results get discussed first. CORE = known to exist;
# EXTRA are tried too, and dropped automatically if Reddit rejects them.
CORE_SUBS = ["Btechtards", "developersIndia", "Indian_Academia", "hackathon", "india"]
EXTRA_SUBS = ["hackathons", "IndianStudents", "SmartIndiaHackathon", "NITSilchar", "StudentsofIndia"]
_custom = os.environ.get("REDDIT_SUBS", "").strip()
if _custom:
    CORE_SUBS, EXTRA_SUBS = [x.strip() for x in _custom.split(",") if x.strip()], []
SUBREDDITS = CORE_SUBS + EXTRA_SUBS
FELL_BACK = False
RSS_GAP = 61  # seconds between RSS requests
SEARCH_QUERY = '"SIH 2026" OR "SIH result" OR "SIH results" OR "smart india hackathon"'

SIH = re.compile(r"\bSIH\b|smart\s*india\s*hackathon", re.IGNORECASE)
# Words that signal results are actually OUT (English + Hinglish)
STRONG = re.compile(
    r"\bout\b|declared|announced|released|live now|"
    r"aa\s?gay[ae]|aa\s?gy[ae]|aagay[ae]|aaya hai|aa chuka|aa chuke|"
    r"shortlist|short-list|short listed|selected|select ho|got through|cleared|qualif|"
    r"congrat|grand\s*finale|finalist|nodal|mail (aaya|aa gaya|received|came|mila)",
    re.IGNORECASE,
)
# Pure "when will it come?" chatter - ignored unless it also confirms results are out
QUESTION = re.compile(
    r"\bkab\b|\bwhen\b|any update|koi update|expected|still waiting|kitne din|how long|"
    r"kab tak|will (it|they|results?) (come|be)",
    re.IGNORECASE,
)
CONFIRM = re.compile(
    r"\bout\b|declared|released|aa\s?gay[ae]|aa\s?gy[ae]|aagay[ae]|aa chuk|congrat|got (selected|through)|mail (aaya|received|came|mila)",
    re.IGNORECASE,
)
OLD_EDITION = re.compile(r"\b20(1\d|2[0-5])\b")
MAX_AGE = 2 * 86400  # ignore anything older than 2 days

STATE_DIR = Path("state")
STATE_FILE = STATE_DIR / "reddit_state.json"
FAIL_ALERT_AFTER = 6  # consecutive fully-failed runs (~1 hour)

UA = "github-actions:sih-result-watch:v1.0 (personal alert bot by u/jayInnovates)"


# --------------------------------------------------------------------------- #
def notify(title: str, body: str, urgent: bool = False, click: str = "") -> None:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    print(f"[notify] {title}\n{body}")
    if not topic:
        print("WARNING: NTFY_TOPIC not set", file=sys.stderr)
        return
    headers = {
        "Title": title.encode("ascii", "ignore").decode().strip(),
        "Priority": "5" if urgent else "3",
        "Tags": "rotating_light" if urgent else "eyes",
    }
    if click:
        headers["Click"] = click
    req = urllib.request.Request(
        f"https://ntfy.sh/{urllib.parse.quote(topic)}",
        data=body.encode("utf-8"), headers=headers, method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:  # noqa: BLE001
        print(f"ntfy failed: {e}", file=sys.stderr)


def http_get(url: str, headers: dict | None = None) -> bytes:
    h = {"User-Agent": UA}
    h.update(headers or {})
    return urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=25).read()


# --------------------------------------------------------------------------- #
# Fetchers -> list of {id, kind, sub, text, link, created}
# --------------------------------------------------------------------------- #
def _from_listing(data: dict) -> list[dict]:
    out = []
    for ch in data.get("data", {}).get("children", []):
        d = ch.get("data", {})
        if ch.get("kind") == "t1":  # comment
            text = f"{d.get('body', '')}  [on: {d.get('link_title', '')}]"
            kind = "comment"
        else:
            text = f"{d.get('title', '')}  {d.get('selftext', '')[:500]}"
            kind = "post"
        out.append({
            "id": d.get("name") or d.get("id"),
            "kind": kind,
            "sub": d.get("subreddit", ""),
            "text": re.sub(r"\s+", " ", text).strip(),
            "link": "https://www.reddit.com" + d.get("permalink", ""),
            "created": float(d.get("created_utc", 0)),
        })
    return out


def _from_rss(raw: bytes, sub: str) -> list[dict]:
    import calendar
    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(raw)
    out = []
    for e in root.findall("a:entry", ns):
        link_el = e.find("a:link", ns)
        link = link_el.get("href") if link_el is not None else ""
        cat = e.find("a:category", ns)
        content = re.sub(r"<[^>]+>", " ", e.findtext("a:content", "", ns))
        content = re.sub(r"submitted by\s+/u/\S+.*$", "", content)
        title = e.findtext("a:title", "", ns)
        upd = e.findtext("a:published", "", ns) or e.findtext("a:updated", "", ns)
        try:
            created = calendar.timegm(time.strptime(upd[:19], "%Y-%m-%dT%H:%M:%S"))
        except Exception:  # noqa: BLE001
            created = time.time()
        is_comment = bool(re.search(r"/comments/[^/]+/[^/]*/[a-z0-9]+/?$", link))
        out.append({
            "id": e.findtext("a:id", "", ns) or link,
            "kind": "comment" if is_comment else "post",
            "sub": (cat.get("term") if cat is not None else sub) or sub,
            "text": re.sub(r"\s+", " ", f"{title} {content}").strip()[:800],
            "link": link,
            "created": created,
        })
    return out


def oauth_token() -> str | None:
    cid = os.environ.get("REDDIT_CLIENT_ID", "").strip()
    sec = os.environ.get("REDDIT_CLIENT_SECRET", "").strip()
    if not (cid and sec):
        return None
    req = urllib.request.Request(
        "https://www.reddit.com/api/v1/access_token",
        data=b"grant_type=client_credentials",
        headers={
            "User-Agent": UA,
            "Authorization": "Basic " + base64.b64encode(f"{cid}:{sec}".encode()).decode(),
        },
        method="POST",
    )
    return json.loads(urllib.request.urlopen(req, timeout=25).read())["access_token"]


def rss_feeds(subs: list[str]) -> list[tuple[str, str]]:
    multi = "+".join(subs)
    busy = [x for x in subs if x in ("Btechtards", "developersIndia")]
    # r/india is far too busy to follow every comment; its posts are still in "posts"
    rest = [x for x in subs if x not in busy and x != "india"] or subs
    q = urllib.parse.quote(SEARCH_QUERY)
    feeds = [("posts", f"https://www.reddit.com/r/{multi}/new/.rss?limit=100")]
    # comments: busy subs get their own feed so 100-item window covers 10 minutes
    for b_ in busy:
        feeds.append((f"comments:{b_}", f"https://www.reddit.com/r/{b_}/comments/.rss?limit=100"))
    feeds.append(("comments:other", f"https://www.reddit.com/r/{'+'.join(rest)}/comments/.rss?limit=100"))
    # (Reddit-wide search.rss returns 403 to GitHub's servers, so it is not used)
    return feeds


def get_rss(url: str) -> bytes:
    for attempt in range(3):
        try:
            return http_get(url, {"Accept": "application/atom+xml"})
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                wait = 30 * (attempt + 1)
                print(f"[reddit:rss] 429, waiting {wait}s")
                time.sleep(wait)
                continue
            raise
    raise RuntimeError("unreachable")


def fetch_all() -> tuple[list[dict], str, int, int]:
    """Returns (items, method, ok_count, total)."""
    # Official API, only if keys exist (Reddit stopped issuing new keys in 2025-26)
    try:
        tok = oauth_token()
    except Exception as e:  # noqa: BLE001
        tok = None
        print(f"[reddit] OAuth failed: {e}", file=sys.stderr)
    if tok:
        items, ok = [], 0
        multi = "+".join(SUBREDDITS)
        paths = [f"/r/{multi}/new", f"/r/{multi}/comments",
                 f"/search?q={urllib.parse.quote(SEARCH_QUERY)}&sort=new&t=week"]
        for pth in paths:
            sep = "&" if "?" in pth else "?"
            try:
                raw = http_get(f"https://oauth.reddit.com{pth}{sep}limit=100&raw_json=1",
                               {"Authorization": f"bearer {tok}"})
                items += _from_listing(json.loads(raw))
                ok += 1
            except Exception as e:  # noqa: BLE001
                print(f"[reddit:api] {pth[:60]}: {e}", file=sys.stderr)
        if ok:
            return items, "api", ok, len(paths)

    # Public RSS (works from GitHub, but rate-limited -> few combined requests)
    subs = list(SUBREDDITS)
    feeds = rss_feeds(subs)
    items, ok = [], 0
    i = 0
    while i < len(feeds):
        name, url = feeds[i]
        try:
            got = _from_rss(get_rss(url), name)
            items += got
            ok += 1
            print(f"[reddit:rss] {name}: {len(got)} items")
        except urllib.error.HTTPError as e:
            print(f"[reddit:rss] {e.code} {name}", file=sys.stderr)
            # a missing/private/banned sub breaks a combined feed -> retry with CORE only
            if e.code in (403, 404) and set(subs) != set(CORE_SUBS) and name in ("posts", "comments:other"):
                global FELL_BACK
                FELL_BACK = True
                subs = list(CORE_SUBS)
                feeds = feeds[:i] + [f for f in rss_feeds(subs) if f[0] == name] + feeds[i + 1:]
                print(f"[reddit:rss] retrying {name} with core subs only")
                continue
        except Exception as e:  # noqa: BLE001
            print(f"[reddit:rss] error {name}: {e}", file=sys.stderr)
        i += 1
        if i < len(feeds):
            time.sleep(RSS_GAP)  # Reddit allows ~1 RSS request/minute from GitHub
    print(f"[reddit:rss] {ok}/{len(feeds)} feeds OK, {len(items)} items")
    return (items, "rss", ok, len(feeds)) if ok else ([], "none", 0, len(feeds))


# --------------------------------------------------------------------------- #
def is_hit(it: dict) -> bool:
    t = it["text"]
    if not (SIH.search(t) and STRONG.search(t)):
        return False
    if QUESTION.search(t) and not CONFIRM.search(t):
        return False
    if OLD_EDITION.search(t) and "2026" not in t:
        return False
    return True


def main() -> int:
    STATE_DIR.mkdir(exist_ok=True)
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    first = not state.get("announced")
    since = state.setdefault("since", time.time())
    alerted = set(state.get("alerted", []))

    global SUBREDDITS
    if state.get("core_only"):
        SUBREDDITS = list(CORE_SUBS)
    items, method, ok, total = fetch_all()
    if FELL_BACK:
        state["core_only"] = True
        SUBREDDITS = list(CORE_SUBS)

    if method == "none":
        state["fail"] = state.get("fail", 0) + 1
        if state["fail"] == FAIL_ALERT_AFTER:
            notify(
                "Reddit watcher: Reddit is blocking GitHub",
                "Reddit refused every request from GitHub's servers for ~1 hour "
                "(rate limit or block). The watcher keeps trying every 10 minutes.",
            )
        STATE_FILE.write_text(json.dumps(state))
        return 0
    state["fail"] = 0

    now = time.time()
    hits = []
    for it in items:
        if not it["id"] or it["id"] in alerted:
            continue
        if it["created"] < since - 60 or now - it["created"] > MAX_AGE:
            continue
        if is_hit(it):
            hits.append(it)
            alerted.add(it["id"])
    state["alerted"] = list(alerted)[-2000:]

    # de-duplicate (search + subreddit feeds can return the same post)
    seen, uniq = set(), []
    for h in sorted(hits, key=lambda x: x["created"], reverse=True):
        if h["link"] not in seen:
            seen.add(h["link"])
            uniq.append(h)

    if uniq:
        lines = [
            f"• r/{h['sub']} {h['kind']}: {h['text'][:170]}" for h in uniq[:6]
        ]
        notify(
            "🚨 Reddit: SIH result chatter!",
            f"{len(uniq)} new Reddit {'mention' if len(uniq) == 1 else 'mentions'} of SIH results:\n"
            + "\n".join(lines) + "\n\nVerify on sih.gov.in.",
            urgent=True,
            click=uniq[0]["link"],
        )
    else:
        print(f"[reddit] no new SIH-result mentions ({len(items)} items scanned via {method})")

    if first:
        state["announced"] = True
        notify(
            "Reddit watcher is running ✅",
            f"Scanning new posts + comments in r/{', r/'.join(SUBREDDITS)} and a Reddit-wide "
            f"search (when available) every ~10 min.\nThis run: {ok}/{total} feeds OK, {len(items)} items.",
            click="https://www.reddit.com/r/Btechtards/new/",
        )

    STATE_FILE.write_text(json.dumps(state))
    return 0


if __name__ == "__main__":
    sys.exit(main())
