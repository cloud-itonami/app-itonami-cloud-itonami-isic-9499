import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import aozora_post
import watch_sources
from review_store import Store

NOW = datetime(2026, 11, 21, 3, 0, tzinfo=timezone.utc)
SRC = "https://www.mhlw.go.jp/content/11120000/001302699.pdf"
GOOD = {"text": "施行通知が公開されています。 " + SRC, "sources": [SRC]}
CREDS = {"AOZORA_IDENTIFIER": "bot.example", "AOZORA_APP_PASSWORD": "test-value"}
# A first post five weeks ago: past the initial all-approval period.
OLD = {"at": (NOW - timedelta(weeks=5)).isoformat(), "hash": "0", "sources": []}


def settled():
    return {"posts": [dict(OLD)]}


def no_network(*_a, **_k):
    raise AssertionError("network must not be used")


class FakePds:
    def __init__(self):
        self.calls = []

    def __call__(self, url, body, headers):
        self.calls.append((url, body, headers))
        if url.endswith("createSession"):
            return {"did": "did:web:pds.example:bot", "accessJwt": "jwt"}
        return {"uri": "at://did:web:pds.example:bot/app.bsky.feed.post/1", "cid": "bafy"}


class Run(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = Store(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_refused_draft_sends_nothing(self):
        state = {"posts": []}
        code, result = aozora_post.run({"text": "入手方法を紹介 " + SRC, "sources": [SRC]},
                                       state, NOW, True, CREDS, self.store, http=no_network)
        self.assertEqual(1, code)
        self.assertEqual("refused", result["status"])
        self.assertEqual([], state["posts"])

    def test_without_execute_is_dry_run(self):
        code, result = aozora_post.run(GOOD, settled(), NOW, False, CREDS, self.store, http=no_network)
        self.assertEqual((0, "dry-run", "no --execute"), (code, result["status"], result["why"]))

    def test_without_credentials_is_dry_run(self):
        state = settled()
        code, result = aozora_post.run(GOOD, state, NOW, True, {}, self.store, http=no_network)
        self.assertEqual((0, "dry-run", "credentials not set"),
                         (code, result["status"], result["why"]))
        self.assertEqual([OLD], state["posts"])
        self.assertEqual("app.bsky.feed.post", result["record"]["$type"])

    def test_execute_posts_and_records(self):
        pds, state = FakePds(), settled()
        code, result = aozora_post.run(GOOD, state, NOW, True, CREDS, self.store,
                                       pds="https://pds.example", http=pds)
        self.assertEqual((0, "posted"), (code, result["status"]))
        self.assertEqual(["https://pds.example/xrpc/com.atproto.server.createSession",
                          "https://pds.example/xrpc/com.atproto.repo.createRecord"],
                         [c[0] for c in pds.calls])
        _, body, headers = pds.calls[1]
        self.assertEqual("did:web:pds.example:bot", body["repo"])
        self.assertEqual("Bearer jwt", headers["Authorization"])
        self.assertEqual(GOOD["text"], body["record"]["text"])
        self.assertEqual(2, len(state["posts"]))
        self.assertEqual(result["uri"], state["posts"][1]["uri"])

    def test_second_identical_post_is_refused_from_state(self):
        pds, state = FakePds(), settled()
        aozora_post.run(GOOD, state, NOW, True, CREDS, self.store, http=pds)
        code, result = aozora_post.run(GOOD, state, NOW, True, CREDS, self.store, http=no_network)
        self.assertEqual(1, code)
        self.assertIn("duplicate-text", result["reasons"])

    def test_send_failure_records_nothing_and_hides_detail(self):
        def failing(*_a, **_k):
            raise RuntimeError("secret detail")
        state = settled()
        code, result = aozora_post.run(GOOD, state, NOW, True, CREDS, self.store, http=failing)
        self.assertEqual((2, {"status": "send-failed", "error": "RuntimeError"}), (code, result))
        self.assertEqual([OLD], state["posts"])

    def test_link_facet_uses_utf8_byte_offsets(self):
        facet = aozora_post.build_record(GOOD["text"], NOW)["facets"][0]
        raw = GOOD["text"].encode("utf-8")
        self.assertEqual(SRC, raw[facet["index"]["byteStart"]:facet["index"]["byteEnd"]].decode())
        self.assertEqual(SRC, facet["features"][0]["uri"])


RDF = """<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel><title>feed</title></channel>
  <item><title>大麻草の栽培の規制に関する
    省令案に係る意見募集</title><link>https://public-comment.e-gov.go.jp/a</link>
    <dc:date>2026-11-20T12:00:00+09:00</dc:date></item>
  <item><title>消防法施行令の一部改正</title><link>https://public-comment.e-gov.go.jp/b</link></item>
  <item><title>カンナビジオールの審査報告書</title><link>https://www.pmda.go.jp/c</link></item>
</rdf:RDF>""".encode("utf-8")


class Watch(unittest.TestCase):
    def test_parse_items(self):
        items = watch_sources.parse_items(RDF)
        self.assertEqual(3, len(items))
        self.assertEqual({"title": "大麻草の栽培の規制に関する 省令案に係る意見募集",
                          "link": "https://public-comment.e-gov.go.jp/a",
                          "date": "2026-11-20T12:00:00+09:00"}, items[0])

    def test_select_new_filters_by_keyword_and_seen(self):
        items = watch_sources.parse_items(RDF)
        self.assertEqual(["https://public-comment.e-gov.go.jp/a", "https://www.pmda.go.jp/c"],
                         [i["link"] for i in watch_sources.select_new(items, {})])

    def test_item_is_reported_again_until_the_limit(self):
        items = watch_sources.parse_items(RDF)
        a = "https://public-comment.e-gov.go.jp/a"
        below = {a: watch_sources.MAX_REPORTS - 1}
        self.assertIn(a, [i["link"] for i in watch_sources.select_new(items, below)])
        at_limit = {a: watch_sources.MAX_REPORTS}
        self.assertEqual(["https://www.pmda.go.jp/c"],
                         [i["link"] for i in watch_sources.select_new(items, at_limit)])

    def test_first_state_format_counts_as_done(self):
        reported = watch_sources.load_reported({"seen": ["https://x/a"], "reported": {"https://x/b": 1}})
        self.assertEqual({"https://x/a": watch_sources.MAX_REPORTS, "https://x/b": 1}, reported)


if __name__ == "__main__":
    unittest.main()
