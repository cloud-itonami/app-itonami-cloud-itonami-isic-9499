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
     `scripts/queue approve ID --by NAME`; the next tick (scripts/tick.py), or
     `--send-approved ID`, sends it (post_guard runs again at send time).
     Timing refusals (daily-limit, too-soon, source-posted-recently) block a
     send, not queuing.
Real sending needs "posting_enabled": true in review_config.json (off until
the owner turns it on).
A draft is sent only when both gates pass AND --execute is given AND the
bot's did:key seed is in the Keychain (aozora_identity.py). Anything else is a
dry run. Every decision is appended to <review-dir>/log.jsonl.

Sending (docs/agent-identity.md §8, network-awai/app-kotoba-cloud): for every
post, fetch a nonce, prove the did:key and take a fresh 15-minute Biscuit with
repo:post only (aozora_identity.biscuit_for_post), then createRecord on the
bot's own repo with the `bot` self-label. The Biscuit is used once and never
saved. The bot never replies: a record never carries `reply` (the PDS also
refuses bot replies, agent_reply_not_permitted).

Exit codes: 0 sent, queued or dry run; 1 refused; 2 send failed.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

import aozora_identity
import post_guard
import review
from review_store import ReviewError, Store, load_config, posting_enabled

# post_guard reasons about timing, not content: they block a send, but a draft
# that only waits for a human may still be queued.
RATE_REASONS = ("daily-limit", "too-soon", "source-posted-recently")
DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "review_config.json")

DEFAULT_PDS = "https://pds.aozora.app"
COLLECTION = "app.bsky.feed.post"
BOT_LABELS = {"$type": "com.atproto.label.defs#selfLabels", "values": [{"val": "bot"}]}
# Fresh Biscuit per post; tests replace this.
IDENTITY = aozora_identity.biscuit_for_post


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
    record = {"$type": COLLECTION, "text": text, "langs": ["ja"], "labels": BOT_LABELS,
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


def _http_failure(e):
    """status and error code of a failed PDS call; nothing else of the body."""
    out = {"status": "send-failed", "step": "createRecord", "error": type(e).__name__}
    if isinstance(e, urllib.error.HTTPError):
        out["http_status"] = e.code
        try:
            code = json.loads(e.read().decode("utf-8")).get("error")
            if isinstance(code, str) and aozora_identity.CODE_RE.match('"error":"%s"' % code):
                out["code"] = code
        except Exception:
            pass
    return out


def first_post_at(state):
    times = [datetime.fromisoformat(p["at"]) for p in state.get("posts", [])]
    return min(times) if times else None


def send(draft, state, now, execute, env, pds=DEFAULT_PDS, http=http_post_json, enabled=True):
    """Send a draft both gates passed. Returns (exit_code, result).
    Mutates `state` only after a send. `enabled` is the owner's posting switch.
    Neither the Keychain nor the network is touched unless execute and
    enabled are both true."""
    record = build_record(draft["text"], now)
    assert "reply" not in record  # never reply
    if not (execute and enabled):
        why = "no --execute" if not execute else \
            "posting disabled (review_config.json posting_enabled)"
        return 0, {"status": "dry-run", "why": why, "record": record}

    try:
        auth = IDENTITY(env)
    except aozora_identity.NotConfigured as e:
        return 0, {"status": "dry-run", "why": "identity not set (%s)" % e, "record": record}
    except aozora_identity.IdentityError as e:
        return 2, {"status": "send-failed", "step": e.step, "http_status": e.status,
                   "code": e.code, "error": "IdentityError"}
    except Exception as e:
        return 2, {"status": "send-failed", "step": "biscuit", "error": type(e).__name__}
    try:
        created = http(pds + "/xrpc/com.atproto.repo.createRecord",
                       {"repo": auth["did"], "collection": COLLECTION, "record": record},
                       {"Authorization": "Bearer " + auth["token"]})
        uri = created["uri"]
    except Exception as e:  # the response body may hold detail: keep status and code only
        return 2, _http_failure(e)
    finally:
        auth = None

    state.setdefault("posts", []).append({
        "at": now.isoformat(), "hash": post_guard.text_hash(draft["text"]),
        "sources": draft.get("sources", []), "uri": uri, "cid": created.get("cid")})
    return 0, {"status": "posted", "uri": uri, "cid": created.get("cid")}


def _log(store, now, result, extra=None):
    event = {"event": "result", "at": now.isoformat(), "status": result["status"]}
    for k in ("reasons", "why", "uri", "cid", "error", "id", "step", "http_status", "code"):
        if k in result:
            event[k] = result[k]
    event.update(extra or {})
    store.log(event)


def run(draft, state, now, execute, env, store, pds=DEFAULT_PDS, http=http_post_json,
        enabled=True):
    """Review a new draft, then queue it or send it. Returns (exit_code, result)."""
    verdict = post_guard.check(draft, history_of(state), now)
    content = [r for r in verdict["reasons"] if r not in RATE_REASONS]
    rate = [r for r in verdict["reasons"] if r in RATE_REASONS]
    if content:
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
        return 0, {"status": "queued", "id": item["id"], "reasons": item["reasons"]}
    if rate:
        result = {"status": "refused", "reasons": rate}
        _log(store, now, result, {"text": draft["text"]})
        return 1, result

    code, result = send(draft, state, now, execute, env, pds, http, enabled)
    _log(store, now, result, {"text": draft["text"], "review": "no approval needed"})
    return code, result


def send_approved(item_id, state, now, execute, env, store, pds=DEFAULT_PDS, http=http_post_json,
                  enabled=True):
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
    code, result = send(item["draft"], state, now, execute, env, pds, http, enabled)
    if result["status"] == "posted":
        store.mark_posted(item_id, result, now)
    else:
        _log(store, now, dict(result, id=item_id))
    return code, result


def send_next_approved(state, now, execute, env, store, pds=DEFAULT_PDS, http=http_post_json,
                       enabled=True):
    """Send the oldest approved draft, if any (one per tick). Returns
    (exit_code, result) or None when nothing is approved."""
    approved = store.items("approved")
    if not approved:
        return None
    return send_approved(approved[0]["id"], state, now, execute, env, store, pds, http, enabled)


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Review, then post one draft to the Aozora PDS.")
    what = ap.add_mutually_exclusive_group(required=True)
    what.add_argument("--draft")
    what.add_argument("--send-approved", metavar="ID")
    ap.add_argument("--state", required=True)
    ap.add_argument("--review-dir", default="workspace/review")
    ap.add_argument("--pds", default=DEFAULT_PDS)
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    args = ap.parse_args(argv)
    enabled = posting_enabled(load_config(args.config))

    state = load_state(args.state)
    store = Store(args.review_dir)
    now = datetime.now(timezone.utc)
    if args.draft:
        with open(args.draft, encoding="utf-8") as f:
            draft = json.load(f)
        code, result = run(draft, state, now, args.execute, os.environ, store, args.pds,
                           enabled=enabled)
    else:
        code, result = send_approved(args.send_approved, state, now, args.execute,
                                     os.environ, store, args.pds, enabled=enabled)
    if result["status"] == "posted":
        save_state(args.state, state)
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
