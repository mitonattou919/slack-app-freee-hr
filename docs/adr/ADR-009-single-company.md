# ADR-009: 対象の freee 事業所は1つに固定する

- **Status:** Accepted
- **Date:** 2026-09-29

---

## Context

freee のアカウントは複数の事業所に所属できるので、どの事業所で操作するかを決める必要がある。

---

## Decision

`FREEE_COMPANY_ID` で1つの事業所に固定する。連携するとき `GET /users/me` を呼び、その事業所の従業員として登録されていないユーザーは断る。

---

## Rationale

社内ツールなので、対象は1事業所で足りる。事業所を選ばせる画面を作ったり状態を持ったりする必要がなくなる。

---

## Options Compared

| Option | Pros | Cons |
|---|---|---|
| **1事業所に固定** (selected) | 単純で、誤った事業所を操作しない | グループ会社では使えない |
| 連携のときに事業所を選ばせる | 複数の事業所に対応できる | 画面と保存する状態が増える |

---

## Trade-offs

**Pros:**
- 連携の流れが単純

**Cons / Constraints:**
- 複数の事業所に対応するときは `user_links` に `company_id` を持たせる必要がある

---

## Impact

`config.freee_company_id`、連携処理(`on_link_submit`)、`HRApi`。
