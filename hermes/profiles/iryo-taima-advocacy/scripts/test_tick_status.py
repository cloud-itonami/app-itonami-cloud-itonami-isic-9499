import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from unittest import mock

import tick
import tick_status

NOW = datetime(2026, 11, 22, 3, 0, tzinfo=timezone.utc)  # 12:00 JST
E503 = '{"code" "fleet_capacity_unavailable", "executed" false}'


def run(ts, status, turns=0, attempts=0):
    return {"ts": ts, "job_id": tick_status.JOB_ID, "status": status, "turns": turns,
            "exit_reason": "provider_error" if status == "error" else "final",
            "attempts": [{"provider": "murakumo", "model": "mishima", "status": 503, "error": E503}
                         for _ in range(attempts)]}


class Status(unittest.TestCase):
    def setUp(self):
        self.profile = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.profile, "cron"))
        self.config = os.path.join(self.profile, "config.json")

    def tearDown(self):
        shutil.rmtree(self.profile)

    def write_runs(self, rows, usage=()):
        with open(os.path.join(self.profile, "cron", "itonami-runs.jsonl"), "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        with open(os.path.join(self.profile, "cron", "usage_audit.jsonl"), "w") as f:
            for u in usage:
                f.write(json.dumps(u) + "\n")

    def set_config(self, **kw):
        with open(self.config, "w") as f:
            json.dump(kw, f)

    def test_no_runs(self):
        s = tick_status.compute(self.profile, NOW, {})
        self.assertEqual((None, 0, 0, None), (s["last_run"], s["consecutive_failures"],
                                               s["murakumo_turns_today"], s["est_cost_usd_today"]))
        self.assertIn("unknown", s["cost_basis"])

    def test_failures_turns_and_tokens(self):
        self.write_runs([
            run("2026-11-21T06:23:00+09:00", "ok", turns=4),             # yesterday (JST)
            run("2026-11-22T00:23:00+09:00", "ok", turns=3),
            run("2026-11-22T06:23:00+09:00", "error", attempts=3),
            run("2026-11-22T11:23:00+09:00", "error", attempts=3),
            {"ts": "2026-11-22T11:30:00+09:00", "job_id": "other", "status": "error"},
        ], usage=[{"ts": "2026-11-22T00:23:00+09:00", "job_id": tick_status.JOB_ID, "total_tokens": 900},
                  {"ts": "2026-11-21T06:23:00+09:00", "job_id": tick_status.JOB_ID, "total_tokens": 50}])
        s = tick_status.compute(self.profile, NOW, {})
        self.assertEqual("2026-11-22T11:23:00+09:00", s["last_run"])
        self.assertEqual((1, 2), (s["exit_code"], s["consecutive_failures"]))
        self.assertEqual(("provider_error", E503, 3),
                         tuple(s["last_error"][k] for k in ("exit_reason", "error", "attempts")))
        # 3 agent turns + 6 failed murakumo attempts (retries count): 9 turns today
        self.assertEqual((3, 9, 3, 6, 900),
                         (s["runs_today"], s["murakumo_turns_today"], s["agent_turns_today"],
                          s["murakumo_failed_requests_today"], s["tokens_today"]))
        self.assertIsNone(s["est_cost_usd_today"])
        # successful runs over all days: yesterday 4 turns / 50 tokens, today 3 / 900
        self.assertEqual((2, 7, 950, 135.7),
                         (s["successful_runs_total"], s["successful_turns_total"],
                          s["tokens_total_successful"], s["tokens_per_successful_turn"]))

    def test_success_resets_failures_and_exit_code_can_be_given(self):
        self.write_runs([run("2026-11-22T06:23:00+09:00", "error", attempts=3),
                         run("2026-11-22T11:23:00+09:00", "silent")])
        s = tick_status.compute(self.profile, NOW, {}, exit_code=0)
        self.assertEqual((0, 0, None), (s["exit_code"], s["consecutive_failures"], s["last_error"]))

    def test_decision_budget_four_turns_a_day(self):
        config = {"usd_per_turn": 0.25, "daily_budget_usd": 1.0, "max_murakumo_turns_per_day": 4}
        self.write_runs([run("2026-11-22T00:23:00+09:00", "ok", turns=3)])
        s = tick_status.compute(self.profile, NOW, config)
        self.assertEqual((3, 0.75), (s["murakumo_turns_today"], s["est_cost_usd_today"]))
        self.assertEqual(True, tick_status.budget_allows_turn(s, config)[0])  # 0.75 + 0.25 = 1.0
        self.write_runs([run("2026-11-22T00:23:00+09:00", "ok", turns=4)])
        allowed, why = tick_status.budget_allows_turn(tick_status.compute(self.profile, NOW, config),
                                                      config)
        self.assertEqual(False, allowed)
        self.assertIn("turn cap", why)

    def test_in_run_retries_count_toward_the_cap(self):
        config = {"usd_per_turn": 0.25, "max_murakumo_turns_per_day": 4}
        self.write_runs([run("2026-11-22T00:23:00+09:00", "error", attempts=3),
                         run("2026-11-22T06:23:00+09:00", "ok", turns=1)])
        s = tick_status.compute(self.profile, NOW, config)
        self.assertEqual((4, 1.0), (s["murakumo_turns_today"], s["est_cost_usd_today"]))
        self.assertFalse(tick_status.budget_allows_turn(s, config)[0])

    def test_cap_resets_on_the_next_jst_day(self):
        config = {"usd_per_turn": 0.25, "max_murakumo_turns_per_day": 4}
        self.write_runs([run("2026-11-21T18:23:00+09:00", "error", attempts=3),
                         run("2026-11-21T23:30:00+09:00", "error", attempts=3)])
        s = tick_status.compute(self.profile, NOW, config)
        self.assertEqual(0, s["murakumo_turns_today"])
        self.assertTrue(tick_status.budget_allows_turn(s, config)[0])

    def test_cost_cap_applies_below_the_turn_cap(self):
        config = {"usd_per_turn": 0.5, "daily_budget_usd": 1.0, "max_murakumo_turns_per_day": 10}
        self.write_runs([run("2026-11-22T00:23:00+09:00", "ok", turns=2)])
        allowed, why = tick_status.budget_allows_turn(tick_status.compute(self.profile, NOW, config),
                                                      config)
        self.assertEqual(False, allowed)
        self.assertIn("exceed 1.00", why)

    def test_unknown_cost_still_applies_the_turn_cap(self):
        allowed, why = tick_status.budget_allows_turn(tick_status.compute(self.profile, NOW, {}), {})
        self.assertEqual((True, "within the turn cap; cost unknown"), (allowed, why))

    def test_shipped_config_is_the_decision(self):
        with open(tick_status.CONFIG, encoding="utf-8") as f:
            config = json.load(f)
        self.assertEqual((0.25, 4, 1.0, False),
                         (config["usd_per_turn"], config["max_murakumo_turns_per_day"],
                          config["daily_budget_usd"], config["posting_enabled"]))

    def test_budget_skip_does_not_run_the_watcher(self):
        self.write_runs([run("2026-11-22T00:23:00+09:00", "ok", turns=10)])
        self.set_config(usd_per_turn=0.5, daily_budget_usd=1.0)
        with mock.patch.object(tick.aozora_post, "DEFAULT_CONFIG", self.config):
            reason = tick.budget_step(NOW, self.profile, self.config)
        self.assertIn("turn cap", reason)
        s = tick_status.read(self.profile)
        self.assertEqual(reason, s["agent_skipped_today"]["reason"])
        with open(os.path.join(self.profile, "workspace", "review", "log.jsonl")) as f:
            self.assertEqual("agent-turn-skipped", json.loads(f.readline())["event"])
        out = io.StringIO()
        with mock.patch.object(tick, "PROFILE", self.profile), \
             mock.patch.object(tick, "budget_step", lambda now: reason), \
             mock.patch.object(tick.watch_sources, "main", side_effect=AssertionError("ran")), \
             redirect_stdout(out):
            self.assertEqual(0, tick.main())
        self.assertEqual({"wakeAgent": False}, json.loads(out.getvalue().strip().splitlines()[-1]))

    def test_within_budget_runs_the_watcher(self):
        out = io.StringIO()
        with mock.patch.object(tick, "budget_step", lambda now: None), \
             mock.patch.object(tick, "send_step", lambda now, env: None), \
             mock.patch.object(tick.watch_sources, "main", return_value=0) as watch, \
             redirect_stdout(out):
            self.assertEqual(0, tick.main())
        watch.assert_called_once()

    def test_main_writes_the_file(self):
        self.write_runs([run("2026-11-22T00:23:00+09:00", "error", attempts=3)])
        with mock.patch.object(tick_status, "PROFILE", self.profile), \
             mock.patch.object(tick_status, "CONFIG", self.config), redirect_stdout(io.StringIO()):
            self.set_config(posting_enabled=False)
            tick_status.main(["--exit-code", "1"])
        s = tick_status.read(self.profile)
        self.assertEqual((1, 1, False), (s["exit_code"], s["consecutive_failures"], s["posting_enabled"]))


if __name__ == "__main__":
    unittest.main()
