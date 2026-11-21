"""Post one guarded draft to the Aozora PDS (AT Protocol).

  python3 aozora_post.py --draft draft.json --state state.json            # dry run
  python3 aozora_post.py --draft draft.json --state state.json --execute  # send

draft.json: {"text": "...", "sources": ["https://..."]}

A draft is sent only when post_guard.check passes AND --execute is given AND
both AOZORA_IDENTIFIER and AOZORA_APP_PASSWORD are set in the environment.
Anything else is a dry run: the record is printed and no request is made.
The state file keeps the post history the guard reads (rate limit, dedupe).

Exit codes: 0 sent or dry run, 1 refused by the guard, 2 send failed.

Not verified against the live PDS: the createSession call uses the standard
AT Protocol identifier/password form. If pds.aozora.app accepts only its
CACAO session form for this account, the send fails with exit 2 and nothing
is recorded as posted.
"""
import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

import post_guard

DEFAULT_PDS = "https://pds.aozora.app"
COLLECTION = "app.bsky.feed.post"


def load_state(path):
    if not os.path.exists(path):
        return {"posts": []}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def history_of(state):
    return [{"at": datetime.fromisoformat(p["at"]), "hash": p["hash"],
             "sources": p.get("sources", [])} for p in state.get("posts", [])]


def link_facets(text):
    """AT Protocol link facets; indices are UTF-8 byte offsets."""
    facets = []
    for m in post_guard.URL_RE.finditer(text):
        start = len(text[:m.start()].encode("utf-8"))
        end = start + len(m.group(0).encode("utf-8"))
        facets.append({"index": {"byteStart": start, "byteEnd": end},
                       "features": [{"$type": "app.bsky.richtext.facet#link",
                                     "uri": m.group(0)}]})
    return facets


def build_record(text, now):
    record = {"$type": COLLECTION, "text": text, "langs": ["ja"],
              "createdAt": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")}
    facets = link_facets(text)
    if facets:
        record["facets"] = facets
    return record


def http_post_json(url, body, headers):
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json", **headers},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def run(draft, state, now, execute, env, pds=DEFAULT_PDS, http=http_post_json):
    """Return (exit_code, result dict). Mutates `state` only after a send."""
    verdict = post_guard.check(draft, history_of(state), now)
    if not verdict["ok"]:
        return 1, {"status": "refused", "reasons": verdict["reasons"]}

    record = build_record(draft["text"], now)
    identifier = env.get("AOZORA_IDENTIFIER")
    password = env.get("AOZORA_APP_PASSWORD")
    if not (execute and identifier and password):
        why = "no --execute" if not execute else "credentials not set"
        return 0, {"status": "dry-run", "why": why, "record": record}

    try:
        session = http(pds + "/xrpc/com.atproto.server.createSession",
                       {"identifier": identifier, "password": password}, {})
        created = http(pds + "/xrpc/com.atproto.repo.createRecord",
                       {"repo": session["did"], "collection": COLLECTION, "record": record},
                       {"Authorization": "Bearer " + session["accessJwt"]})
        uri = created["uri"]
    except Exception as e:  # the response body may hold account detail: keep the class only
        return 2, {"status": "send-failed", "error": type(e).__name__}

    state.setdefault("posts", []).append({
        "at": now.isoformat(), "hash": post_guard.text_hash(draft["text"]),
        "sources": draft.get("sources", []), "uri": uri, "cid": created.get("cid")})
    return 0, {"status": "posted", "uri": uri, "cid": created.get("cid")}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Post one guarded draft to the Aozora PDS.")
    ap.add_argument("--draft", required=True)
    ap.add_argument("--state", required=True)
    ap.add_argument("--pds", default=DEFAULT_PDS)
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args(argv)

    with open(args.draft, encoding="utf-8") as f:
        draft = json.load(f)
    state = load_state(args.state)
    code, result = run(draft, state, datetime.now(timezone.utc), args.execute,
                       os.environ, args.pds)
    if result["status"] == "posted":
        tmp = args.state + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, args.state)
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
