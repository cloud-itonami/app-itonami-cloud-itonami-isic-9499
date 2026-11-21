#!/usr/bin/env node
// Bot identity on Aozora: prove a did:key at auth.kotoba.cloud, exchange the
// agent session for a 15-minute Biscuit, and use it at pds.aozora.app.
// Documented in docs/agent-identity.md (AWAI-3809). Zero dependencies:
// Node >= 20 (WebCrypto Ed25519 through node:crypto, global fetch).
//
// The bot's Ed25519 seed (32 bytes, base64url) is read ONLY from the
// environment variable AOZORA_AGENT_SEED_B64URL, never from argv or a file in
// a repository. Keep it in the host Keychain / Keystore, one key per bot:
//
//   security add-generic-password -a <bot> -s aozora-agent-seed -w "$(node aozora-agent.mjs keygen)"
//   export AOZORA_AGENT_SEED_B64URL="$(security find-generic-password -a <bot> -s aozora-agent-seed -w)"
//
// Commands (all print JSON on stdout; secrets are never printed except by
// `keygen`, whose only output is the new seed):
//   keygen                              new random seed (pipe it into the Keychain)
//   did                                 the did:key of the seed
//   cacao <nonce> [domain] [aud]        a CACAO over a nonce from /v1/agents/did-key/nonce
//                                       (for the curl walk-through in the docs)
//   token <perm>[,<perm>...]            Biscuit for aozora://repo/<did>
//                                       (perms: account:bind, repo:post, repo:write)
//   create-account <handle>             createAccount (account:bind)
//   put-profile <displayName> <description>
//                                       putRecord app.bsky.actor.profile/self with
//                                       the `bot` self-label (repo:write)
//   post <text>                         createRecord app.bsky.feed.post with the
//                                       `bot` self-label (repo:post)
//   delete <at-uri>                     deleteRecord (repo:write)
//
// Environment: AUTHN_ORIGIN (default https://auth.kotoba.cloud),
// PDS_ORIGIN (default https://pds.aozora.app).

import crypto from "node:crypto";

const AUTHN = process.env.AUTHN_ORIGIN || "https://auth.kotoba.cloud";
const PDS = process.env.PDS_ORIGIN || "https://pds.aozora.app";

// ── encodings ──────────────────────────────────────────────────────────────

const B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";

export function base58btc(bytes) {
  let n = 0n;
  for (const b of bytes) n = n * 256n + BigInt(b);
  let out = "";
  while (n > 0n) { out = B58[Number(n % 58n)] + out; n /= 58n; }
  for (const b of bytes) { if (b === 0) out = "1" + out; else break; }
  return out;
}

const b64url = (buf) => Buffer.from(buf).toString("base64url");
const fromB64url = (s) => Buffer.from(s, "base64url");

// Definite-length CBOR, text strings and maps only: the restricted profile
// cacao.edge.cbor decodes (kotoba-lang/kotoba lib/org.chainagnostic.cacao).
function cborHead(major, n) {
  if (n < 24) return [(major << 5) | n];
  if (n <= 0xff) return [(major << 5) | 24, n];
  if (n <= 0xffff) return [(major << 5) | 25, n >> 8, n & 0xff];
  throw new Error("cbor: value too large");
}
function cborText(s) {
  const b = [...Buffer.from(s, "utf8")];
  return [...cborHead(3, b.length), ...b];
}
function cborMap(pairs) {
  return [...cborHead(5, pairs.length), ...pairs.flatMap(([k, v]) => [...cborText(k), ...cborText(v)])];
}

// ── key ────────────────────────────────────────────────────────────────────

const PKCS8_ED25519_PREFIX = Buffer.from("302e020100300506032b657004220420", "hex");

export function keyFromSeed(seedB64url) {
  const seed = fromB64url(seedB64url);
  if (seed.length !== 32) throw new Error("AOZORA_AGENT_SEED_B64URL must be 32 bytes base64url");
  const privateKey = crypto.createPrivateKey({
    key: Buffer.concat([PKCS8_ED25519_PREFIX, seed]), format: "der", type: "pkcs8" });
  const pub = fromB64url(crypto.createPublicKey(privateKey).export({ format: "jwk" }).x);
  const did = "did:key:z" + base58btc(Buffer.concat([Buffer.from([0xed, 0x01]), pub]));
  return { privateKey, did };
}

// ── CACAO (CAIP-122 / SIWE plaintext, Ed25519) ─────────────────────────────

export const isoSec = (ms) => new Date(Math.floor(ms / 1000) * 1000).toISOString().replace(".000Z", "Z");

// Byte-for-byte the plaintext cacao.edge.verify/siwe-message reconstructs.
export function siweMessage({ iss, domain, aud, nonce, iat, exp }) {
  const lines = [`${domain} wants you to sign in with your Ethereum account:`,
    iss.split(":").pop(), "",
    `URI: ${aud}`, "Version: 1", "Chain ID: 1", `Nonce: ${nonce}`, `Issued At: ${iat}`];
  if (exp) lines.push(`Expiration Time: ${exp}`);
  return lines.join("\n");
}

export function mintCacao(key, { domain, aud, nonce, nowMs = Date.now(), ttlSec = 120 }) {
  const iat = isoSec(nowMs);
  const exp = isoSec(nowMs + ttlSec * 1000);
  const msg = siweMessage({ iss: key.did, domain, aud, nonce, iat, exp });
  const sig = crypto.sign(null, Buffer.from(msg, "utf8"), key.privateKey).toString("base64");
  // Field order is the order cacao.edge.mint writes; verify reads by name.
  const p = [["iss", key.did], ["aud", aud], ["iat", iat], ["nonce", nonce],
             ["domain", domain], ["version", "1"], ["exp", exp]];
  const outer = [...cborHead(5, 2), ...cborText("p"), ...cborMap(p),
                 ...cborText("s"), ...cborMap([["s", sig]])];
  return Buffer.from(outer).toString("base64");
}

// ── HTTP ───────────────────────────────────────────────────────────────────

async function call(url, { method = "POST", headers = {}, body } = {}) {
  const r = await fetch(url, {
    method, redirect: "manual",
    headers: { accept: "application/json", ...(body ? { "content-type": "application/json" } : {}), ...headers },
    body: body ? JSON.stringify(body) : undefined });
  const text = await r.text();
  let data; try { data = JSON.parse(text); } catch { data = { raw: text.slice(0, 500) }; }
  return { status: r.status, data, headers: r.headers };
}

function fail(step, r) {
  const e = new Error(`${step}: HTTP ${r.status} ${JSON.stringify(r.data)}`);
  e.step = step; e.status = r.status; e.data = r.data;
  throw e;
}

// 1+2. did:key proof → kind-agent session (created on the first exchange).
export async function agentSession(key) {
  const origin = { origin: AUTHN };
  const n = await call(`${AUTHN}/v1/agents/did-key/nonce`, { headers: origin, body: {} });
  if (n.status !== 200 || !n.data.nonce) fail("did-key/nonce", n);
  const cacao = mintCacao(key, { domain: n.data.domain, aud: n.data.aud, nonce: n.data.nonce });
  const s = await call(`${AUTHN}/v1/agents/did-key/session`, { headers: origin, body: { cacao_b64: cacao } });
  if (s.status !== 200) fail("did-key/session", s);
  const cookie = (s.headers.get("set-cookie") || "").match(/gftd_session=([^;]+)/)?.[1];
  if (!cookie) fail("did-key/session (no gftd_session cookie)", s);
  return { cookie, did: s.data.did, principalKind: s.data.principalKind, sessionExpiresAt: s.data.sessionExpiresAt };
}

// 3. agent session → Biscuit for the bot's own repo. No tenantId: the aozora
// branch of /v1/biscuit/token is selected by `resource` and refuses tenantId.
export async function biscuit(key, permissions, session) {
  const sess = session || await agentSession(key);
  const r = await call(`${AUTHN}/v1/biscuit/token`, {
    headers: { origin: AUTHN, cookie: `gftd_session=${sess.cookie}` },
    body: { resource: `aozora://repo/${key.did}`, permissions } });
  if (r.status !== 201) fail("biscuit/token", r);
  return r.data; // {token, authorization, expiresAt, expiresIn, holder, resource, audience, permissions}
}

const BOT_LABELS = { $type: "com.atproto.label.defs#selfLabels", values: [{ val: "bot" }] };

async function xrpc(nsid, token, body) {
  return call(`${PDS}/xrpc/${nsid}`, { headers: { authorization: `Bearer ${token}` }, body });
}

// ── CLI ────────────────────────────────────────────────────────────────────

async function main([cmd, ...args]) {
  if (cmd === "keygen") { process.stdout.write(b64url(crypto.randomBytes(32)) + "\n"); return; }
  const seed = process.env.AOZORA_AGENT_SEED_B64URL;
  if (!seed) throw new Error("AOZORA_AGENT_SEED_B64URL is not set (read it from the Keychain)");
  const key = keyFromSeed(seed.trim());
  const out = (v) => process.stdout.write(JSON.stringify(v, null, 2) + "\n");
  switch (cmd) {
    case "did": return out({ did: key.did });
    case "cacao": {
      if (!args[0]) throw new Error("cacao needs the nonce from /v1/agents/did-key/nonce");
      const host = new URL(AUTHN).host;
      process.stdout.write(mintCacao(key, { nonce: args[0], domain: args[1] || host, aud: args[2] || AUTHN }) + "\n");
      return;
    }
    case "token": {
      const t = await biscuit(key, (args[0] || "repo:post").split(","));
      // The token is a bearer secret: print its metadata, and the token only
      // when explicitly asked (for piping into curl).
      return out(process.env.PRINT_TOKEN === "1" ? t : { ...t, token: "<redacted>", authorization: "<redacted>" });
    }
    case "create-account": {
      const t = await biscuit(key, ["account:bind"]);
      const r = await xrpc("com.atproto.server.createAccount", t.token, { handle: args[0] });
      return out({ status: r.status, ...r.data });
    }
    case "put-profile": {
      const t = await biscuit(key, ["repo:write"]);
      const r = await xrpc("com.atproto.repo.putRecord", t.token, {
        repo: key.did, collection: "app.bsky.actor.profile", rkey: "self",
        record: { $type: "app.bsky.actor.profile", displayName: args[0], description: args[1], labels: BOT_LABELS } });
      return out({ status: r.status, ...r.data });
    }
    case "post": {
      const t = await biscuit(key, ["repo:post"]);
      const r = await xrpc("com.atproto.repo.createRecord", t.token, {
        repo: key.did, collection: "app.bsky.feed.post",
        record: { $type: "app.bsky.feed.post", text: args[0], createdAt: new Date().toISOString(),
                  langs: ["ja"], labels: BOT_LABELS } });
      return out({ status: r.status, ...r.data });
    }
    case "delete": {
      const m = /^at:\/\/([^/]+)\/([^/]+)\/([^/]+)$/.exec(args[0] || "");
      if (!m || m[1] !== key.did) throw new Error("delete needs at://<this did>/<collection>/<rkey>");
      const t = await biscuit(key, ["repo:write"]);
      const r = await xrpc("com.atproto.repo.deleteRecord", t.token, { repo: m[1], collection: m[2], rkey: m[3] });
      return out({ status: r.status, ...r.data });
    }
    default:
      throw new Error("usage: aozora-agent.mjs keygen|did|cacao|token|create-account|put-profile|post|delete");
  }
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main(process.argv.slice(2)).catch((e) => {
    process.stderr.write(JSON.stringify({ error: e.message, step: e.step, status: e.status }) + "\n");
    process.exit(1);
  });
}
