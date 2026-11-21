"""File store for the review pipeline: the approval queue and the review log.

Layout under the review directory (default `workspace/review`):

  queue/<id>.json   one held draft: draft, reasons, status, approver, timestamps
  log.jsonl         append-only: one line per review decision, approval,
                    rejection and send. Never rewritten.

Status of a queue item: pending -> approved -> posted, or pending -> rejected.
An approval records who approved and when. Who may approve is configuration
(`review_config.json` "approvers", or ITONAMI_REVIEW_APPROVERS, comma-separated);
with no approver configured nothing can be approved.
"""
import hashlib
import json
import os

STATUSES = ("pending", "approved", "rejected", "posted")


class ReviewError(Exception):
    pass


def approvers(config, env):
    raw = env.get("ITONAMI_REVIEW_APPROVERS")
    if raw is not None:
        return [a.strip() for a in raw.split(",") if a.strip()]
    return [a for a in config.get("approvers", []) if isinstance(a, str) and a.strip()]


def load_config(path):
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class Store:
    def __init__(self, root):
        self.root = root
        self.queue = os.path.join(root, "queue")

    def _ensure(self):
        os.makedirs(self.queue, exist_ok=True)

    def _write(self, path, obj):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def log(self, event):
        self._ensure()
        with open(os.path.join(self.root, "log.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def read_log(self):
        path = os.path.join(self.root, "log.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def _path(self, item_id):
        if not item_id or any(c not in "0123456789abcdef" for c in item_id):
            raise ReviewError("invalid id: %r" % item_id)
        return os.path.join(self.queue, item_id + ".json")

    def get(self, item_id):
        path = self._path(item_id)
        if not os.path.exists(path):
            raise ReviewError("no queued draft %s" % item_id)
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def items(self, status=None):
        if not os.path.isdir(self.queue):
            return []
        out = []
        for name in sorted(os.listdir(self.queue)):
            if name.endswith(".json"):
                with open(os.path.join(self.queue, name), encoding="utf-8") as f:
                    item = json.load(f)
                if status is None or item["status"] == status:
                    out.append(item)
        return sorted(out, key=lambda i: i["queued_at"])

    def enqueue(self, draft, reasons, now):
        self._ensure()
        seed = json.dumps([draft, now.isoformat()], ensure_ascii=False, sort_keys=True)
        item_id = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
        item = {"id": item_id, "status": "pending", "draft": draft, "reasons": reasons,
                "queued_at": now.isoformat(), "approver": None, "decided_at": None,
                "decision_note": None, "posted": None}
        self._write(self._path(item_id), item)
        self.log({"event": "queued", "id": item_id, "at": now.isoformat(),
                  "reasons": reasons, "text": draft["text"], "sources": draft.get("sources", [])})
        return item

    def decide(self, item_id, approve, approver, allowed, now, note=None):
        if not allowed:
            raise ReviewError("no approver is configured (review_config.json approvers "
                              "or ITONAMI_REVIEW_APPROVERS)")
        if approver not in allowed:
            raise ReviewError("%r is not a configured approver" % approver)
        item = self.get(item_id)
        if item["status"] != "pending":
            raise ReviewError("draft %s is %s, not pending" % (item_id, item["status"]))
        item.update(status="approved" if approve else "rejected", approver=approver,
                    decided_at=now.isoformat(), decision_note=note)
        self._write(self._path(item_id), item)
        self.log({"event": item["status"], "id": item_id, "at": now.isoformat(),
                  "approver": approver, "note": note})
        return item

    def mark_posted(self, item_id, result, now):
        item = self.get(item_id)
        item.update(status="posted", posted={"at": now.isoformat(), "uri": result.get("uri"),
                                             "cid": result.get("cid")})
        self._write(self._path(item_id), item)
        self.log({"event": "posted", "id": item_id, "at": now.isoformat(),
                  "uri": result.get("uri"), "cid": result.get("cid")})
        return item
