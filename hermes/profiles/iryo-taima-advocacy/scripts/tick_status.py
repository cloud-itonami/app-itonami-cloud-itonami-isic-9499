"""Machine-readable status of the iryo-taima-advocacy tick (AWAI-3658).

Writes workspace/review/tick-status.json from the host's own run records
(cron/itonami-runs.jsonl and cron/usage_audit.jsonl), for the Orca automation:

  {"updated_at", "job_id", "last_run", "last_status", "exit_code",
   "consecutive_failures", "last_error", "day", "runs_today",
   "murakumo_turns_today", "murakumo_requests_today", "tokens_today",
   "est_cost_usd_today", "cost_basis", "daily_budget_usd",
   "agent_skipped_today", "posting_enabled"}

Run after each tick (the scheduled task does), optionally with the launcher's
exit code:  python3 scripts/tick_status.py [--exit-code N]
scripts/tick.py also refreshes it before deciding whether today's budget
allows an agent turn.

Cost: murakumo publishes no price for the mishima fleet (checked 2026-11-22:
/v1/models carries no price field), so est_cost_usd_today is null and
cost_basis says so, unless review_config.json sets "usd_per_turn". Turns,
requests and tokens are always recorded.
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
    turns = sum(int(r.get("turns") or 0) for r in today(runs))
    requests = sum(1 for r in today(runs) for a in r.get("attempts") or []
                   if a.get("provider") == "murakumo")
    tokens = sum(int(u.get("total_tokens") or 0) for u in today(usage))
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
        "murakumo_requests_today": requests,
        "tokens_today": tokens,
        "est_cost_usd_today": round(turns * per_turn, 4) if per_turn is not None else None,
        "cost_basis": ("usd_per_turn=%s from review_config.json" % per_turn if per_turn is not None
                       else "unknown: murakumo publishes no price for mishima; turns and tokens recorded"),
        "daily_budget_usd": config.get("daily_budget_usd", DEFAULT_BUDGET_USD),
        "agent_skipped_today": skipped,
        "posting_enabled": posting_enabled(config),
    }


def budget_allows_turn(status, config):
    """(allowed, reason). With a known per-turn cost, refuse a turn that would
    take today's estimate over the budget. With no known cost the budget cannot
    be applied: allowed, and the reason says so."""
    per_turn = usd_per_turn(config)
    budget = float(config.get("daily_budget_usd", DEFAULT_BUDGET_USD))
    if per_turn is None:
        return True, "cost unknown; budget not applied (turns recorded)"
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
