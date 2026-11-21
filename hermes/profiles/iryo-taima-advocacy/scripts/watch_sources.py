"""Cron evidence script: new items about cannabis-derived medicine in public feeds.

Reads the RSS 1.0 feeds in sources.json, keeps the items whose title matches
the keyword list, and prints them as the evidence block the agent turn reads.
Prints {"wakeAgent": false} when there is nothing to report, so the agent
turn is skipped.

An item is reported on up to MAX_REPORTS runs, not once: the agent turn that
follows this script can fail (for example when no inference capacity is
available), and an item reported only once would then be lost. A repeat
cannot double-post, because post_guard refuses a source posted in the last
7 days. Report counts are kept in the profile's workspace (WATCH_STATE,
default workspace/watch-state.json next to the profile). A feed that cannot
be read is reported as UNREADABLE; it is not treated as "nothing new".
"""
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
KEYWORDS = re.compile(
    r"大麻|カンナビ|麻薬及び向精神薬|麻向法|ＣＢＤ|CBD|ＴＨＣ|THC|エピディオレックス|難治性てんかん|cannabi",
    re.I)
MAX_TRACKED = 2000
MAX_REPORTS = 3


def local(tag):
    return tag.rsplit("}", 1)[-1]


def parse_items(xml_bytes):
    """[{title, link, date}] from an RSS 1.0 / RSS 2.0 document."""
    items = []
    for el in ET.fromstring(xml_bytes).iter():
        if local(el.tag) != "item":
            continue
        fields = {local(c.tag): (c.text or "").strip() for c in el}
        if fields.get("title") and fields.get("link"):
            items.append({"title": re.sub(r"\s+", " ", fields["title"]),
                          "link": fields["link"],
                          "date": fields.get("date") or fields.get("pubDate") or ""})
    return items


def select_new(items, reported):
    """Matching items reported on fewer than MAX_REPORTS runs. `reported`: {link: runs}."""
    return [i for i in items
            if KEYWORDS.search(i["title"]) and reported.get(i["link"], 0) < MAX_REPORTS]


def load_reported(state):
    """{link: runs}. The first state format was {"seen": [link]}: those are done."""
    reported = {link: MAX_REPORTS for link in state.get("seen", [])}
    reported.update(state.get("reported", {}))
    return reported


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "iryo-taima-advocacy-watch"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def main():
    with open(os.path.join(HERE, "sources.json"), encoding="utf-8") as f:
        feeds = json.load(f)["feeds"]
    state_path = os.environ.get("WATCH_STATE") or os.path.join(
        HERE, "..", "workspace", "watch-state.json")
    reported = {}
    if os.path.exists(state_path):
        with open(state_path, encoding="utf-8") as f:
            reported = load_reported(json.load(f))

    found, unreadable = [], []
    for feed in feeds:
        try:
            new = select_new(parse_items(fetch(feed["url"])), reported)
        except Exception as e:
            unreadable.append("%s (%s)" % (feed["name"], type(e).__name__))
            continue
        for item in new:
            found.append((feed["name"], item))
            reported[item["link"]] = reported.get(item["link"], 0) + 1

    if not found and not unreadable:
        print(json.dumps({"wakeAgent": False}))
        return 0

    for name, item in found:
        print("NEW [%s] %s | %s | %s" % (name, item["date"], item["title"], item["link"]))
    for u in unreadable:
        print("UNREADABLE %s" % u)
    if not found:
        print(json.dumps({"wakeAgent": False}))

    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump({"reported": dict(list(reported.items())[-MAX_TRACKED:])}, f, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
