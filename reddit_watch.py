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

SUBREDDITS = [
    s.strip()
    for s in (
        os.environ.get("REDDIT_SUBS", "").strip()
        or "Btechtards,developersIndia,Indian_Academia,IndianStudents,SmartIndiaHackathon,NITSilchar"
    ).split(",")
    if s.strip()
]
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
    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(raw)
    out = []
    for e in root.findall("a:entry", ns):
        link_el = e.find("a:link", ns)
        link = link_el.get("href") if link_el is not None else ""
        content = re.sub(r"<[^>]+>", " ", e.findtext("a:content", "", ns))
        title = e.findtext("a:title", "", ns)
        upd = e.findtext("a:updated", "", ns) or e.findtext("a:published", "", ns)
        try:
            created = time.mktime(time.strptime(upd[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
        except Exception:  # noqa: BLE001
            created = time.time()
        out.append({
            "id": e.findtext("a:id", "", ns) or link,
            "kind": "comment" if "/comments/" in link and link.rstrip("/").count("/") > 7 else "post",
            "sub": sub,
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


def endpoints() -> list[tuple[str, str]]:
    eps = []
    for s in SUBREDDITS:
        eps.append((s, f"/r/{s}/new"))
        eps.append((s, f"/r/{s}/comments"))
    q = urllib.parse.quote(SEARCH_QUERY)
    eps.append(("search", f"/search?q={q}&sort=new&t=week&type=link"))
    return eps


def fetch_all() -> tuple[list[dict], str, int, int]:
    """Returns (items, method, ok_count, total)."""
    eps = endpoints()
    methods = []
    try:
        tok = oauth_token()
        if tok:
            methods.append(("api", tok))
    except Exception as e:  # noqa: BLE001
        print(f"[reddit] OAuth failed: {e}", file=sys.stderr)
    methods += [("json", None), ("rss", None)]

    for method, tok in methods:
        items, ok, blocked = [], 0, False
        for sub, path in eps:
            sep = "&" if "?" in path else "?"
            try:
                if method == "api":
                    raw = http_get(f"https://oauth.reddit.com{path}{sep}limit=100&raw_json=1",
                                   {"Authorization": f"bearer {tok}"})
                    items += _from_listing(json.loads(raw))
                elif method == "json":
                    base, _, qs = path.partition("?")
                    raw = http_get(f"https://www.reddit.com{base}.json?{qs}{'&' if qs else ''}limit=100&raw_json=1")
                    items += _from_listing(json.loads(raw))
                else:
                    base, _, qs = path.partition("?")
                    raw = http_get(f"https://www.reddit.com{base}/.rss?{qs}{'&' if qs else ''}limit=100")
                    items += _from_rss(raw, sub)
                ok += 1
            except urllib.error.HTTPError as e:
                print(f"[reddit:{method}] {e.code} {path[:70]}", file=sys.stderr)
                if e.code == 429:
                    blocked = ok == 0
                    break
            except Exception as e:  # noqa: BLE001
                print(f"[reddit:{method}] error {path[:70]}: {e}", file=sys.stderr)
            time.sleep(1.0 if method == "api" else 2.5)
        print(f"[reddit:{method}] {ok}/{len(eps)} endpoints OK, {len(items)} items")
        if ok and not blocked:
            return items, method, ok, len(eps)
    return [], "none", 0, len(eps)


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
    first = "since" not in state
    since = state.setdefault("since", time.time())
    alerted = set(state.get("alerted", []))

    items, method, ok, total = fetch_all()

    if method == "none":
        state["fail"] = state.get("fail", 0) + 1
        if first or state["fail"] == FAIL_ALERT_AFTER:
            notify(
                "Reddit watcher: Reddit is blocking GitHub",
                "Reddit refused all requests from GitHub's servers. Fix: add free Reddit API "
                "keys (REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET repo secrets).",
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
        notify(
            "Reddit watcher is running ✅",
            f"Scanning new posts + comments in r/{', r/'.join(SUBREDDITS)} and a Reddit-wide "
            f"search every ~10 min.\nAccess: {method} ({ok}/{total} feeds OK, {len(items)} items).",
            click="https://www.reddit.com/r/Btechtards/new/",
        )

    STATE_FILE.write_text(json.dumps(state))
    return 0


if __name__ == "__main__":
    sys.exit(main())
