"""Review, then post one draft to the Aozora PDS (AT Protocol).

  python3 aozora_post.py --draft draft.json --state state.json [--execute]
  python3 aozora_post.py --send-approved ID --state state.json [--execute]

draft.json: {"text": "...", "sources": ["https://..."]}

Every draft goes through two gates, in order:
  1. post_guard.check: hard refusals (no primary source, banned content,
     rate limits). A refused draft is never queued.
  2. review.assess (Legal's pre-post review, AWAI-3655 policy A): a draft that
     needs a human is queued under --review-dir and NOT sent. During the first
     4 weeks of posting that is every draft. An approver approves it with
     review_queue.py; then `--send-approved ID` sends it (post_guard runs again
     at send time).
A draft is sent only when both gates pass AND --execute is given AND both
AOZORA_IDENTIFIER and AOZORA_APP_PASSWORD are set. Anything else is a dry run.
Every decision is appended to <review-dir>/log.jsonl.

Exit codes: 0 sent, queued or dry run; 1 refused; 2 send failed.

Known not to work yet: pds.aozora.app refuses identifier/password
createSession (LegacyLoginDisabled, checked 2026-11-22); bots must present an
Authn Biscuit (AWAI-3658). Until that lands every real send fails with exit 2
and nothing is recorded as posted.
"""
import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

import post_guard
import review
from review_store import ReviewError, Store

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


def first_post_at(state):
    times = [datetime.fromisoformat(p["at"]) for p in state.get("posts", [])]
    return min(times) if times else None


def send(draft, state, now, execute, env, pds=DEFAULT_PDS, http=http_post_json):
    """Send a draft both gates passed. Returns (exit_code, result).
    Mutates `state` only after a send."""
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


def _log(store, now, result, extra=None):
    event = {"event": "result", "at": now.isoformat(), "status": result["status"]}
    for k in ("reasons", "why", "uri", "cid", "error", "id"):
        if k in result:
            event[k] = result[k]
    event.update(extra or {})
    store.log(event)


def run(draft, state, now, execute, env, store, pds=DEFAULT_PDS, http=http_post_json):
    """Review a new draft, then queue it or send it. Returns (exit_code, result)."""
    verdict = post_guard.check(draft, history_of(state), now)
    if not verdict["ok"]:
        result = {"status": "refused", "reasons": verdict["reasons"]}
        _log(store, now, result, {"text": draft.get("text") if isinstance(draft, dict) else None})
        return 1, result

    assessed = review.assess(draft, now, first_post_at(state))
    if assessed["decision"] == "fix":
        result = {"status": "refused", "reasons": assessed["reasons"]}
        _log(store, now, result, {"text": draft["text"]})
        return 1, result
    if assessed["decision"] == "hold":
        item = store.enqueue(draft, assessed["reasons"], now)
        return 0, {"status": "queued", "id": item["id"], "reasons": assessed["reasons"]}

    code, result = send(draft, state, now, execute, env, pds, http)
    _log(store, now, result, {"text": draft["text"], "review": "no approval needed"})
    return code, result


def send_approved(item_id, state, now, execute, env, store, pds=DEFAULT_PDS, http=http_post_json):
    """Send a queued draft an approver approved. Returns (exit_code, result)."""
    try:
        item = store.get(item_id)
    except ReviewError as e:
        return 1, {"status": "refused", "reasons": [str(e)]}
    if item["status"] != "approved":
        return 1, {"status": "refused", "reasons": ["not-approved:%s" % item["status"]]}
    verdict = post_guard.check(item["draft"], history_of(state), now)
    if not verdict["ok"]:
        result = {"status": "refused", "reasons": verdict["reasons"], "id": item_id}
        _log(store, now, result)
        return 1, result
    code, result = send(item["draft"], state, now, execute, env, pds, http)
    if result["status"] == "posted":
        store.mark_posted(item_id, result, now)
    else:
        _log(store, now, dict(result, id=item_id))
    return code, result


def main(argv=None):
    ap = argparse.ArgumentParser(description="Review, then post one draft to the Aozora PDS.")
    what = ap.add_mutually_exclusive_group(required=True)
    what.add_argument("--draft")
    what.add_argument("--send-approved", metavar="ID")
    ap.add_argument("--state", required=True)
    ap.add_argument("--review-dir", default="workspace/review")
    ap.add_argument("--pds", default=DEFAULT_PDS)
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args(argv)

    state = load_state(args.state)
    store = Store(args.review_dir)
    now = datetime.now(timezone.utc)
    if args.draft:
        with open(args.draft, encoding="utf-8") as f:
            draft = json.load(f)
        code, result = run(draft, state, now, args.execute, os.environ, store, args.pds)
    else:
        code, result = send_approved(args.send_approved, state, now, args.execute,
                                     os.environ, store, args.pds)
    if result["status"] == "posted":
        tmp = args.state + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, args.state)
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
