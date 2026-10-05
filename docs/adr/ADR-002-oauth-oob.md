# ADR-002: freee の認可コードは OOB 方式で受け取る

- **Status:** Accepted
- **Date:** 2026-09-29

---

## Context

Socket Mode を選んだのは、公開 HTTP エンドポイントを持たずに済むからである。
ところが OAuth の認可コードフローでは、普通はリダイレクト先の URL でコードを受け取る。

---

## Decision

`redirect_uri=urn:ietf:wg:oauth:2.0:oob` を使う。freee の画面に表示された認可コードを、ユーザーに Slack のモーダルへ貼ってもらう。

---

## Rationale

freee は OOB 方式に対応しているので、公開エンドポイントなしで認可を完結できる。
コードを貼る手間がかかるのは最初の連携1回だけで、その後はリフレッシュトークンで自動的に更新される。

---

## Options Compared

| Option | Pros | Cons |
|---|---|---|
| **OOB 方式+モーダルに貼る** (selected) | 公開 URL が要らず、Socket Mode の利点を保てる | コピー&ペーストの手間(初回だけ) |
| 公開コールバックを立てる(ngrok や Cloud Run) | クリックだけで済む | 公開エンドポイントと、そのための基盤が増える |
| 管理者トークンで代わりに操作 | 認可が要らない | 本人名義にならない([ADR-001](ADR-001-per-user-freee-oauth.md) と矛盾) |

---

## Trade-offs

**Pros:**
- 外から入ってくる通信が一切ない

**Cons / Constraints:**
- 認可コードの有効期限は短い(約10分)。失敗したら連携ボタンを出し直す
- 認可 URL に付けている `prompt=select_company` が OOB 方式と一緒に使えるかは、実際の環境でまだ確かめていない

---

## Impact

`freee/oauth.py`、Slack の連携モーダル(`slack/blocks.py` の `link_modal`)。
