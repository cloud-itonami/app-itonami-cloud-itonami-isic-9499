"""Deterministic gate for every Aozora post of the iryo-taima-advocacy bot.

The agent drafts; this module decides whether the draft may be sent. It is a
floor, not a proof of safety: the patterns catch the known refusal classes
(acquisition guidance, dosing advice, cure claims, product promotion,
recreational advocacy, encouraging use) and nothing else. SOUL.md holds the
rules the agent itself must follow.

Pure functions only: no network, no clock, no file access. The caller passes
`now` and the post history.
"""
import hashlib
import re
import unicodedata
from datetime import timedelta
from urllib.parse import urlsplit

MAX_GRAPHEMES = 300
MAX_POSTS_PER_DAY = 3
MIN_INTERVAL = timedelta(hours=2)
SOURCE_REPEAT_WINDOW = timedelta(days=7)

# Public primary sources: Japanese government, legislature, regulators,
# international bodies and peer-reviewed indexes. A host matches when it is
# the entry or a subdomain of it.
ALLOWED_SOURCE_HOSTS = (
    "mhlw.go.jp",
    "e-gov.go.jp",
    "pmda.go.jp",
    "shugiin.go.jp",
    "sangiin.go.jp",
    "ndl.go.jp",
    "kantei.go.jp",
    "who.int",
    "incb.org",
    "unodc.org",
    "ema.europa.eu",
    "fda.gov",
    "pubmed.ncbi.nlm.nih.gov",
    "cochranelibrary.com",
    # DOI resolver (a paper's DOI link) and foreign government health
    # regulators, for overseas systems and research (Legal memo, AWAI-3655).
    "doi.org",
    "nih.gov",
    "cdc.gov",
    "gov.uk",
    "canada.ca",
    "europa.eu",
    "tga.gov.au",
    "health.gov.au",
    "bfarm.de",
    "ansm.sante.fr",
    "health.gov.il",
)

# (reason, pattern). Japanese and English forms of each refusal class.
BANNED = (
    ("acquisition-guidance", re.compile(
        r"入手(方法|先|ルート|でき)|購入(方法|先|でき)|買(え|い方|う方法)|通販|個人輸入|密輸|売人|"
        r"栽培(方法|の仕方|キット)|育て方|種子の(入手|購入)|"
        r"where to (buy|get)|how to (buy|grow|obtain|import)", re.I)),
    ("dosing-advice", re.compile(
        r"\d+(\.\d+)?\s*(mg|ｍｇ|ミリグラム|滴|ml|ｍｌ)|用量|服用量|摂取量|飲み方|使い方|"
        r"\bdos(e|age|ing)\b", re.I)),
    ("cure-claim", re.compile(
        r"治(る|り|せ|す)|完治|特効|効果抜群|必ず効|がんが消|副作用(は|が)?(ない|なし|ゼロ)|"
        r"\bcures?\b|miracle", re.I)),
    ("product-promotion", re.compile(
        r"おすすめ|オススメ|お勧め|クーポン|割引|セール|アフィリエイト|"
        r"(CBD|ＣＢＤ)(オイル|グミ|リキッド|ベイプ)|ブランド|"
        r"discount|coupon|promo code", re.I)),
    ("recreational-advocacy", re.compile(
        r"嗜好(用|目的)?.{0,12}(解禁|合法化|自由化)(を|すべき|しよう|に賛成)|"
        r"legali[sz]e (recreational|weed|marijuana)", re.I)),
    ("encouraging-use", re.compile(
        r"吸(おう|ってみ|えば)|試して(みて|みよう|ください)|使ってみ(て|よう)|キマ|ハイにな|"
        r"\b(try it|get high)\b", re.I)),
)

URL_RE = re.compile(r"https?://[^\s<>\"'）)」』】]+")


def graphemes(text):
    """Approximate grapheme count: one per code point, except combining marks,
    variation selectors and zero-width joiners (and the code point a joiner
    attaches). Close enough for the 300 budget; it never undercounts plain
    Japanese text."""
    n = 0
    joined = False
    for ch in unicodedata.normalize("NFC", text):
        cp = ord(ch)
        if ch == "‍":
            joined = True
            continue
        if unicodedata.combining(ch) or 0xFE00 <= cp <= 0xFE0F or 0xE0100 <= cp <= 0xE01EF:
            continue
        if joined:
            joined = False
            continue
        n += 1
    return n


def host_allowed(url):
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        return False
    host = parts.hostname.lower()
    return any(host == h or host.endswith("." + h) for h in ALLOWED_SOURCE_HOSTS)


def text_hash(text):
    normal = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip().lower()
    return hashlib.sha256(normal.encode("utf-8")).hexdigest()


def check(draft, history, now):
    """Return {"ok": bool, "reasons": [str]}.

    draft:   {"text": str, "sources": [url, ...]}
    history: [{"at": aware datetime, "hash": str, "sources": [url, ...]}, ...]
    now:     aware datetime
    """
    reasons = []
    text = draft.get("text") if isinstance(draft, dict) else None
    sources = draft.get("sources") if isinstance(draft, dict) else None
    if not isinstance(text, str) or not text.strip():
        return {"ok": False, "reasons": ["empty-text"]}
    if not isinstance(sources, list) or not all(isinstance(s, str) for s in sources):
        sources = []

    count = graphemes(text)
    if count > MAX_GRAPHEMES:
        reasons.append("too-long:%d" % count)

    if not sources:
        reasons.append("no-source")
    for s in sources:
        if not host_allowed(s):
            reasons.append("source-not-allowed:%s" % s)
    in_text = URL_RE.findall(text)
    if sources and not any(s in in_text for s in sources):
        reasons.append("source-url-not-in-text")
    for u in in_text:
        if not host_allowed(u):
            reasons.append("link-not-allowed:%s" % u)

    for reason, pattern in BANNED:
        if pattern.search(unicodedata.normalize("NFKC", text)):
            reasons.append("banned:%s" % reason)

    recent = [h for h in history if now - h["at"] < timedelta(days=1)]
    if len(recent) >= MAX_POSTS_PER_DAY:
        reasons.append("daily-limit")
    if any(now - h["at"] < MIN_INTERVAL for h in history):
        reasons.append("too-soon")
    digest = text_hash(text)
    if any(h.get("hash") == digest for h in history):
        reasons.append("duplicate-text")
    for h in history:
        if now - h["at"] < SOURCE_REPEAT_WINDOW and set(h.get("sources", [])) & set(sources):
            reasons.append("source-posted-recently")
            break

    return {"ok": not reasons, "reasons": reasons}
