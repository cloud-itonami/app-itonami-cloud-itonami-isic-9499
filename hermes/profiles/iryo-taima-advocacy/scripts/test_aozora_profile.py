import copy
import unittest

import aozora_profile as ap

DECIDED = {"operator_name": "Example Labs", "operator_contact": "https://example.org/contact"}


class Profile(unittest.TestCase):
    def setUp(self):
        self.spec = ap.load()

    def test_shipped_profile_meets_the_rules(self):
        self.assertEqual([], ap.problems(self.spec))

    def test_render_refuses_until_the_owner_decides_the_operator(self):
        with self.assertRaises(ValueError) as cm:
            ap.render(self.spec)
        self.assertIn("operator_name is not decided", str(cm.exception))

    def test_rendered_record(self):
        rec = ap.render(dict(self.spec, **DECIDED))
        self.assertTrue(rec["displayName"].endswith("（bot）"))
        first = rec["description"].split("\n")[0]
        for part in ("bot", "Example Labs", "https://example.org/contact", "承認"):
            self.assertIn(part, first)
        self.assertIn("日本では大麻の所持・使用は違法です。医療の判断は医師に相談してください。",
                      rec["description"])
        self.assertEqual({"$type": "com.atproto.label.defs#selfLabels", "values": [{"val": "bot"}]},
                         rec["labels"])
        self.assertNotIn("avatar", rec)
        self.assertLessEqual(ap.post_guard.graphemes(rec["description"]), 256)

    def test_each_rule_is_checked(self):
        cases = {
            "displayName must end": lambda r: r.update(displayName="医療大麻の制度メモ"),
            "self-label bot missing": lambda r: r.pop("labels"),
            "policy notice": lambda r: r.update(description=r["description"].replace("違法です", "")),
            "first line must state: human approval":
                lambda r: r.update(description=r["description"].replace("承認", "確認", 1)),
            "description too long": lambda r: r.update(description=r["description"] + "あ" * 300),
            "avatar is set": lambda r: r.update(avatar={"$type": "blob"}),
        }
        for expect, change in cases.items():
            spec = copy.deepcopy(self.spec)
            change(spec["record"])
            self.assertTrue(any(expect in p for p in ap.problems(spec)), expect)


if __name__ == "__main__":
    unittest.main()
