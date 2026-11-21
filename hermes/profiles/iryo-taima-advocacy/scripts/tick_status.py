"""Machine-readable status of the iryo-taima-advocacy tick (AWAI-3658).

Writes workspace/review/tick-status.json from the host's own run records
(cron/itonami-runs.jsonl and cron/usage_audit.jsonl), for the Orca automation:

  {"updated_at", "job_id", "last_run", "last_status", "exit_code",
   "consecutive_failures", "last_error", "day", "runs_today",
   "murakumo_turns_today", "agent_turns_today", "murakumo_failed_requests_today",
   "max_murakumo_turns_per_day", "tokens_today", "est_cost_usd_today",
   "cost_basis", "daily_budget_usd", "agent_skipped_today",
   "successful_runs_total", "successful_turns_total", "tokens_total_successful",
   "tokens_per_successful_turn", "posting_enabled"}

Counting (Decision 2026-11-22): a murakumo turn is every model request the
run made, in-run retries included: the agent's completed turns plus every
failed murakumo attempt. The day is JST. At most max_murakumo_turns_per_day
(4) per day; usd_per_turn is 0.25 until the real price is known, so 4 turns
= 1 USD/day. Tokens per successful turn are kept so the price can be
recomputed after about 28 successful turns.

Run after each tick (the scheduled task does), optionally with the launcher's
exit code:  python3 scripts/tick_status.py [--exit-code N]
scripts/tick.py also refreshes it before deciding whether today's budget
allows an agent turn.

Cost: murakumo publishes no price for the mishima fleet (checked 2026-11-22:
/v1/models carries no price field). usd_per_turn in review_config.json is a
placeholder (0.25) until it is known; with it unset, est_cost_usd_today is
null and only the turn cap applies.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from review_store import load_config, posting_enabled  # noqa: E402

JOB_ID = "1a7a0ad0c4c1"
JST = timezone(timedelta(hours=9))
FAILED = ("error", "delivery_failed")
DEFAULT_BUDGET_USD = 1.0
DEFAULT_MAX_TURNS = 4
CONFIG = os.path.join(HERE, "review_config.json")


def _lines(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out


def _ts(row):
    try:
        return datetime.fromisoformat(row["ts"])
    except (KeyError, TypeError, ValueError):
        return None


def _error(row):
    if row.get("status") not in FAILED:
        return None
    errs = [a.get("error") for a in row.get("attempts") or [] if a.get("error")]
    return {"exit_reason": row.get("exit_reason"), "error": errs[-1] if errs else row.get("error"),
            "attempts": len(row.get("attempts") or []), "at": row.get("ts")}


def max_turns(config):
    v = config.get("max_murakumo_turns_per_day", DEFAULT_MAX_TURNS)
    return int(v) if isinstance(v, int) and v >= 0 else DEFAULT_MAX_TURNS


def usd_per_turn(config):
    v = config.get("usd_per_turn")
    return float(v) if isinstance(v, (int, float)) and v >= 0 else None


def compute(profile, now, config, exit_code=None):
    runs = [r for r in _lines(os.path.join(profile, "cron", "itonami-runs.jsonl"))
            if r.get("job_id") == JOB_ID]
    usage = [u for u in _lines(os.path.join(profile, "cron", "usage_audit.jsonl"))
             if u.get("job_id") == JOB_ID]
    day = now.astimezone(JST).date()
    today = lambda rows: [r for r in rows if _ts(r) and _ts(r).astimezone(JST).date() == day]

    failures = 0
    for r in reversed(runs):
        if r.get("status") in FAILED:
            failures += 1
        else:
            break
    last = runs[-1] if runs else None
    agent_turns = sum(int(r.get("turns") or 0) for r in today(runs))
    failed = sum(1 for r in today(runs) for a in r.get("attempts") or []
                 if a.get("provider") == "murakumo")
    turns = agent_turns + failed
    tokens = sum(int(u.get("total_tokens") or 0) for u in today(usage))
    ok_runs = [r for r in runs if r.get("status") not in FAILED and int(r.get("turns") or 0) > 0]
    ok_turns = sum(int(r["turns"]) for r in ok_runs)
    ok_ts = {r.get("ts") for r in ok_runs}
    ok_tokens = sum(int(u.get("total_tokens") or 0) for u in usage if u.get("ts") in ok_ts)
    per_turn = usd_per_turn(config)
    previous = read(profile) or {}
    skipped = previous.get("agent_skipped_today") if previous.get("day") == day.isoformat() else None
    if last is None:
        code = exit_code
    elif exit_code is not None:
        code = exit_code
    else:
        code = 1 if last.get("status") in FAILED else 0
    return {
        "updated_at": now.isoformat(), "job_id": JOB_ID,
        "last_run": last.get("ts") if last else None,
        "last_status": last.get("status") if last else None,
        "exit_code": code,
        "consecutive_failures": failures,
        "last_error": _error(last) if last else None,
        "day": day.isoformat(), "runs_today": len(today(runs)),
        "murakumo_turns_today": turns,
        "agent_turns_today": agent_turns,
        "murakumo_failed_requests_today": failed,
        "max_murakumo_turns_per_day": max_turns(config),
        "tokens_today": tokens,
        "est_cost_usd_today": round(turns * per_turn, 4) if per_turn is not None else None,
        "cost_basis": ("usd_per_turn=%s from review_config.json" % per_turn if per_turn is not None
                       else "unknown: murakumo publishes no price for mishima; turns and tokens recorded"),
        "daily_budget_usd": config.get("daily_budget_usd", DEFAULT_BUDGET_USD),
        "agent_skipped_today": skipped,
        "successful_runs_total": len(ok_runs),
        "successful_turns_total": ok_turns,
        "tokens_total_successful": ok_tokens,
        "tokens_per_successful_turn": round(ok_tokens / ok_turns, 1) if ok_turns else None,
        "posting_enabled": posting_enabled(config),
    }


def budget_allows_turn(status, config):
    """(allowed, reason). Refuse once today's murakumo turns (retries
    included) reached the cap, or when one more turn would take the estimate
    over the budget. A run already under way is not cut: one run can use more
    than one turn, so a day may end a little over the cap."""
    cap = max_turns(config)
    if status["murakumo_turns_today"] >= cap:
        return False, "turn cap: %d murakumo turns today (retries included), cap %d" % (
            status["murakumo_turns_today"], cap)
    per_turn = usd_per_turn(config)
    budget = float(config.get("daily_budget_usd", DEFAULT_BUDGET_USD))
    if per_turn is None:
        return True, "within the turn cap; cost unknown"
    projected = status["murakumo_turns_today"] * per_turn + per_turn
    if projected > budget:
        return False, "daily budget: %.4f + %.4f USD would exceed %.2f USD" % (
            status["murakumo_turns_today"] * per_turn, per_turn, budget)
    return True, "within budget"


def path_of(profile):
    return os.path.join(profile, "workspace", "review", "tick-status.json")


def read(profile):
    p = path_of(profile)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def write(profile, status):
    p = path_of(profile)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    return status


def main(argv=None):
    ap = argparse.ArgumentParser(description="Write workspace/review/tick-status.json.")
    ap.add_argument("--exit-code", type=int)
    args = ap.parse_args(argv)
    status = write(PROFILE, compute(PROFILE, datetime.now(timezone.utc), load_config(CONFIG),
                                    args.exit_code))
    print(json.dumps(status, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
