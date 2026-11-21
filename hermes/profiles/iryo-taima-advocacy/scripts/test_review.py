import io
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import aozora_post
import review
import review_queue
import tick
from review_store import ReviewError, Store, approvers

NOW = datetime(2026, 11, 22, 3, 0, tzinfo=timezone.utc)
SRC = "https://www.mhlw.go.jp/content/11120000/001302699.pdf"
DOI = "https://doi.org/10.1056/NEJMoa1611618"
SRC2 = "https://public-comment.e-gov.go.jp/servlet/Public?CLASSNAME=PCMMSTDETAIL&id=495240000"
CREDS = {"AOZORA_IDENTIFIER": "bot.example", "AOZORA_APP_PASSWORD": "test-value"}
PLAIN = {"text": "大麻草の栽培の規制に関する省令の施行通知が公開されました。 " + SRC, "sources": [SRC]}


def at(days):
    return {"at": (NOW - timedelta(days=days)).isoformat(), "hash": "h%d" % days, "sources": []}


def no_network(*_a, **_k):
    raise AssertionError("network must not be used")


class FakePds:
    def __init__(self):
        self.calls = []

    def __call__(self, url, body, headers):
        self.calls.append(url)
        if url.endswith("createSession"):
            return {"did": "did:web:pds.example:bot", "accessJwt": "jwt"}
        return {"uri": "at://did:web:pds.example:bot/app.bsky.feed.post/9", "cid": "bafy9"}


class Holds(unittest.TestCase):
    def test_memo_blocklist_holds(self):
        cases = {
            "入手できる場所の話": "acquisition", "購入について": "acquisition",
            "売ります": "acquisition", "譲ります": "acquisition", "個人輸入の制度": "acquisition",
            "栽培方法の規制": "cultivation", "吸い方": "use-method", "用量の基準": "dosage",
            "連絡先はこちら": "contact", "info@example.com まで": "contact",
            "03-1234-5678": "contact", "クリニックの案内": "place",
            "詳しくは https://bit.ly/abc": "short-url",
        }
        for text, reason in cases.items():
            self.assertIn(reason, review.holds(text), text)

    def test_source_url_digits_are_not_a_phone_number(self):
        self.assertEqual([], review.holds(PLAIN["text"]))

    def test_product_with_efficacy_in_one_sentence(self):
        self.assertIn("product-with-efficacy", review.holds("エピディオレックスは発作を改善する。"))
        self.assertNotIn("product-with-efficacy",
                         review.holds("エピディオレックスの審査が始まった。発作の改善が研究課題だ。"))

    def test_sensitive_topics(self):
        self.assertEqual(["patient-voice"], review.sensitive_topics("患者さんの声を紹介します"))
        self.assertEqual(["product-or-drug"], review.sensitive_topics("CBD の規制"))
        self.assertEqual(["abroad"], review.sensitive_topics("ドイツの制度"))
        self.assertEqual([], review.sensitive_topics(PLAIN["text"]))


class Assess(unittest.TestCase):
    def test_first_four_weeks_hold_every_draft(self):
        self.assertEqual({"decision": "hold", "reasons": ["initial-period"]},
                         review.assess(PLAIN, NOW, None))
        self.assertEqual("hold", review.assess(PLAIN, NOW, NOW - timedelta(days=27))["decision"])

    def test_after_four_weeks_plain_draft_needs_no_approval(self):
        self.assertEqual({"decision": "send", "reasons": []},
                         review.assess(PLAIN, NOW, NOW - timedelta(days=29)))

    def test_sensitive_topic_always_holds(self):
        draft = {"text": "ドイツの医療用大麻の制度。" + review.DISCLAIMER + " " + SRC}
        self.assertEqual({"decision": "hold", "reasons": ["topic:abroad"]},
                         review.assess(draft, NOW, NOW - timedelta(days=100)))

    def test_out_of_scope_topics_hold_forever(self):
        """Owner decision 2026-11-22 (Legal memo): the bot starts with 制度・研究・
        政策提言 only. Patient voices, product or drug names and use abroad are
        held for a human at any age of the account, not only in the first 4 weeks."""
        d = review.DISCLAIMER
        cases = {
            "patient-voice": "患者さんの声を紹介します。制度の見直しを求めています。",
            "product-or-drug": "エピディオレックスの承認審査について。" + d,
            "abroad": "カナダの医療用大麻制度について。" + d,
        }
        for topic, text in cases.items():
            for weeks in (5, 52, 520):
                got = review.assess({"text": text + " " + SRC}, NOW, NOW - timedelta(weeks=weeks))
                self.assertEqual("hold", got["decision"], (topic, weeks))
                self.assertIn("topic:" + topic, got["reasons"], (topic, weeks))
                self.assertNotIn("initial-period", got["reasons"])

    def test_in_scope_policy_post_needs_no_approval_after_week_4(self):
        text = "大麻由来医薬品の治験の拡大を求めるパブリックコメントの募集が始まりました。 " + SRC
        self.assertEqual({"decision": "send", "reasons": []},
                         review.assess({"text": text}, NOW, NOW - timedelta(weeks=5)))

    def test_there_is_no_reply_path(self):
        import aozora_post as ap
        import tick as tk
        for mod in (review, ap, tk):
            self.assertFalse([n for n in dir(mod) if "reply" in n.lower() and n != "referral_reply"],
                             mod.__name__)

    def test_drug_or_abroad_without_disclaimer_must_be_fixed(self):
        draft = {"text": "ドイツの医療用大麻の制度。 " + SRC}
        self.assertEqual({"decision": "fix", "reasons": ["disclaimer-required"]},
                         review.assess(draft, NOW, None))

    def test_referral_reply(self):
        self.assertEqual(review.REFERRAL_TEXT, review.referral_reply("どこで買えますか？"))
        self.assertEqual(review.REFERRAL_TEXT, review.referral_reply("私の症状に効きますか"))
        self.assertIsNone(review.referral_reply("施行日はいつですか"))
        self.assertIn(review.DISCLAIMER, review.REFERRAL_TEXT)


class Pipeline(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.dir, "review"))

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_no_source_is_refused_not_queued(self):
        code, result = aozora_post.run({"text": "通知が出ました。", "sources": []}, {"posts": []},
                                       NOW, True, CREDS, self.store, http=no_network)
        self.assertEqual((1, "refused"), (code, result["status"]))
        self.assertIn("no-source", result["reasons"])
        self.assertEqual([], self.store.items())
        self.assertEqual("refused", self.store.read_log()[-1]["status"])

    def test_doi_link_is_an_allowed_source(self):
        draft = {"text": "発作の頻度を調べた試験の報告がある。 " + DOI, "sources": [DOI]}
        code, result = aozora_post.run(draft, {"posts": []}, NOW, True, CREDS, self.store,
                                       http=no_network)
        self.assertEqual((0, "queued"), (code, result["status"]))

    def test_held_draft_is_queued_and_not_sent(self):
        state = {"posts": []}
        code, result = aozora_post.run(PLAIN, state, NOW, True, CREDS, self.store, http=no_network)
        self.assertEqual((0, "queued", ["initial-period"]),
                         (code, result["status"], result["reasons"]))
        self.assertEqual([], state["posts"])
        item = self.store.get(result["id"])
        self.assertEqual(("pending", PLAIN), (item["status"], item["draft"]))
        self.assertEqual("queued", self.store.read_log()[-1]["event"])

    def test_approve_then_send_records_approver_and_post(self):
        state = {"posts": []}
        _, queued = aozora_post.run(PLAIN, state, NOW, True, CREDS, self.store, http=no_network)
        later = NOW + timedelta(hours=1)
        item = self.store.decide(queued["id"], True, "reviewer-a", ["reviewer-a"], later)
        self.assertEqual(("approved", "reviewer-a", later.isoformat()),
                         (item["status"], item["approver"], item["decided_at"]))
        pds = FakePds()
        code, result = aozora_post.send_approved(queued["id"], state, later, True, CREDS,
                                                 self.store, pds="https://pds.example", http=pds)
        self.assertEqual((0, "posted"), (code, result["status"]))
        self.assertEqual(1, len(state["posts"]))
        done = self.store.get(queued["id"])
        self.assertEqual(("posted", result["uri"]), (done["status"], done["posted"]["uri"]))
        self.assertEqual(["queued", "approved", "posted"],
                         [e["event"] for e in self.store.read_log()])

    def test_unapproved_draft_cannot_be_sent(self):
        _, queued = aozora_post.run(PLAIN, {"posts": []}, NOW, True, CREDS, self.store,
                                    http=no_network)
        code, result = aozora_post.send_approved(queued["id"], {"posts": []}, NOW, True, CREDS,
                                                 self.store, http=no_network)
        self.assertEqual((1, ["not-approved:pending"]), (code, result["reasons"]))

    def test_send_time_guard_still_applies(self):
        state = {"posts": [at(0)]}  # a post just now: too soon
        _, queued = aozora_post.run(PLAIN, {"posts": []}, NOW, True, CREDS, self.store,
                                    http=no_network)
        self.store.decide(queued["id"], True, "a", ["a"], NOW)
        code, result = aozora_post.send_approved(queued["id"], state, NOW, True, CREDS,
                                                 self.store, http=no_network)
        self.assertEqual(1, code)
        self.assertIn("too-soon", result["reasons"])
        self.assertEqual("approved", self.store.get(queued["id"])["status"])

    def test_approval_needs_a_configured_approver(self):
        _, queued = aozora_post.run(PLAIN, {"posts": []}, NOW, True, CREDS, self.store,
                                    http=no_network)
        with self.assertRaises(ReviewError):
            self.store.decide(queued["id"], True, "anyone", [], NOW)
        with self.assertRaises(ReviewError):
            self.store.decide(queued["id"], True, "mallory", ["a"], NOW)
        self.assertEqual("pending", self.store.get(queued["id"])["status"])

    def test_rejected_draft_stays_rejected(self):
        _, queued = aozora_post.run(PLAIN, {"posts": []}, NOW, True, CREDS, self.store,
                                    http=no_network)
        self.store.decide(queued["id"], False, "a", ["a"], NOW, "出典が二次資料")
        with self.assertRaises(ReviewError):
            self.store.decide(queued["id"], True, "a", ["a"], NOW)
        self.assertEqual("rejected", self.store.get(queued["id"])["status"])

    def test_approvers_from_env_override_config(self):
        self.assertEqual(["x", "y"], approvers({"approvers": ["c"]}, {"ITONAMI_REVIEW_APPROVERS": "x, y"}))
        self.assertEqual(["c"], approvers({"approvers": ["c"]}, {}))
        self.assertEqual([], approvers({}, {}))

    def test_invalid_id_is_refused(self):
        with self.assertRaises(ReviewError):
            self.store.get("../../etc/passwd")


class Cli(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.review = os.path.join(self.dir, "review")
        self.config = os.path.join(self.dir, "review_config.json")
        with open(self.config, "w", encoding="utf-8") as f:
            json.dump({"approvers": ["reviewer-a"]}, f)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def cli(self, *args, env=None):
        out = io.StringIO()
        code = review_queue.main(["--review-dir", self.review, "--config", self.config, *args],
                                 env=env or {}, now=NOW, out=out)
        return code, out.getvalue()

    def test_list_and_approve(self):
        _, queued = aozora_post.run(PLAIN, {"posts": []}, NOW, True, CREDS, Store(self.review),
                                    http=no_network)
        code, out = self.cli("list", "--status", "pending")
        self.assertEqual(0, code)
        self.assertIn(queued["id"], out)
        self.assertEqual(1, self.cli("approve", queued["id"], "--approver", "nobody")[0])
        code, out = self.cli("approve", queued["id"], "--approver", "reviewer-a")
        self.assertEqual((0, "approved"), (code, json.loads(out)["status"]))

    def test_shipped_config_owner_approves_and_posting_is_off(self):
        with open(review_queue.DEFAULT_CONFIG, encoding="utf-8") as f:
            config = json.load(f)
        self.assertEqual(["owner"], config["approvers"])
        self.assertIs(False, config["posting_enabled"])

    def test_orca_bridge_surface(self):
        store = Store(self.review)
        _, a = aozora_post.run(PLAIN, {"posts": []}, NOW, True, CREDS, store, http=no_network)
        other = {"text": "意見募集が始まりました。 " + SRC2, "sources": [SRC2]}
        _, b = aozora_post.run(other, {"posts": []}, NOW, True, CREDS, store, http=no_network)
        code, out = self.cli("list", "--pending", "--json")
        listed = json.loads(out)
        self.assertEqual(0, code)
        self.assertEqual([a["id"], b["id"]], [i["id"] for i in listed])
        self.assertEqual({"id", "status", "text", "sources", "reasons", "queued_at", "approver",
                          "decided_at"}, set(listed[0]))
        self.assertEqual((PLAIN["text"], [SRC], ["initial-period"]),
                         (listed[0]["text"], listed[0]["sources"], listed[0]["reasons"]))
        code, out = self.cli("approve", a["id"], "--by", "reviewer-a")
        self.assertEqual(("approved", "reviewer-a", NOW.isoformat()),
                         tuple(json.loads(out)[k] for k in ("status", "approver", "decided_at")))
        code, out = self.cli("reject", b["id"], "--by", "reviewer-a", "--reason", "二次資料")
        self.assertEqual(("rejected", "二次資料"),
                         tuple(json.loads(out)[k] for k in ("status", "decision_note")))
        self.assertEqual([], json.loads(self.cli("list", "--pending", "--json")[1]))
        events = json.loads(self.cli("log", "--json")[1])
        self.assertEqual(["queued", "queued", "approved", "rejected"], [e["event"] for e in events])

    def test_reject_needs_a_reason(self):
        _, a = aozora_post.run(PLAIN, {"posts": []}, NOW, True, CREDS, Store(self.review),
                               http=no_network)
        with self.assertRaises(SystemExit):
            self.cli("reject", a["id"], "--by", "reviewer-a")

    def test_queue_shim_runs_from_anywhere(self):
        import subprocess
        shim = os.path.join(os.path.dirname(os.path.abspath(review_queue.__file__)), "queue")
        r = subprocess.run([shim, "--review-dir", self.review, "list", "--pending", "--json"],
                           capture_output=True, text=True, cwd=self.dir, timeout=60)
        self.assertEqual((0, []), (r.returncode, json.loads(r.stdout)))


class Tick(unittest.TestCase):
    def setUp(self):
        self.profile = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.profile, "workspace", "review"))
        self.on = os.path.join(self.profile, "on.json")
        with open(self.on, "w", encoding="utf-8") as f:
            json.dump({"posting_enabled": True}, f)

    def tearDown(self):
        shutil.rmtree(self.profile)

    def queue(self, draft):
        _, r = aozora_post.run(draft, {"posts": []}, NOW, True, CREDS, self.store, http=no_network)
        return r["id"]

    def test_nothing_approved_sends_nothing(self):
        self.queue(PLAIN)
        self.assertIsNone(tick.send_step(NOW, CREDS, self.profile, self.on, http=no_network))

    def test_approved_draft_is_sent_on_the_next_tick(self):
        item = self.queue(PLAIN)
        self.store.decide(item, True, "owner", ["owner"], NOW)
        pds = FakePds()
        result = tick.send_step(NOW, CREDS, self.profile, self.on, http=pds)
        self.assertEqual("posted", result["status"])
        self.assertEqual("posted", self.store.get(item)["status"])
        with open(os.path.join(self.profile, "workspace", "post-state.json"), encoding="utf-8") as f:
            self.assertEqual(result["uri"], json.load(f)["posts"][0]["uri"])
        self.assertIsNone(tick.send_step(NOW, CREDS, self.profile, self.on, http=no_network))

    def test_rejected_draft_is_never_sent(self):
        item = self.queue(PLAIN)
        self.store.decide(item, False, "owner", ["owner"], NOW, "no")
        self.assertIsNone(tick.send_step(NOW, CREDS, self.profile, self.on, http=no_network))

    def test_posting_switch_off_is_a_dry_run_and_keeps_the_approval(self):
        item = self.queue(PLAIN)
        self.store.decide(item, True, "owner", ["owner"], NOW)
        result = tick.send_step(NOW, CREDS, self.profile, review_queue.DEFAULT_CONFIG,
                                http=no_network)
        self.assertEqual("dry-run", result["status"])
        self.assertIn("posting disabled", result["why"])
        self.assertNotIn("record", result)
        self.assertEqual("approved", self.store.get(item)["status"])


class QueueRules(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = Store(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_same_draft_is_queued_once(self):
        _, a = aozora_post.run(PLAIN, {"posts": []}, NOW, True, CREDS, self.store, http=no_network)
        _, b = aozora_post.run(PLAIN, {"posts": []}, NOW + timedelta(hours=6), True, CREDS,
                               self.store, http=no_network)
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(1, len(self.store.items()))

    def test_timing_limits_do_not_block_queuing(self):
        code, result = aozora_post.run(PLAIN, {"posts": [at(0)]}, NOW, True, CREDS, self.store,
                                       http=no_network)
        self.assertEqual((0, "queued"), (code, result["status"]))

    def test_timing_limits_block_a_direct_send(self):
        state = {"posts": [at(40), at(0)]}
        code, result = aozora_post.run(PLAIN, state, NOW, True, CREDS, self.store, http=no_network)
        self.assertEqual((1, ["too-soon"]), (code, result["reasons"]))

    def test_posting_switch_off_means_dry_run_for_direct_send(self):
        code, result = aozora_post.run(PLAIN, {"posts": [at(40)]}, NOW, True, CREDS, self.store,
                                       http=no_network, enabled=False)
        self.assertEqual((0, "dry-run"), (code, result["status"]))


if __name__ == "__main__":
    unittest.main()
