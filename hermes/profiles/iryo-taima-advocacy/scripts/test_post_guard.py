import unittest
from datetime import datetime, timedelta, timezone

import post_guard

NOW = datetime(2026, 11, 21, 3, 0, tzinfo=timezone.utc)
SRC = "https://www.mhlw.go.jp/content/11120000/001302699.pdf"


def draft(text, sources=(SRC,)):
    return {"text": text, "sources": list(sources)}


def reasons(text, sources=(SRC,), history=()):
    return post_guard.check(draft(text, sources), list(history), NOW)["reasons"]


def posted(hours_ago, text="前の投稿", sources=("https://www.pmda.go.jp/x",)):
    return {"at": NOW - timedelta(hours=hours_ago), "hash": post_guard.text_hash(text),
            "sources": list(sources)}


class Accept(unittest.TestCase):
    def test_sourced_summary_passes(self):
        text = "厚生労働省が大麻由来医薬品の施行通知を公開しました。制度の概要を確認できます。 " + SRC
        self.assertEqual({"ok": True, "reasons": []},
                         post_guard.check(draft(text), [], NOW))

    def test_treatment_words_are_not_cure_claims(self):
        text = "難治性てんかんの治療薬について、治験の継続が審査されました。 " + SRC
        self.assertEqual([], reasons(text))


class Refuse(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(["empty-text"], reasons("   "))

    def test_no_source(self):
        self.assertIn("no-source", reasons("制度が変わりました。", sources=()))

    def test_source_host_not_allowed(self):
        bad = "https://example.com/cbd"
        self.assertIn("source-not-allowed:" + bad, reasons("解説です " + bad, sources=(bad,)))

    def test_lookalike_host_not_allowed(self):
        bad = "https://mhlw.go.jp.example.com/a"
        self.assertIn("source-not-allowed:" + bad, reasons("解説です " + bad, sources=(bad,)))

    def test_http_source_not_allowed(self):
        bad = "http://www.mhlw.go.jp/a"
        self.assertIn("source-not-allowed:" + bad, reasons("解説です " + bad, sources=(bad,)))

    def test_source_must_appear_in_text(self):
        self.assertIn("source-url-not-in-text", reasons("出典を書いていない投稿です。"))

    def test_extra_link_must_be_allowed(self):
        text = "通知です " + SRC + " 詳しくは https://shop.example.com/"
        self.assertIn("link-not-allowed:https://shop.example.com/", reasons(text))

    def test_too_long(self):
        self.assertIn("too-long:301", reasons("あ" * (301 - len(SRC) - 1) + " " + SRC))

    def test_acquisition_guidance(self):
        for t in ("海外からの個人輸入で入手方法があります", "どこで買えるかを紹介", "栽培方法を解説"):
            self.assertIn("banned:acquisition-guidance", reasons(t + " " + SRC), t)

    def test_dosing_advice(self):
        for t in ("1日20mgから始めます", "適切な用量の目安", "１０ｍｇで十分"):
            self.assertIn("banned:dosing-advice", reasons(t + " " + SRC), t)

    def test_cure_claim(self):
        for t in ("てんかんが治る", "がんが消えた例", "副作用はない"):
            self.assertIn("banned:cure-claim", reasons(t + " " + SRC), t)

    def test_product_promotion(self):
        for t in ("おすすめのCBDオイル", "クーポンあり", "このブランドが安心"):
            self.assertIn("banned:product-promotion", reasons(t + " " + SRC), t)

    def test_recreational_advocacy(self):
        self.assertIn("banned:recreational-advocacy",
                      reasons("嗜好用大麻の合法化を求めます " + SRC))

    def test_encouraging_use(self):
        self.assertIn("banned:encouraging-use", reasons("一度試してみてください " + SRC))

    def test_daily_limit(self):
        hist = [posted(20, "a"), posted(12, "b"), posted(4, "c")]
        self.assertIn("daily-limit", reasons("通知です " + SRC, history=hist))

    def test_too_soon(self):
        self.assertIn("too-soon", reasons("通知です " + SRC, history=[posted(1)]))

    def test_duplicate_text_ignores_spacing_and_width(self):
        hist = [posted(30, "通知です " + SRC)]
        self.assertIn("duplicate-text", reasons("通知です　" + SRC, history=hist))

    def test_same_source_within_a_week(self):
        hist = [posted(48, "別の文面", sources=(SRC,))]
        self.assertIn("source-posted-recently", reasons("通知です " + SRC, history=hist))

    def test_same_source_after_a_week_passes(self):
        hist = [posted(24 * 8, "別の文面", sources=(SRC,))]
        self.assertEqual([], reasons("通知です " + SRC, history=hist))

    def test_malformed_draft(self):
        self.assertEqual({"ok": False, "reasons": ["empty-text"]},
                         post_guard.check(None, [], NOW))


class Graphemes(unittest.TestCase):
    def test_counts(self):
        self.assertEqual(3, post_guard.graphemes("大麻草"))
        self.assertEqual(1, post_guard.graphemes("が"))        # combining dakuten
        self.assertEqual(1, post_guard.graphemes("👨‍👩"))        # joined pair


if __name__ == "__main__":
    unittest.main()
