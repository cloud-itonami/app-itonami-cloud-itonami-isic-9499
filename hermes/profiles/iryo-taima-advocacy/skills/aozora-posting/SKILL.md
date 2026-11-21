---
name: aozora-posting
description: Use when posting a sourced summary on aozora.app (pds.aozora.app) as the iryo-taima-advocacy bot.
---

# Aozora への投稿

経路は 1 つだけ: `scripts/aozora_post.py`。`curl` や他の手段で `createRecord` を直接呼ばない。

## 手順

1. 一次資料のページを読む。読めなければここでやめる。
2. 草稿を `workspace/drafts/<日時>.json` に書く。

   ```json
   {"text": "本文。末尾に出典の URL を 1 つ。", "sources": ["https://..."]}
   ```

3. 実行する。

   ```bash
   python3 scripts/aozora_post.py --draft workspace/drafts/<日時>.json --state workspace/post-state.json --execute
   ```

4. 出力の JSON をそのまま報告に入れる。

## 草稿の書き方

- 300 文字以内。URL も文字数に数える。
- 何が起きたかを先に書く: 誰が、何を、いつ。意見募集なら締切。
- 出典の URL は本文に 1 つ、`sources` にも同じものを入れる。許可された出典は `scripts/post_guard.py` の `ALLOWED_SOURCE_HOSTS`。
- 評価の言葉を足さない。資料に書いていないことを書かない。
- ハッシュタグは付けない。

## 結果の読み方

| `status` | 意味 | 次にすること |
|---|---|---|
| `posted` | 送信され、`uri` と `cid` が返った | その JSON を報告する。投稿の証拠はこれだけ |
| `dry-run` | 送っていない（資格情報が未設定、または `--execute` なし） | 「未投稿」と報告する |
| `refused` | ガードが拒否した。`reasons` に理由 | 下の表を見る |
| `send-failed` | 送信に失敗した。記録もされていない | 「未投稿」と報告する。同じ実行で再試行しない |

## 拒否されたとき

| 理由 | 直してよいか |
|---|---|
| `no-source` `source-url-not-in-text` | よい。出典を付ける |
| `too-long:N` | よい。短くする |
| `source-not-allowed:…` `link-not-allowed:…` | 一次資料の URL に替えられるときだけ。替えられなければやめる |
| `banned:…` | **直さない**。言い換えて通すのは規則の回避。その項目は投稿しない |
| `daily-limit` `too-soon` `duplicate-text` `source-posted-recently` | 直さない。今回は投稿しない |

## 確認していないこと

`pds.aozora.app` に対する送信は、この skill を書いた時点で実行していない。
`createSession` は AT Protocol の標準形（identifier と password）で呼ぶ。
PDS がこのアカウントに別の形だけを受け付ける場合、`send-failed` になる。そのときは owner に報告する。
