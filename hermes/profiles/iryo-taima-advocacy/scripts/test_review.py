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
from review_store import ReviewError, Store, approvers

NOW = datetime(2026, 11, 22, 3, 0, tzinfo=timezone.utc)
SRC = "https://www.mhlw.go.jp/content/11120000/001302699.pdf"
DOI = "https://doi.org/10.1056/NEJMoa1611618"
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

    def test_shipped_config_has_no_approver(self):
        with open(review_queue.DEFAULT_CONFIG, encoding="utf-8") as f:
            self.assertEqual([], json.load(f)["approvers"])


if __name__ == "__main__":
    unittest.main()
