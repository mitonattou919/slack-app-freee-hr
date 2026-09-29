# slack-app-freee-hr

Slack の DM で話しかけるだけで、**freee人事労務** の勤怠登録や有給・残業申請ができるボット。
Slack とは Socket Mode で接続するので、公開 URL は不要。

```
あなた:  昨日 9:00〜19:00 休憩12:00〜13:00
ボット:  確認カードを出しました
         ┌ 勤怠登録の確認 ─────────────────────
         │ 09/28(月) ⚠️ 上書き
         │ 現在: 勤務 09:00〜18:00 / 休憩 12:00〜13:00
         │ → 新規: 09:00〜19:00 / 休憩 12:00〜13:00
         │ [登録する] [キャンセル]
         └───────────────────────────────
```

## できること(MVP)

| 機能 | 例 |
|---|---|
| 勤怠登録(出退勤・休憩、最大7日まで一括) | 「9/29 9:00〜19:00 休憩12-13」「今週月〜水 9:30-18:30 休憩1h」 |
| 有給申請(全休・午前休・午後休) | 「来週金曜 午前休」 |
| 残業申請 | 「今日 18:00〜21:00 残業申請、理由はリリース対応」 |
| 参照 | 「有休あと何日?」「先週の勤怠見せて」 |
| 連携解除 | 「連携解除」 |

勤怠登録の結果が所定の退勤時刻を超えた場合は、「残業申請を作る」ボタンが出る。

## 設計のポイント

- **AI は freee に書き込まない。** Google ADK のエージェント(Gemini)が作るのは「案」だけ。
  実際の書き込みは、本人が確認カードのボタンを押したときにアプリのコードが行う。
- 確認カードには、押した人が本人か、15分の有効期限内か、処理済みでないかのチェックがある。
  状態は `pending → executing` へアトミックに遷移させるので、ダブルクリックしても二重登録されない。
- 勤怠登録ではまず今の値を取得して、差分と「⚠️上書き」を表示する。締め済みの日は登録できない。
- 承認経路は、候補が1つなら自動で選ぶ。複数あるときはカードで本人が選ぶ(前回の選択を覚えておく)。LLM には選ばせない。
- freee の OAuth は **OOB 方式**。画面に表示された認可コードを Slack のモーダルに貼ってもらう。トークンは Fernet で暗号化して SQLite に保存する。
- Gemini に送るのは日付・時刻・有休残日数だけで、氏名やメールアドレスは送らない。

```
Slack DM ─(Socket Mode)─> slack_bolt ─> ADK Runner(スレッド単位のセッション)
                                           │ 読み取り系ツール: freee から取得
                                           │ propose_* ツール: Proposal を DB に保存
                                           ▼
                                   確認カード ─[ボタン]→ executor ─> freee API
```

## ドキュメント

- [設計書](docs/design-slack-app-freee-hr.md):構成、コンポーネント、認証、セッション、デプロイ
- [ADR 一覧](docs/adr/README.md):決定の理由と、採らなかった選択肢

## セットアップ

### 1. Slack アプリ
1. https://api.slack.com/apps →「Create New App」→「From a manifest」を選び、`slack_manifest.yaml` の内容を貼る
2. Basic Information → App-Level Tokens で、`connections:write` を付けたトークンを作る → `SLACK_APP_TOKEN`(`xapp-...`)
3. ワークスペースにインストールする → Bot User OAuth Token → `SLACK_BOT_TOKEN`(`xoxb-...`)

### 2. freee アプリ
1. [freee アプリストアの開発者ページ](https://app.secure.freee.co.jp/developers/applications)でアプリを作る
2. コールバック URL には `urn:ietf:wg:oauth:2.0:oob` を指定する
3. 権限では、人事労務の「勤怠」「各種申請(有給・残業)」「申請経路」「ログインユーザー」の参照・更新を有効にする
4. Client ID / Secret を `FREEE_CLIENT_ID` / `FREEE_CLIENT_SECRET` に、対象事業所の ID を `FREEE_COMPANY_ID` に設定する
5. **開発中は freee のテスト事業所を使う**(本番の事業所の勤怠を上書きしないため)

### 3. Gemini
Google AI Studio で API キーを発行して `GEMINI_API_KEY` に設定する。
**有料枠(請求先を紐付けた状態)を使うこと。** 無料枠では、入力が Google のサービス改善に使われることがある。
モデルは `GEMINI_MODEL` で切り替えられる(既定は `gemini-2.5-flash`。提供終了になったら最新の Flash 系に変える)。

### 4. 起動(Apple container)
前提:Apple silicon の Mac、macOS 26 以降、[apple/container](https://github.com/apple/container) がインストール済み。

```sh
cp .env.example .env     # 値を埋める
make key                 # 出力を FERNET_KEY に設定
container system start   # 初回のみ
make build && make run
make logs                # 「⚡️ Bolt app is running!」が出れば OK
```

データ(SQLite)はホストの `./data/` に保存される。`FERNET_KEY` を失くすと保存済みのトークンを復号できず、全員が再連携になる。

### 常駐運用の注意
Mac がスリープしたり、電源が落ちたりしている間はボットが応答しない。その間は freee 本体で操作してもらうこと。
- スリープを無効にする:システム設定 → エネルギー →「ディスプレイがオフのときに自動でスリープさせない」をオン(または `sudo pmset -a sleep 0`)
- Mac を再起動したら `make restart` で起動し直す
- イメージはそのままクラウド(Cloud Run の min-instances=1 や ECS など)に持っていける。その場合は DB を Postgres に替える(`db/repo.py` だけ直せばよい)

## 開発

```sh
uv sync
make test    # pytest(freee は respx でモック、LLM は偽モデルで ADK Runner ごとテスト)
make lint
make typecheck   # mypy (strict)
uv run freee-hr-bot   # コンテナを使わずローカルで起動
```

```
src/freee_hr_bot/
  config.py            環境変数
  crypto.py            トークン暗号化
  db/                  SQLAlchemy モデルとリポジトリ
  freee/               OAuth(OOB)、トークン更新、HR API のラッパー
  proposals/           案のモデル、検証、下書き(service)、実行(executor)
  agent/               ADK エージェント、ツール、Runner
  slack/               Block Kit とハンドラ
```

## 今後(Phase 2 の候補)
リアルタイム打刻、勤務時間修正・特別休暇・月次締めの申請、承認者の操作、Slack の AI アシスタント画面への対応、クラウドへの移行。
