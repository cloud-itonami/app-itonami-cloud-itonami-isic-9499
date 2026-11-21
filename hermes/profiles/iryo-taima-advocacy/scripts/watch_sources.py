"""Cron evidence script: new items about cannabis-derived medicine in public feeds.

Reads the RSS 1.0 feeds in sources.json, keeps the items whose title matches
the keyword list and that were not reported before, and prints them as the
evidence block the agent turn reads. Prints {"wakeAgent": false} when there
is nothing new, so the agent turn is skipped.

Seen links are kept in the profile's workspace (WATCH_STATE, default
workspace/watch-state.json next to the profile). A feed that cannot be read
is reported as UNREADABLE; it is not treated as "nothing new".
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
MAX_SEEN = 2000


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


def select_new(items, seen):
    return [i for i in items if KEYWORDS.search(i["title"]) and i["link"] not in seen]


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "iryo-taima-advocacy-watch"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def main():
    with open(os.path.join(HERE, "sources.json"), encoding="utf-8") as f:
        feeds = json.load(f)["feeds"]
    state_path = os.environ.get("WATCH_STATE") or os.path.join(
        HERE, "..", "workspace", "watch-state.json")
    seen = []
    if os.path.exists(state_path):
        with open(state_path, encoding="utf-8") as f:
            seen = json.load(f).get("seen", [])

    found, unreadable = [], []
    for feed in feeds:
        try:
            new = select_new(parse_items(fetch(feed["url"])), set(seen))
        except Exception as e:
            unreadable.append("%s (%s)" % (feed["name"], type(e).__name__))
            continue
        for item in new:
            found.append((feed["name"], item))
            seen.append(item["link"])

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
        json.dump({"seen": seen[-MAX_SEEN:]}, f, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
