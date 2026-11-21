import base64
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import aozora_identity
import aozora_post
from review_store import Store

NOW = datetime(2026, 11, 22, 3, 0, tzinfo=timezone.utc)
DID = "did:key:z6MkTestBot"
CFG = {"aozora": {"did": DID, "keychain_service": "svc", "keychain_account": "acct"}}
NODE = next((p for p in (shutil.which("node"), *aozora_identity.NODE_CANDIDATES)
             if p and os.access(p, os.X_OK)), None)


class Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class FakeRun:
    """Records calls; answers `security` with a seed and node with `reply`."""

    def __init__(self, reply, seed="SEED"):
        self.calls, self.reply, self.seed = [], reply, seed

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if argv[0] == "/usr/bin/security":
            return Proc(0 if self.seed else 44, (self.seed or "") + "\n")
        return self.reply


def token_reply(**over):
    t = {"token": "EnQKCg", "tokenType": "Biscuit", "holder": DID,
         "audience": "did:web:pds.aozora.app", "permissions": ["repo:post"],
         "expiresAt": (NOW + timedelta(minutes=15)).isoformat().replace("+00:00", "Z")}
    t.update(over)
    return Proc(0, json.dumps(t))


class Biscuit(unittest.TestCase):
    def test_seed_from_keychain_goes_to_the_child_env_only(self):
        run = FakeRun(token_reply())
        out = aozora_identity.biscuit_for_post({"PATH": "/usr/bin"}, CFG, run, NOW)
        self.assertEqual(("EnQKCg", DID), (out["token"], out["did"]))
        (sec, _), (node_argv, kw) = run.calls
        self.assertEqual(["-a", "acct", "-s", "svc", "-w"], sec[2:])
        self.assertEqual(["token", "repo:post"], node_argv[2:])
        self.assertNotIn("SEED", " ".join(node_argv))
        self.assertEqual(("SEED", "1"), (kw["env"]["AOZORA_AGENT_SEED_B64URL"],
                                         kw["env"]["PRINT_TOKEN"]))

    def test_no_keychain_item_is_not_configured(self):
        with self.assertRaises(aozora_identity.NotConfigured):
            aozora_identity.biscuit_for_post({}, CFG, FakeRun(token_reply(), seed=None), NOW)

    def test_refuses_a_token_for_another_did_audience_or_scope(self):
        for over, code in (({"holder": "did:key:z6MkOther"}, "holder-not-configured-did"),
                           ({"audience": "did:web:elsewhere"}, "unexpected-audience"),
                           ({"permissions": ["repo:write"]}, "unexpected-permissions"),
                           ({"expiresAt": "2026-11-22T02:00:00Z"}, "already-expired")):
            with self.assertRaises(aozora_identity.IdentityError) as e:
                aozora_identity.biscuit_for_post({}, CFG, FakeRun(token_reply(**over)), NOW)
            self.assertEqual(code, e.exception.code)

    def test_failure_keeps_step_status_and_code_only(self):
        err = {"error": 'did-key/session: HTTP 401 {"error":"invalid_cacao","detail":"x"}',
               "step": "did-key/session", "status": 401}
        with self.assertRaises(aozora_identity.IdentityError) as e:
            aozora_identity.biscuit_for_post({}, CFG, FakeRun(Proc(1, "", json.dumps(err))), NOW)
        self.assertEqual(("did-key/session", 401, "invalid_cacao"),
                         (e.exception.step, e.exception.status, e.exception.code))
        self.assertNotIn("detail", str(e.exception))

    def test_tampered_script_is_refused(self):
        with mock.patch.dict(aozora_identity.VENDOR, sha256="0" * 64), \
                self.assertRaises(aozora_identity.IdentityError) as e:
            aozora_identity.biscuit_for_post({}, CFG, FakeRun(token_reply()), NOW)
        self.assertEqual("sha256-mismatch", e.exception.code)

    def test_vendored_script_matches_the_pin(self):
        aozora_identity.verify_script()

    def test_shipped_config_names_the_keychain_item_not_the_seed(self):
        cfg = aozora_identity.settings()
        self.assertEqual(("aozora-agent-seed", "iryo-taima-advocacy"),
                         (cfg["keychain_service"], cfg["keychain_account"]))
        self.assertTrue(cfg["did"].startswith("did:key:z6Mk"))
        with open(aozora_identity.CONFIG, encoding="utf-8") as f:
            self.assertNotIn("seed\":", f.read().replace("aozora-agent-seed", ""))


# ── local end-to-end: the real vendored script against a mock authn + PDS ──

def cbor_decode(b, i=0):
    """Definite-length text strings and maps only (what the script writes)."""
    major, info = b[i] >> 5, b[i] & 31
    i += 1
    if info < 24:
        n = info
    elif info == 24:
        n, i = b[i], i + 1
    elif info == 25:
        n, i = (b[i] << 8) | b[i + 1], i + 2
    else:
        raise ValueError("cbor")
    if major == 3:
        return b[i:i + n].decode("utf-8"), i + n
    if major == 5:
        out = {}
        for _ in range(n):
            k, i = cbor_decode(b, i)
            out[k], i = cbor_decode(b, i)
        return out, i
    raise ValueError("cbor major %d" % major)


B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def did_pubkey(did):
    n = 0
    for c in did.split(":")[-1][1:]:
        n = n * 58 + B58.index(c)
    raw = n.to_bytes(34, "big")
    assert raw[:2] == b"\xed\x01"
    return raw[2:]


VERIFY_JS = ("const c=require('node:crypto');const [x,m,s]=process.argv.slice(1);"
             "const k=c.createPublicKey({key:{kty:'OKP',crv:'Ed25519',x},format:'jwk'});"
             "process.exit(c.verify(null,Buffer.from(m,'base64'),k,Buffer.from(s,'base64'))?0:1)")


def siwe(p):
    return "\n".join([
        "%s wants you to sign in with your Ethereum account:" % p["domain"],
        p["iss"].split(":")[-1], "", "URI: %s" % p["aud"], "Version: 1", "Chain ID: 1",
        "Nonce: %s" % p["nonce"], "Issued At: %s" % p["iat"],
        "Expiration Time: %s" % p["exp"]])


class Mock:
    """auth.kotoba.cloud (nonce, session, biscuit/token) and pds createRecord."""

    def __init__(self):
        self.events, self.nonces, self.tokens = [], set(), set()
        mock_ = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, code, body, headers=()):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                for k, v in headers:
                    self.send_header(k, v)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["content-length"])) or b"{}")
                code, out, hdr = mock_.handle(self.path, self.headers, body)
                self.reply(code, out, hdr)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.origin = "http://127.0.0.1:%d" % self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def handle(self, path, h, body):
        self.events.append(path)
        if path.startswith("/v1/") and h.get("origin") != self.origin:
            return 403, {"error": "same-origin application/json request required"}, ()
        if path == "/v1/agents/did-key/nonce":
            nonce = "n%d" % len(self.nonces)
            self.nonces.add(nonce)
            host = self.origin.split("//")[1]
            return 200, {"nonce": nonce, "domain": host, "aud": self.origin}, ()
        if path == "/v1/agents/did-key/session":
            outer, _ = cbor_decode(base64.b64decode(body["cacao_b64"]))
            p = outer["p"]
            if p["nonce"] not in self.nonces:
                return 401, {"error": "agent_nonce_expired_or_used"}, ()
            self.nonces.discard(p["nonce"])
            x = base64.urlsafe_b64encode(did_pubkey(p["iss"])).decode().rstrip("=")
            ok = subprocess.run([NODE, "-e", VERIFY_JS, x,
                                 base64.b64encode(siwe(p).encode()).decode(), outer["s"]["s"]])
            if ok.returncode != 0:
                return 401, {"error": "invalid_cacao"}, ()
            self.did = p["iss"]
            return 200, {"did": p["iss"], "principalKind": "agent"}, (
                ("set-cookie", "gftd_session=sess-%s; HttpOnly" % p["nonce"]),)
        if path == "/v1/biscuit/token":
            if "gftd_session=sess-" not in (h.get("cookie") or "") or "tenantId" in body:
                return 400, {"error": "bad"}, ()
            if body["resource"] != "aozora://repo/" + self.did:
                return 403, {"error": "aozora_repo_not_own"}, ()
            tok = "biscuit%d" % len(self.tokens)
            self.tokens.add(tok)
            exp = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
            return 201, {"token": tok, "tokenType": "Biscuit", "holder": self.did,
                         "audience": "did:web:pds.aozora.app", "expiresAt": exp,
                         "expiresIn": 900, "permissions": body["permissions"]}, ()
        if path == "/xrpc/com.atproto.repo.createRecord":
            tok = (h.get("authorization") or "").removeprefix("Bearer ")
            if tok not in self.tokens:
                return 401, {"error": "biscuit_signature_invalid"}, ()
            self.tokens.discard(tok)  # one Biscuit per post in this mock
            rec = body["record"]
            if rec.get("labels", {}).get("values") != [{"val": "bot"}]:
                return 400, {"error": "agent_self_label_required"}, ()
            if "reply" in rec:
                return 403, {"error": "agent_reply_not_permitted"}, ()
            self.posted = body
            return 200, {"uri": "at://%s/app.bsky.feed.post/3k" % body["repo"], "cid": "bafyx"}, ()
        return 404, {"error": "not found"}, ()


@unittest.skipUnless(NODE, "node not installed")
class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.mock = Mock()
        self.dir = tempfile.mkdtemp()
        seed = subprocess.run([NODE, aozora_identity.SCRIPT, "keygen"], capture_output=True,
                              text=True, check=True).stdout.strip()
        self.env = {"PATH": os.environ.get("PATH", ""), "AOZORA_AGENT_SEED_B64URL": seed}
        self.did = aozora_identity.did_of(self.env, {})
        self.cfg = {"aozora": {"did": self.did, "authn_origin": self.mock.origin}}

    def tearDown(self):
        self.mock.server.shutdown()
        shutil.rmtree(self.dir)

    def test_approved_draft_goes_nonce_session_biscuit_post(self):
        src = "https://www.mhlw.go.jp/content/11120000/001302699.pdf"
        draft = {"text": "施行通知が公開されています。 " + src, "sources": [src]}
        old = {"at": (NOW - timedelta(weeks=5)).isoformat(), "hash": "0", "sources": []}
        identity = lambda env: aozora_identity.biscuit_for_post(env, self.cfg)
        state = {"posts": [old]}
        with mock.patch.object(aozora_post, "IDENTITY", identity):
            code, result = aozora_post.run(draft, state, NOW, True, self.env, Store(self.dir),
                                           pds=self.mock.origin, enabled=True)
        self.assertEqual((0, "posted"), (code, result["status"]), result)
        self.assertEqual(["/v1/agents/did-key/nonce", "/v1/agents/did-key/session",
                          "/v1/biscuit/token", "/xrpc/com.atproto.repo.createRecord"],
                         self.mock.events)
        self.assertEqual(self.did, self.mock.posted["repo"])
        self.assertEqual(draft["text"], self.mock.posted["record"]["text"])
        self.assertIn("facets", self.mock.posted["record"])
        # nothing secret reached the log
        with open(os.path.join(self.dir, "log.jsonl"), encoding="utf-8") as f:
            log = f.read()
        for secret in (self.env["AOZORA_AGENT_SEED_B64URL"], "biscuit0", "sess-"):
            self.assertNotIn(secret, log)

    def test_a_second_post_takes_a_new_biscuit(self):
        for _ in range(2):
            aozora_identity.biscuit_for_post(self.env, self.cfg)
        self.assertEqual(2, self.mock.events.count("/v1/agents/did-key/nonce"))
        self.assertEqual({"biscuit0", "biscuit1"}, self.mock.tokens)

    def test_wrong_did_in_config_is_refused(self):
        cfg = {"aozora": {"did": "did:key:z6MkOther", "authn_origin": self.mock.origin}}
        with self.assertRaises(aozora_identity.IdentityError) as e:
            aozora_identity.biscuit_for_post(self.env, cfg)
        self.assertEqual("holder-not-configured-did", e.exception.code)


if __name__ == "__main__":
    unittest.main()
