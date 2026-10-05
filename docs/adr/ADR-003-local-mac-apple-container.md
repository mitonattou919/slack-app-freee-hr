# ADR-003: ローカルの常時起動 Mac で Apple container を使って常駐させる

- **Status:** Accepted
- **Date:** 2026-09-29

---

## Context

Socket Mode は WebSocket をつなぎっぱなしにする常駐プロセスなので、サーバーレスとは相性が悪い。
利用者ごとのトークンを持つので、保存先も永続化が必要になる。
オーナーは、せっかくなのでローカルの Apple `container` で動かしたいと考えている。

---

## Decision

ボットは常時起動の Mac で Apple `container` を使って動かす。コンテナイメージはクラウドにそのまま移せるように作る。

---

## Rationale

- ランニングコストがかからず、手元で運用できる
- Containerfile は OCI 準拠なので、Cloud Run や ECS にも同じイメージで移せる
- Apple container には Compose に当たる仕組みが無いので、`Makefile` で `container build` / `container run` をまとめる

---

## Options Compared

| Option | Pros | Cons |
|---|---|---|
| **ローカル Mac+Apple container** (selected) | 費用ゼロ。オーナーの希望 | Mac が止まるとボットも止まる |
| Docker Compose+SQLite(社内サーバー) | 構成がよく知られている | サーバーを用意する必要がある |
| クラウドのコンテナ+マネージド DB | 止まりにくい | 費用と基盤の手間 |

---

## Trade-offs

**Pros:**
- 最小の構成ですぐ始められる

**Cons / Constraints:**
- **止まりやすい。** Mac のスリープ、再起動、ネットワーク切断の間は全員分のボットが止まる。その間は freee 本体で操作してもらう運用にする
- インスタンスは1つだけ(設計書「5. セッション管理」の制約を参照)

---

## Impact

`Containerfile`、`Makefile`、README の運用上の注意。クラウドに移すときは [ADR-011](ADR-011-sqlite-fernet-storage.md) の DB を Postgres に替える。
