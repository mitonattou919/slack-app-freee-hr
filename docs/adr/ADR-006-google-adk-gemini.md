# ADR-006: 自然言語の解釈には Google ADK と Gemini(AI Studio の有料枠)を使う

- **Status:** Accepted
- **Date:** 2026-09-29

---

## Context

「昨日は9時から7時まで、昼休憩1時間」「今週月〜水は9:30-18:30」「来週金曜 午前休」のように、書き方の揺れや相対的な日付、複数日の指定を扱う必要がある。
LLM に送るのは社員の勤怠時刻や休暇の予定、残業の理由といった個人に関わる情報である。

---

## Decision

- Google ADK(`google-adk`)で対話エージェントを作り、LLM には Google AI Studio の Gemini を使う
- API キーは**有料枠**のものを使う
- モデルは `GEMINI_MODEL` で指定する(既定は Flash 系)
- LLM に渡す情報は最小限にして、氏名やメールアドレスは渡さない

---

## Rationale

- ADK を使うと、ツール呼び出し・会話の続き・セッションの永続化(`DatabaseSessionService`)がまとめて手に入る
- AI Studio の無料枠では、入力が Google のサービス改善に使われることがある。有料枠ならそれが無いので、勤怠データを扱う用途に向いている
- 実際の利用量なら費用はわずか

---

## Options Compared

| Option | Pros | Cons |
|---|---|---|
| **ADK+Gemini(AI Studio の有料枠)** (selected) | 対話エージェントの仕組みがまとめて揃う。オーナーの希望 | ADK はまだ変化が速い |
| Claude API(Haiku) | tool use の精度が高い | オーナーの希望と違う |
| ルールベース(正規表現+日付パーサー) | 外に何も送らず、結果が毎回同じ | 書き方を固定しないとすぐ解釈できなくなる |
| Gemini の無料枠 | 費用ゼロ | 入力が学習に使われる恐れ |
| Vertex AI 経由 | 企業向けの規約 | GCP プロジェクトとサービスアカウントの手間 |

---

## Trade-offs

**Pros:**
- 表現の揺れ、聞き返し、複数日の指定を自然に扱える

**Cons / Constraints:**
- LLM は間違えることがある。その対策は [ADR-007](ADR-007-llm-drafts-human-confirms.md) で扱う
- モデルは提供終了することがあるので、`GEMINI_MODEL` で差し替えられるようにしてある
- ADK 2.x を使う。関数宣言には JSON Schema の実験的な機能(`JSON_SCHEMA_FOR_FUNC_DECL`)が有効になっていて、警告が出る

---

## Impact

`agent/`、`main.py`(`GOOGLE_API_KEY`、`GOOGLE_GENAI_USE_VERTEXAI=FALSE`)、ツールが返すデータの形。
