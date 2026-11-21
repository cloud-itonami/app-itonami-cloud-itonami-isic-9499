"""The bot's Aozora profile record (AWAI-3658; Legal memo aozora-bot-accounts.md (2)).

  python3 scripts/aozora_profile.py check    # rules only; placeholders allowed
  python3 scripts/aozora_profile.py render   # the app.bsky.actor.profile record to put;
                                             # refuses while an operator TODO remains

Rules (Legal, until Aozora shows a bot badge):
- displayName ends with 「（bot）」;
- the first line of the description says it is an automated posting bot, names
  the operator and a contact, and says posts are human-approved;
- the description carries the policy notice;
- the account self-label `bot` (com.atproto.label.defs#selfLabels; "bot" is a
  known labelValue in bluesky-social/atproto lexicons at 34063e9);
- no human name or photo as the avatar.
Limits from app.bsky.actor.profile: displayName 64 graphemes / 640 bytes,
description 256 graphemes / 2560 bytes.

Writing the record to the PDS (com.atproto.repo.putRecord, collection
app.bsky.actor.profile, rkey "self") needs the account and its Biscuit; that
is not implemented here.
"""
import copy
import json
import os
import sys

import post_guard
import review

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE_FILE = os.path.join(os.path.dirname(HERE), "aozora_profile.json")
BOT_SUFFIX = "（bot）"
PLACEHOLDER = "TODO"


def load(path=PROFILE_FILE):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def problems(spec, allow_placeholders=True):
    out = []
    rec = spec["record"]
    name = rec.get("displayName", "")
    desc_template = rec.get("description", "")
    filled = desc_template.format(operator_name=spec.get("operator_name", ""),
                                  operator_contact=spec.get("operator_contact", ""))
    if rec.get("$type") != "app.bsky.actor.profile":
        out.append("record $type must be app.bsky.actor.profile")
    if not name.endswith(BOT_SUFFIX):
        out.append("displayName must end with %s" % BOT_SUFFIX)
    if post_guard.graphemes(name) > 64 or len(name.encode("utf-8")) > 640:
        out.append("displayName too long")
    if post_guard.graphemes(filled) > 256 or len(filled.encode("utf-8")) > 2560:
        out.append("description too long: %d graphemes" % post_guard.graphemes(filled))
    first = filled.split("\n", 1)[0]
    for need, label in (("bot", "bot"), ("運営", "operator"), ("承認", "human approval")):
        if need not in first:
            out.append("first line must state: %s" % label)
    if "{operator_name}" not in desc_template or "{operator_contact}" not in desc_template:
        out.append("first line must carry {operator_name} and {operator_contact}")
    if not review.has_disclaimer(filled):
        out.append("description must carry the policy notice")
    labels = rec.get("labels") or {}
    if labels.get("$type") != "com.atproto.label.defs#selfLabels" or \
            {"val": "bot"} not in labels.get("values", []):
        out.append("self-label bot missing")
    if rec.get("avatar") is not None:
        out.append("avatar is set in the record; check it is not a human name or photo, then set it at apply time")
    if not allow_placeholders:
        for key in ("operator_name", "operator_contact"):
            v = spec.get(key) or ""
            if not v.strip() or PLACEHOLDER in v:
                out.append("%s is not decided (owner): %r" % (key, v))
    return out


def render(spec):
    errs = problems(spec, allow_placeholders=False)
    if errs:
        raise ValueError("; ".join(errs))
    rec = copy.deepcopy(spec["record"])
    rec["description"] = rec["description"].format(operator_name=spec["operator_name"],
                                                   operator_contact=spec["operator_contact"])
    return rec


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else "check"
    spec = load()
    if cmd == "check":
        errs = problems(spec)
        print(json.dumps({"ok": not errs, "problems": errs}, ensure_ascii=False))
        return 0 if not errs else 1
    if cmd == "render":
        try:
            print(json.dumps(render(spec), ensure_ascii=False, indent=2))
        except ValueError as e:
            print("aozora_profile: %s" % e, file=sys.stderr)
            return 1
        return 0
    print("usage: aozora_profile.py check | render", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
