# ADR-011: 状態は SQLite に保存し、freee のトークンは Fernet で暗号化する

- **Status:** Accepted
- **Date:** 2026-09-29

---

## Context

保存が必要な状態は、利用者ごとの freee トークン、Proposal、ADK の会話セッション、前回の承認経路。
構成は1台のローカル Mac で、インスタンスも1つ([ADR-003](ADR-003-local-mac-apple-container.md))。

---

## Decision

- SQLite(`./data/bot.db`)を1ファイルだけ使い、アプリのテーブルと ADK の `DatabaseSessionService` を同じファイルに置く
- アクセストークンとリフレッシュトークンは Fernet で暗号化して保存する。鍵は `FERNET_KEY` で渡す
- DB へのアクセスは `Repository` にまとめ、Postgres に移しやすくしておく

---

## Rationale

1台・1インスタンスなら SQLite で十分で、運用の手間もほぼない。
トークンは本人の権限そのものなので、ファイルが漏れてもそのままでは使えないようにしておく。

---

## Options Compared

| Option | Pros | Cons |
|---|---|---|
| **SQLite+Fernet** (selected) | 構成がいちばん小さく、バックアップも簡単 | スケールアウトできない |
| Postgres | スケールでき、クラウドに移しやすい | ローカルで動かすには大げさ |
| macOS のキーチェーン | OS の保護機能が使える | コンテナから使えない |

---

## Trade-offs

**Pros:**
- `./data` と `FERNET_KEY` を保管しておけば復元できる

**Cons / Constraints:**
- **`FERNET_KEY` を失くすと保存済みのトークンを復号できず、全員が連携し直しになる**
- SQLite ではタイムゾーンの情報が消えるので、読み出すときに UTC として扱い直している(`repo._aware`)

---

## Impact

`db/`、`crypto.py`、`.env.example`、クラウドに移すときの作業。
