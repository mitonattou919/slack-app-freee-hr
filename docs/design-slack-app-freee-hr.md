# slack-app-freee-hr 設計書

> 最終更新: 2026-09-29

決定の背景と、検討して採らなかった選択肢は [ADR 一覧](adr/README.md) にまとめている。

---

## 1. アーキテクチャ概要

社員が Slack でボットに DM を送り、自然言語で freee人事労務 の勤怠を登録したり、有給・残業の申請を出したりするための社内ツール。
Slack とは Socket Mode で接続するので、公開 HTTP エンドポイントは持たない([ADR-002](adr/ADR-002-oauth-oob.md))。
ローカルの常時起動 Mac で、Apple `container` を使って常駐させる([ADR-003](adr/ADR-003-local-mac-apple-container.md))。

依頼の解釈は Google ADK と Gemini で作った対話エージェントが担う([ADR-006](adr/ADR-006-google-adk-gemini.md))。
ただし **エージェントは freee に書き込まない**。エージェントが作るのは「登録案(Proposal)」だけで、本人が Slack の確認カードでボタンを押したときに、アプリのコードが freee API を呼ぶ([ADR-007](adr/ADR-007-llm-drafts-human-confirms.md))。
freee の操作は、利用者ごとに OAuth で認可を取り、本人名義で行う([ADR-001](adr/ADR-001-per-user-freee-oauth.md))。

---

## 2. コンポーネント構成

```
            ┌──────────────────────── Apple container(ローカル Mac) ────────────────────────┐
Slack DM ──WebSocket(Socket Mode)──> Slack 層 ──> Agent 層(ADK Runner) ──> Gemini API
                                    │  ▲             │ 読み取り系ツール / propose_* ツール
                                    │  │             ▼
                                    │  │         Proposals 層 ──(読み取り)──> freee API
                                    │  └── 確認カード ◀─ Proposal(DB)
                                    └─ ボタン押下 ──> Executor ──(書き込み)──> freee API
                                               │
                                          SQLite(./data, ボリューム)
            └──────────────────────────────────────────────────────────────────────────┘
```

| コンポーネント | 役割 | 場所 |
|---|---|---|
| Slack 層 | DM・ボタン・モーダルを受けて振り分ける、Block Kit を組み立てる | `src/freee_hr_bot/slack/` |
| Agent 層 | ADK のエージェント・ツール・Runner。自然言語を解釈して案を作る | `src/freee_hr_bot/agent/` |
| Proposals 層 | 案のモデル、検証、下書き(service)、実行(executor) | `src/freee_hr_bot/proposals/` |
| freee 層 | OAuth(OOB)、トークンの更新、HR API のラッパー | `src/freee_hr_bot/freee/` |
| DB 層 | 連携情報・案・設定の保存(SQLite)、ADK セッション | `src/freee_hr_bot/db/` |
| 外部:Slack | 入口(DM のみ) | Socket Mode |
| 外部:freee人事労務 API | 勤怠・申請・承認経路 | `https://api.freee.co.jp/hr` |
| 外部:Gemini API | LLM(Google AI Studio の有料枠) | ADK 経由 |

---

## 3. コンポーネント詳細

### Slack 層(`slack/handlers.py`, `slack/blocks.py`)

**役割:** Slack とのやり取りをすべて受け持つ。

**主な責務:**
- `message.im` を受ける。受け付けるのは人が送った DM だけで、チャンネル、ボットの発言、編集イベントは無視する([ADR-008](adr/ADR-008-dm-only.md))
- 未連携のユーザーには連携ボタンを返す。「連携解除」と送られたら連携を削除する
- 連携済みのユーザーの発言はエージェントに渡し、返答と確認カードをスレッドに投稿する
- 確認・キャンセル・承認経路選択の各ボタンは、押した人が本人か、有効期限内か、未処理かを確認してから処理する
- 勤怠登録で所定の退勤時刻を超えた日があれば、残業申請への導線(理由入力モーダル → 残業申請の確認カード)を出す

**依存先:** Agent 層、Proposals 層、freee 層

---

### Agent 層(`agent/`)

**役割:** 自然言語の依頼を解釈して、足りない情報を聞き返し、案を作る。

**主な責務:**
- `LlmAgent`(Gemini)。システム指示には毎回、今日の日付と曜日(Asia/Tokyo)を入れる
- ツールは2種類
  - 読み取り系:`get_work_records`(現在の勤怠)、`get_paid_leave_balance`(有休の残日数)
  - 案を作るもの:`propose_work_records` / `propose_paid_leave` / `propose_overtime`。DB に Proposal を保存して ID を返すだけで、freee には書き込まない
- ツールで起きたエラーは例外として投げず、`{"status": "error", "message": ...}` で LLM に返す。LLM はそれを見てユーザーに言い換えや修正を促す
- `AgentRunner` は、Runner のイベントの中から関数の応答(function response)を探して `proposal_id` を集め、Slack 層に渡す
- LLM に渡すのは日付・時刻・残日数だけで、氏名やメールアドレスは渡さない([ADR-006](adr/ADR-006-google-adk-gemini.md))

**依存先:** Proposals 層(service)、Gemini API

---

### Proposals 層(`proposals/`)

**役割:** 書き込みの「案」を扱う中核の部分。

**主な責務:**
- `models.py`:3種類の案(`WorkRecordProposal` / `PaidLeaveProposal` / `OvertimeProposal`)と、既存勤怠のスナップショット `ExistingRecord`
- `validate.py`:副作用のない検証と変換の関数
  - 時刻は `HH:MM` 形式。日をまたぐ時刻は `25:30` のように24以上で書く
  - 未来の日付は不可、1回に7日まで、同じ日付の重複は不可
  - 退勤は出勤より後、休憩は勤務時間の中に収まり休憩同士が重ならないこと
  - 残業申請は日をまたげない(freee API の制約)
- `service.py`:案の下書き。検証したあと freee から今の勤怠・承認経路・残業設定を取得して案に書き足し、DB に保存する
- `executor.py`:ボタンが押された後に freee へ書き込む。勤怠は1日ずつ登録し、失敗した日があっても残りの日は続ける。締め済みの日は飛ばす。申請が成功したら、使った承認経路を覚えておく

**状態遷移:**

```
pending ──(確認ボタン)──> executing ──> done | failed
pending ──(キャンセル)──> cancelled
pending ──(期限切れの後に操作)──> expired
```

`pending → executing` は `UPDATE ... WHERE status='pending'` で1回だけ成功するので、ボタンが連打されても書き込みは1回になる。
有効期限は 15 分(`PROPOSAL_TTL_MINUTES`)。

---

### freee 層(`freee/`)

**役割:** freee人事労務 API との通信。

**主な責務:**
- `oauth.py`:OOB 方式の認可 URL を作り、認可コードやリフレッシュトークンを使ってトークンを取得する
- `client.py`
  - `TokenProvider` は利用者ごとに有効なアクセストークンを渡す。期限切れや 401 のときは更新する。**更新は利用者ごとにロックして1回に絞る**。freee のリフレッシュトークンは1回しか使えないので、二重に更新すると利用者が締め出されてしまう
  - `FreeeClient` は 401 が返ったら1回だけ更新してやり直す
- `hr.py`:使う API だけをまとめた `HRApi`

**使う API:**

| 用途 | エンドポイント |
|---|---|
| 連携時に本人確認 | `GET /api/v1/users/me`(事業所ごとの `employee_id`) |
| 勤怠の取得・更新 | `GET`/`PUT /api/v1/employees/{id}/work_records/{date}` |
| 有休の残日数 | `GET /api/v1/employees/{id}/work_record_summaries/{y}/{m}`(`num_paid_holidays_left`) |
| 承認経路 | `GET /api/v1/approval_flow_routes?usage=AttendanceWorkflow` |
| 有給申請 | `POST /api/v1/approval_requests/paid_holidays` |
| 残業の設定と申請 | `GET .../overtime_works/setting`、`POST /api/v1/approval_requests/overtime_works` |

**実装時に分かった API の癖:**
- 有給申請の区分は `morning` / `afternoon` だが、勤怠データ側では `morning_off` / `afternoon_off` と表記が違う
- 残業申請で送る項目は、会社の「勤怠に反映する」設定によって変わる
  - 反映する場合:`overtime_work_start_at` / `overtime_work_end_at`。開始時刻は所定の退勤時刻と同じでなければならない
  - 反映しない場合:`start_at` / `end_at`
- 残業申請の時刻は `HH:MM` 形式で、日をまたげない
- 勤怠の PUT では `work_record_segments` と `break_records` を指定し、日時は `YYYY-MM-DD HH:MM:SS`(現地時刻)で書く。PUT はその日の勤怠を丸ごと置き換える

**モックについて:**
- 自動テストでは、freee を `respx` で、Gemini を決まった応答を返す偽の LLM(`BaseLlm` のサブクラス)で置き換える
- 実際の API で確かめるときは freee の**テスト事業所**を使う([ADR-012](adr/ADR-012-test-strategy.md))

---

### DB 層(`db/`)

**役割:** 保存が必要な状態を SQLite に置く。

| テーブル | 内容 |
|---|---|
| `user_links` | Slack ユーザー ↔ freee の `user_id` と `employee_id`、Fernet で暗号化したトークン、有効期限 |
| `proposals` | 案の中身(JSON)、状態、持ち主、投稿先(チャンネルとスレッド)、有効期限、実行結果 |
| `user_preferences` | 前回使った承認経路など |
| (ADK のテーブル) | `DatabaseSessionService` の会話セッション。同じ SQLite ファイルに入る |

DB へのアクセスは `Repository` にまとめてあるので、Postgres に移すときは接続 URL と `db/repo.py` だけ直せばよい([ADR-011](adr/ADR-011-sqlite-fernet-storage.md))。

---

## 4. 認証フロー

| 対象 | 方式 |
|---|---|
| Slack ↔ ボット | Socket Mode。App-Level Token(`xapp-`)と Bot Token(`xoxb-`) |
| ボット ↔ freee | 利用者ごとの OAuth 2.0 認可コードフロー(OOB 方式)。1事業所に固定 |
| ボット ↔ Gemini | Google AI Studio の API キー(有料枠) |
| ローカル開発 | freee のテスト事業所に、本番と同じ OAuth で接続する(認証を飛ばす仕組みは作っていない) |

**freee と連携する流れ([ADR-001](adr/ADR-001-per-user-freee-oauth.md), [ADR-002](adr/ADR-002-oauth-oob.md), [ADR-009](adr/ADR-009-single-company.md)):**

1. 未連携のユーザーが DM を送ると、ボットが「freee と連携する」ボタンを返す
2. ボタンを押すとモーダルが開く。認可 URL(`redirect_uri=urn:ietf:wg:oauth:2.0:oob`)へのリンクと、コードの入力欄がある
3. ユーザーが freee で許可すると、画面に認可コードが表示されるので、それをモーダルに貼る
4. ボットはコードをトークンに交換し、`GET /users/me` で `FREEE_COMPANY_ID` の事業所の `employee_id` を取得する。その事業所の従業員として登録されていなければ、連携を断る
5. トークンを Fernet で暗号化して `user_links` に保存する

アクセストークンの有効期限は約6時間。期限の5分前か、401 が返った時点で更新する。更新するたびにリフレッシュトークンは新しいものに置き換わる。

---

## 5. セッション管理

| 対象 | 方式 |
|---|---|
| 会話(ADK のセッション) | DM のスレッド単位で、`session_id = "{channel}:{thread_ts}"`。`DatabaseSessionService` で SQLite に保存するので、再起動しても会話は続く。同じスレッドの発言は `asyncio.Lock` で1つずつ処理する |
| 案(Proposal) | DB に保存し、有効期限は 15 分。ボタンに入れるのは `proposal_id` だけで、中身は DB から読む(書き換え防止) |
| 前回の承認経路 | `user_preferences` に保存する |

> **制約:** Proposal の状態遷移は DB で守られているので複数プロセスでも安全だが、会話ロックと更新ロックはプロセスの中でしか効かない。**インスタンスは1つ**で動かすこと。Socket Mode で複数の接続を張ると、同じイベントがどちらに届くか決まらない。スケールアウトするなら、Postgres に移したうえでロックを DB 側に移す必要がある。

---

## 6. デプロイ構成

| コンポーネント | インフラ | 備考 |
|---|---|---|
| ボット本体 | ローカル Mac(Apple silicon、macOS 26 以降)+ Apple `container` | `Containerfile`、`make build` / `make run` |
| DB | SQLite。ホストの `./data` を `/app/data` にマウントする | バックアップ対象。`FERNET_KEY` も一緒に保管する |
| 設定 | `.env`(`--env-file` で渡す) | `.env.example` を参照 |

**運用上の注意([ADR-003](adr/ADR-003-local-mac-apple-container.md)):**
- Mac が止まっている間はボットも止まる。その間は freee 本体で操作してもらう
- スリープを無効にする(`sudo pmset -a sleep 0` など)。Mac を再起動したら `make restart` で起動し直す
- イメージはそのままクラウド(Cloud Run の min-instances=1 や ECS など)に持っていける。移すときは DB を Postgres に替える

---

## 7. ローカル開発環境

```bash
uv sync                    # 依存関係(Python 3.12)
cp .env.example .env       # テスト事業所の値を入れる
make key                   # FERNET_KEY を生成する

make check                 # ruff → mypy(strict)→ pytest
uv run freee-hr-bot        # コンテナなしで起動
make build && make run     # Apple container で起動
make logs
```

**テストの構成(82件、カバレッジ約 89%):**

| 対象 | ファイル |
|---|---|
| 検証・変換 | `tests/test_validate.py` |
| トークン更新(同時に更新が走る場合を含む) | `tests/test_client.py` |
| 下書き・実行 | `tests/test_service.py`, `tests/test_executor.py` |
| エージェントのツール | `tests/test_tools.py` |
| ADK Runner(偽の LLM で最後まで) | `tests/test_agent_runner.py` |
| Slack ハンドラ(偽の Bolt App) | `tests/test_handlers.py` |
| Repository(状態遷移の競合を含む) | `tests/test_repo.py` |
| Block Kit | `tests/test_blocks.py` |

**まだ確認していないこと:** 本物の Slack・freee・Gemini と、Apple container 上での起動。テスト事業所で確かめる手順は [ADR-012](adr/ADR-012-test-strategy.md) を参照。

---

## 8. 対象外(Phase 2 の候補)

- リアルタイム打刻(出勤・退勤ボタン)
- 勤務時間の修正、特別休暇、月次締め、時間単位の有給の申請
- 承認者としての操作(承認・差し戻し、通知)
- Slack の AI アシスタント画面(Agents & AI Apps)とスラッシュコマンド
- クラウドへの移行と、Postgres を前提にしたスケールアウト
