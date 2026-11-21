"""Cron script for every tick of iryo-taima-advocacy (AWAI-3658).

1. Send the oldest draft an approver approved (at most one per tick), after
   post_guard runs again. A rejected or pending draft is never sent. Real
   sending also needs "posting_enabled": true in review_config.json and the
   Aozora credentials in the environment; otherwise the attempt is a dry run
   and the draft stays approved for the next tick.
2. Refresh workspace/review/tick-status.json (tick_status.py) and apply the
   daily murakumo budget: at most max_murakumo_turns_per_day turns per JST day
   (4; in-run retries count), and daily_budget_usd (1 USD) at usd_per_turn
   (0.25, a placeholder). When the cap is reached, the watcher does
   not run (so no item is used up) and the agent turn is skipped with
   {"wakeAgent": false}; the reason is recorded in the status file and the
   review log. With no known per-turn cost the budget cannot be applied and
   the status file says so.
3. Run watch_sources.py and print its evidence block for the agent turn.

The send result is printed as one `APPROVED-SEND {...}` line before the
evidence, and appended to workspace/review/log.jsonl.
"""
import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import aozora_post  # noqa: E402
import tick_status  # noqa: E402
import watch_sources  # noqa: E402
from review_store import Store, load_config, posting_enabled  # noqa: E402


def send_step(now, env, profile=PROFILE, config_path=aozora_post.DEFAULT_CONFIG, http=None):
    store = Store(os.path.join(profile, "workspace", "review"))
    state_path = os.path.join(profile, "workspace", "post-state.json")
    state = aozora_post.load_state(state_path)
    kwargs = {"enabled": posting_enabled(load_config(config_path))}
    if http is not None:
        kwargs["http"] = http
    sent = aozora_post.send_next_approved(state, now, True, env, store, **kwargs)
    if sent is None:
        return None
    _, result = sent
    if result["status"] == "posted":
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        aozora_post.save_state(state_path, state)
    return {k: v for k, v in result.items() if k != "record"}


def budget_step(now, profile=PROFILE, config_path=aozora_post.DEFAULT_CONFIG):
    """Refresh the status file; return None to go on, or the skip reason."""
    config = load_config(config_path)
    status = tick_status.compute(profile, now, config)
    allowed, why = tick_status.budget_allows_turn(status, config)
    if not allowed:
        status["agent_skipped_today"] = {"at": now.isoformat(), "reason": why}
        Store(os.path.join(profile, "workspace", "review")).log(
            {"event": "agent-turn-skipped", "at": now.isoformat(), "reason": why})
    tick_status.write(profile, status)
    return None if allowed else why


def main():
    now = datetime.now(timezone.utc)
    try:
        result = send_step(now, os.environ)
    except Exception as e:  # never let a send problem hide the watcher's evidence
        result = {"status": "send-step-error", "error": type(e).__name__}
    if result:
        print("APPROVED-SEND " + json.dumps(result, ensure_ascii=False))
    try:
        skip = budget_step(now)
    except Exception as e:  # a status problem must not stop the watcher
        print("STATUS-ERROR %s" % type(e).__name__)
        skip = None
    if skip:
        print("BUDGET-SKIP " + skip)
        print(json.dumps({"wakeAgent": False}))
        return 0
    return watch_sources.main()


if __name__ == "__main__":
    sys.exit(main())
