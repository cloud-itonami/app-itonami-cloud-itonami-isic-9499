"""Pre-post review for the iryo-taima-advocacy bot (AWAI-3658).

Implements the "投稿前の審査" section of Legal's memo for AWAI-3655 (policy A):

1. Hold patterns: a draft that matches one is held for a human, not sent.
2. Source required: post_guard.py already refuses a draft without an allowed
   primary-source link (it runs before this module).
3. Human approval: every post during the first 4 weeks of posting; after that,
   every post that mentions a patient's voice, a product or drug name, or use
   abroad.
4. A fixed disclaimer: in the profile, and required in a post about a drug,
   a product or use abroad.
5. Replies: the bot never answers an acquisition question or a personal
   consultation. `referral_reply` gives the one fixed text a human may use.

post_guard.check stays the hard floor (refusals the agent must not route
around). This module decides between "send now" and "hold for a human".
Pure functions only: no network, no clock, no file access.
"""
import re
import unicodedata
from datetime import timedelta

from post_guard import URL_RE

DISCLAIMER = "日本では大麻の所持・使用は違法です。医療の判断は医師に相談してください。"

REFERRAL_TEXT = ("このアカウントは制度と公開資料の情報だけを発信しています。"
                 "入手方法や個別の症状・治療についてはお答えできません。"
                 "医療のことは主治医に、制度のことは厚生労働省（https://www.mhlw.go.jp/）の"
                 "案内をご確認ください。" + DISCLAIMER)

INITIAL_APPROVAL_PERIOD = timedelta(weeks=4)

# (reason, pattern). A match holds the draft for a human; it is not a refusal.
HOLD = (
    ("acquisition", re.compile(r"入手|購入|売ります|譲ります|販売|個人輸入|輸入代行|仕入")),
    ("cultivation", re.compile(r"栽培方法|栽培の(仕方|方法)|育て方|種子")),
    ("use-method", re.compile(r"吸い方|吸引|摂取方法|使用方法")),
    ("dosage", re.compile(r"用量|服用量|摂取量|投与量")),
    ("contact", re.compile(
        r"連絡先|問い合わせ先|DM(を|で|ください|下さい)|LINE ?ID|"
        r"[\w.+-]+@[\w-]+\.[\w.-]+|"
        r"(?<!\d)(\+81[- ]?\d{1,4}|0\d{1,4})[- ]\d{1,4}[- ]\d{3,4}(?!\d)|(?<!\d)0\d{9,10}(?!\d)", re.I)),
    ("place", re.compile(r"店舗|販売店|ショップ|クリニック|診療所|薬局|住所|所在地")),
    ("short-url", re.compile(
        r"\b(bit\.ly|t\.co|tinyurl\.com|goo\.gl|ow\.ly|is\.gd|buff\.ly|x\.gd|amzn\.to|"
        r"rebrand\.ly|cutt\.ly|lnkd\.in|t\.ly|shorturl\.at|s\.id)/", re.I)),
)

PRODUCT_OR_DRUG = re.compile(
    r"エピディオレックス|Epidiolex|サティベックス|Sativex|ナビキシモルス|nabiximols|"
    r"ドロナビノール|dronabinol|マリノール|Marinol|シンドロス|Syndros|ナビロン|nabilone|Cesamet|"
    r"カンナビジオール|cannabidiol|\bCBD\b|\bTHC\b|テトラヒドロカンナビノール|"
    r"製品名|商品名", re.I)
# Names only. The generic class "大麻由来医薬品" is the bot's in-scope subject
# (制度・研究・政策提言), so it is deliberately not a drug name here.
EFFICACY = re.compile(r"効く|効い|効き|効果|効能|治る|治り|治っ|改善|軽減|緩和")
PATIENT_VOICE = re.compile(
    r"患者(さん)?の声|当事者の声|体験談|体験|私は|私の|家族の声|娘|息子|闘病|証言|インタビュー")
ABROAD = re.compile(
    r"海外|外国|米国|アメリカ|カナダ|英国|イギリス|ドイツ|フランス|オランダ|イスラエル|タイ|"
    r"オーストラリア|ニュージーランド|欧州|EU|州法|諸外国|各国", re.I)
SENTENCE_END = re.compile(r"[。！？!?\n]+")

ACQUISITION_QUESTION = re.compile(r"入手|購入|買|売|手に入|譲|個人輸入|どこで|栽培|育て")
CONSULTATION = re.compile(r"症状|病気|治療|処方|用量|飲ん|使って(も|いい)|効きますか|相談|主治医|副作用")


def _normal(text):
    return unicodedata.normalize("NFKC", text)


def holds(text):
    """Hold reasons for `text` (a list, empty when nothing matched). Links are
    removed before matching, except for the short-URL check: a source URL's
    path must not read as a phone number or a word."""
    t = _normal(text)
    bare = URL_RE.sub(" ", t)
    reasons = [r for r, p in HOLD if p.search(t if r == "short-url" else bare)]
    for sentence in SENTENCE_END.split(bare):
        if PRODUCT_OR_DRUG.search(sentence) and EFFICACY.search(sentence):
            reasons.append("product-with-efficacy")
            break
    return reasons


def sensitive_topics(text):
    """Topics that always need a human approval, at any age of the account."""
    t = _normal(text)
    topics = []
    if PATIENT_VOICE.search(t):
        topics.append("patient-voice")
    if PRODUCT_OR_DRUG.search(t):
        topics.append("product-or-drug")
    if ABROAD.search(t):
        topics.append("abroad")
    return topics


def needs_disclaimer(text):
    t = _normal(text)
    return bool(PRODUCT_OR_DRUG.search(t) or ABROAD.search(t))


def has_disclaimer(text):
    squash = lambda s: re.sub(r"\s+", "", _normal(s))
    return squash(DISCLAIMER) in squash(text)


def in_initial_period(first_post_at, now):
    """True until 4 weeks after the first post. No post yet counts as inside."""
    return first_post_at is None or now - first_post_at < INITIAL_APPROVAL_PERIOD


def assess(draft, now, first_post_at):
    """Decide what happens to a draft that post_guard.check already passed.

    Returns {"decision": "fix" | "hold" | "send", "reasons": [...]}:
      fix   the agent must change the draft (a missing disclaimer); nothing is queued
      hold  queue the draft for a human approver; reasons say why
      send  no human approval is needed
    """
    text = draft["text"]
    if needs_disclaimer(text) and not has_disclaimer(text):
        return {"decision": "fix", "reasons": ["disclaimer-required"]}
    reasons = ["hold:" + r for r in holds(text)]
    reasons += ["topic:" + t for t in sensitive_topics(text)]
    if in_initial_period(first_post_at, now):
        reasons.append("initial-period")
    return {"decision": "hold" if reasons else "send", "reasons": reasons}


def referral_reply(text):
    """The fixed text for an incoming acquisition question or personal
    consultation, else None. The bot itself never replies (reply_aozora is
    blocked in yakuwari.edn); a human may use this text, or not reply."""
    t = _normal(text)
    if ACQUISITION_QUESTION.search(t) or CONSULTATION.search(t):
        return REFERRAL_TEXT
    return None
