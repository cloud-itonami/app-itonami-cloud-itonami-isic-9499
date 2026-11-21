"""The bot's Aozora identity: its own did:key, proven at auth.kotoba.cloud, and
a fresh 15-minute Biscuit for every post (AWAI-3658, AWAI-3809).

Steps (network-awai/app-kotoba-cloud docs/agent-identity.md §1, §8):
  1. POST auth.kotoba.cloud/v1/agents/did-key/nonce
  2. a CACAO over the nonce, signed with the bot's Ed25519 seed
  3. POST /v1/agents/did-key/session -> kind-agent session cookie
  4. POST /v1/biscuit/token {"resource": "aozora://repo/<did>",
     "permissions": ["repo:post"]} -> Biscuit (15 min; no tenantId)
Steps 1-4 run in scripts/aozora_agent.mjs, vendored byte-for-byte from
app-kotoba-cloud (VENDOR below; the digest is checked before every use).
The Biscuit then goes to pds.aozora.app createRecord (aozora_post.py).

Secrets: the seed lives ONLY in the macOS Keychain (service
"aozora-agent-seed", account "iryo-taima-advocacy"; names in
review_config.json "aozora"). It is read with `security`, passed to node in the
child's environment only (never argv), and never printed or logged. The
Biscuit and the session cookie stay in memory for one post and are dropped;
nothing is saved. Errors carry the step, the HTTP status and the error code
only.

  python3 scripts/aozora_identity.py check   # offline: digest, Keychain, did:key

`check` makes no network request.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from review_store import load_config  # noqa: E402

SCRIPT = os.path.join(HERE, "aozora_agent.mjs")
VENDOR = {
    "repo": "network-awai/app-kotoba-cloud",
    "path": "scripts/agent-identity/aozora-agent.mjs",
    "commit": "a00cbd3cb3e28a0d651e8bf9762c9f9964746699",
    "sha256": "7857142ab0d0d059b0234af85954e4e141a45f99f8fa16f5ae27689bc0408ff5",
}
CONFIG = os.path.join(HERE, "review_config.json")
DEFAULTS = {
    "keychain_service": "aozora-agent-seed",
    "keychain_account": "iryo-taima-advocacy",
    "authn_origin": "https://auth.kotoba.cloud",
    "audience": "did:web:pds.aozora.app",
}
SEED_ENV = "AOZORA_AGENT_SEED_B64URL"
NODE_CANDIDATES = ("/opt/homebrew/bin/node", "/usr/local/bin/node")
CODE_RE = re.compile(r'"error"\s*:\s*"([A-Za-z0-9_.:-]{1,80})"')


class NotConfigured(Exception):
    """No seed: the send is a dry run, not a failure."""


class IdentityError(Exception):
    """Getting a Biscuit failed. str() is safe to log (step, status, code)."""

    def __init__(self, step, status=None, code=None):
        self.step, self.status, self.code = step, status, code
        super().__init__("%s%s%s" % (step, " HTTP %s" % status if status else "",
                                     " %s" % code if code else ""))


def settings(config=None):
    config = load_config(CONFIG) if config is None else config
    out = dict(DEFAULTS)
    out.update({k: v for k, v in (config.get("aozora") or {}).items() if not k.startswith("_")})
    return out


def verify_script(path=SCRIPT):
    with open(path, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    if digest != VENDOR["sha256"]:
        raise IdentityError("vendored-script-digest", code="sha256-mismatch")


def node_path(env):
    for p in (env.get("ITONAMI_NODE"), shutil.which("node", path=env.get("PATH")),
              *NODE_CANDIDATES):
        if p and os.access(p, os.X_OK):
            return p
    raise IdentityError("node-not-found")


def read_seed(env, cfg, run=subprocess.run):
    if env.get(SEED_ENV):
        return env[SEED_ENV].strip()
    try:
        r = run(["/usr/bin/security", "find-generic-password", "-a", cfg["keychain_account"],
                 "-s", cfg["keychain_service"], "-w"],
                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise NotConfigured("keychain unavailable")
    seed = (r.stdout or "").strip()
    if r.returncode != 0 or not seed:
        raise NotConfigured("no Keychain item %s/%s" % (cfg["keychain_service"],
                                                         cfg["keychain_account"]))
    return seed


def _child_env(env, seed, cfg, extra=None):
    child = {"PATH": env.get("PATH", "/usr/bin:/bin"), SEED_ENV: seed,
             "AUTHN_ORIGIN": cfg["authn_origin"]}
    if env.get("HOME"):
        child["HOME"] = env["HOME"]
    child.update(extra or {})
    return child


def _node(args, env, seed, cfg, run, extra=None, timeout=90):
    verify_script()
    try:
        r = run([node_path(env), SCRIPT, *args], capture_output=True, text=True,
                timeout=timeout, env=_child_env(env, seed, cfg, extra))
    except subprocess.TimeoutExpired:
        raise IdentityError(args[0], code="timeout")
    if r.returncode != 0:
        step, status, code = args[0], None, None
        try:
            err = json.loads((r.stderr or "").strip().splitlines()[-1])
            step = err.get("step") or step
            status = err.get("status")
            m = CODE_RE.search(err.get("error") or "")
            code = m.group(1) if m else None
        except (ValueError, IndexError, AttributeError):
            pass
        raise IdentityError(step, status, code)
    try:
        return json.loads(r.stdout)
    except ValueError:
        raise IdentityError(args[0], code="unparsable-output")


def did_of(env=None, config=None, run=subprocess.run):
    env = os.environ if env is None else env
    cfg = settings(config)
    return _node(["did"], env, read_seed(env, cfg, run), cfg, run)["did"]


def biscuit_for_post(env=None, config=None, run=subprocess.run, now=None):
    """A fresh Biscuit with repo:post only, for one post. Returns
    {"token", "did", "expiresAt"}. Raises NotConfigured or IdentityError."""
    env = os.environ if env is None else env
    cfg = settings(config)
    seed = read_seed(env, cfg, run)
    t = _node(["token", "repo:post"], env, seed, cfg, run, {"PRINT_TOKEN": "1"})
    del seed
    did = cfg.get("did")
    token = t.get("token")
    if not token or t.get("tokenType", "Biscuit") != "Biscuit":
        raise IdentityError("biscuit/token", code="no-token")
    if did and t.get("holder") != did:
        raise IdentityError("biscuit/token", code="holder-not-configured-did")
    if t.get("audience") != cfg["audience"]:
        raise IdentityError("biscuit/token", code="unexpected-audience")
    if t.get("permissions") and set(t["permissions"]) != {"repo:post"}:
        raise IdentityError("biscuit/token", code="unexpected-permissions")
    try:
        if datetime.fromisoformat(t["expiresAt"].replace("Z", "+00:00")) <= (
                now or datetime.now(timezone.utc)):
            raise IdentityError("biscuit/token", code="already-expired")
    except (KeyError, AttributeError, ValueError):
        raise IdentityError("biscuit/token", code="no-expiry")
    return {"token": token, "did": t["holder"], "expiresAt": t["expiresAt"]}


def check(env=None, config=None, run=subprocess.run):
    """Offline readiness check: no network request. Never prints the seed."""
    env = os.environ if env is None else env
    cfg = settings(config)
    out = {"script": VENDOR, "keychain": {"service": cfg["keychain_service"],
                                          "account": cfg["keychain_account"]},
           "configured_did": cfg.get("did")}
    try:
        verify_script()
        out["script_digest"] = "ok"
        out["did"] = did_of(env, config, run)
        out["did_matches_config"] = out["did"] == cfg.get("did")
        out["ok"] = out["did_matches_config"]
    except (NotConfigured, IdentityError) as e:
        out["ok"], out["error"] = False, "%s: %s" % (type(e).__name__, e)
    return out


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv != ["check"]:
        print("usage: aozora_identity.py check", file=sys.stderr)
        return 2
    result = check()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
