"""Approver CLI for the iryo-taima-advocacy review queue (AWAI-3658).

Run from the profile directory (~/.itonami/profiles/iryo-taima-advocacy).
`scripts/queue` is the same CLI under a short name (for the Orca bridge):

  scripts/queue list --pending --json          # [{id, status, text, sources, reasons, queued_at}]
  scripts/queue show ID
  scripts/queue approve ID --by NAME [--note TEXT]
  scripts/queue reject ID --by NAME --reason TEXT
  scripts/queue log [--json]
  scripts/queue referral "incoming reply text"

(--approver and --note are the long spellings of --by and --reason.)

Approving does not send. The next tick (scripts/tick.py) sends the oldest
approved draft, after post_guard runs again (rate limit, duplicates); a
rejected draft is never sent. By hand:
  python3 scripts/aozora_post.py --send-approved ID --state workspace/post-state.json --execute
Real sending also needs "posting_enabled": true in review_config.json.

NAME must be listed in scripts/review_config.json "approvers" (or in
ITONAMI_REVIEW_APPROVERS, comma-separated, which takes precedence). With an
empty list nothing can be approved. Every approval and rejection records the
name and the time, in the queue item and in workspace/review/log.jsonl.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

import review
from review_store import ReviewError, Store, approvers, load_config

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(HERE, "review_config.json")


def main(argv=None, env=None, now=None, out=sys.stdout):
    env = os.environ if env is None else env
    ap = argparse.ArgumentParser(description="Review queue for iryo-taima-advocacy posts.")
    ap.add_argument("--review-dir", default="workspace/review")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--status", choices=["pending", "approved", "rejected", "posted"])
    ls.add_argument("--pending", dest="status", action="store_const", const="pending")
    ls.add_argument("--json", action="store_true")
    sub.add_parser("show").add_argument("id")
    for name in ("approve", "reject"):
        p = sub.add_parser(name)
        p.add_argument("id")
        p.add_argument("--by", "--approver", dest="approver", required=True)
        p.add_argument("--reason", "--note", dest="note", required=(name == "reject"))
    sub.add_parser("log").add_argument("--json", action="store_true")
    sub.add_parser("referral").add_argument("text")
    args = ap.parse_args(argv)

    store = Store(args.review_dir)
    now = now or datetime.now(timezone.utc)
    emit = lambda obj: print(json.dumps(obj, ensure_ascii=False, indent=2), file=out)
    try:
        if args.cmd == "list":
            items = store.items(args.status)
            if args.json:
                emit([{"id": i["id"], "status": i["status"], "text": i["draft"]["text"],
                       "sources": i["draft"].get("sources", []), "reasons": i["reasons"],
                       "queued_at": i["queued_at"], "approver": i["approver"],
                       "decided_at": i["decided_at"]} for i in items])
            else:
                for item in items:
                    print("%s  %-8s  %s  %s" % (item["id"], item["status"], item["queued_at"],
                                                ",".join(item["reasons"])), file=out)
        elif args.cmd == "show":
            emit(store.get(args.id))
        elif args.cmd in ("approve", "reject"):
            allowed = approvers(load_config(args.config), env)
            emit(store.decide(args.id, args.cmd == "approve", args.approver, allowed, now, args.note))
        elif args.cmd == "log":
            events = store.read_log()
            if args.json:
                emit(events)
            else:
                for event in events:
                    print(json.dumps(event, ensure_ascii=False), file=out)
        elif args.cmd == "referral":
            emit({"reply": review.referral_reply(args.text)})
    except ReviewError as e:
        print("review_queue: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
