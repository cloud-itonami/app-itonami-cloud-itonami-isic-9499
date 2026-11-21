"""Approver CLI for the iryo-taima-advocacy review queue (AWAI-3658).

Run from the profile directory (~/.itonami/profiles/iryo-taima-advocacy):

  python3 scripts/review_queue.py list [--status pending]
  python3 scripts/review_queue.py show ID
  python3 scripts/review_queue.py approve ID --approver NAME [--note TEXT]
  python3 scripts/review_queue.py reject ID --approver NAME --note TEXT
  python3 scripts/review_queue.py log
  python3 scripts/review_queue.py referral "incoming reply text"

Approving does not send. After approval, send with
  python3 scripts/aozora_post.py --send-approved ID --state workspace/post-state.json --execute
which runs post_guard again (rate limit, duplicates) before sending.

NAME must be listed in scripts/review_config.json "approvers" (or in
ITONAMI_REVIEW_APPROVERS, comma-separated, which takes precedence). The list is
empty until the owner names the approvers; until then nothing can be approved.
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
    sub.add_parser("show").add_argument("id")
    for name in ("approve", "reject"):
        p = sub.add_parser(name)
        p.add_argument("id")
        p.add_argument("--approver", required=True)
        p.add_argument("--note", required=(name == "reject"))
    sub.add_parser("log")
    sub.add_parser("referral").add_argument("text")
    args = ap.parse_args(argv)

    store = Store(args.review_dir)
    now = now or datetime.now(timezone.utc)
    emit = lambda obj: print(json.dumps(obj, ensure_ascii=False, indent=2), file=out)
    try:
        if args.cmd == "list":
            for item in store.items(args.status):
                print("%s  %-8s  %s  %s" % (item["id"], item["status"], item["queued_at"],
                                            ",".join(item["reasons"])), file=out)
        elif args.cmd == "show":
            emit(store.get(args.id))
        elif args.cmd in ("approve", "reject"):
            allowed = approvers(load_config(args.config), env)
            emit(store.decide(args.id, args.cmd == "approve", args.approver, allowed, now, args.note))
        elif args.cmd == "log":
            for event in store.read_log():
                print(json.dumps(event, ensure_ascii=False), file=out)
        elif args.cmd == "referral":
            emit({"reply": review.referral_reply(args.text)})
    except ReviewError as e:
        print("review_queue: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
