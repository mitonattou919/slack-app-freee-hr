# Architecture Decision Records

| # | 決定 | Status |
|---|---|---|
| [ADR-001](ADR-001-per-user-freee-oauth.md) | freee は利用者ごとに OAuth 認可を取り、本人名義で操作する | Accepted |
| [ADR-002](ADR-002-oauth-oob.md) | freee の認可コードは OOB 方式で受け取る | Accepted |
| [ADR-003](ADR-003-local-mac-apple-container.md) | ローカルの常時起動 Mac で Apple container を使って常駐させる | Accepted |
| [ADR-004](ADR-004-mvp-scope.md) | MVP は「後から自然言語で勤怠を登録する」機能と有給・残業申請に絞る | Accepted |
| [ADR-005](ADR-005-direct-work-record-update.md) | 勤怠は work_records API で直接更新し、上書きになる日は差分を見せる | Accepted |
| [ADR-006](ADR-006-google-adk-gemini.md) | 自然言語の解釈には Google ADK と Gemini(AI Studio の有料枠)を使う | Accepted |
| [ADR-007](ADR-007-llm-drafts-human-confirms.md) | LLM は案を作るだけにして、書き込みは本人がボタンを押したときにコードが行う | Accepted |
| [ADR-008](ADR-008-dm-only.md) | 入口はアプリとの DM だけにする | Accepted |
| [ADR-009](ADR-009-single-company.md) | 対象の freee 事業所は1つに固定する | Accepted |
| [ADR-010](ADR-010-approval-route-selection.md) | 承認経路は、候補が1つなら自動、複数ならカードで本人が選ぶ | Accepted |
| [ADR-011](ADR-011-sqlite-fernet-storage.md) | 状態は SQLite に保存し、freee のトークンは Fernet で暗号化する | Accepted |
| [ADR-012](ADR-012-test-strategy.md) | 開発は freee のテスト事業所で行い、自動テストでは外部 API をモックする | Accepted |

決定を変えるときは、元の ADR を書き換えずに `Superseded by ADR-xxx` とし、新しい番号で書く。
