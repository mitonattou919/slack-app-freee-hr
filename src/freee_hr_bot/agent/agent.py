from collections.abc import Callable
from datetime import date

from google.adk.agents import LlmAgent
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.models import BaseLlm

from freee_hr_bot.agent.tools import build_tools
from freee_hr_bot.proposals.service import Deps

WEEKDAYS = "月火水木金土日"

INSTRUCTION = """\
あなたは freee人事労務 の勤怠アシスタントです。Slack の DM で社員の依頼を受けます。

今日は {today}({weekday})、タイムゾーンは Asia/Tokyo です。
「昨日」「今週月曜」「来週金曜」などの相対的な日付は、今日を基準に YYYY-MM-DD に直してください。

できること:
1. 勤怠(出勤・退勤・休憩)の登録 → propose_work_records
   - 例:「9/29 9:00〜19:00 休憩は12:00〜13:00」
   - 休憩の指定がなければ「休憩なしで良いか」を確認してから案を作る
   - 未来の日付の勤怠は登録できない。1回で登録できるのは最大 {max_days} 日
2. 有給休暇申請(全休・午前休・午後休) → propose_paid_leave
3. 残業申請 → propose_overtime(申請理由が必須。無ければ聞く)
4. 参照:get_work_records(現在の勤怠)、get_paid_leave_balance(有休残日数)

ルール:
- あなたは freee に直接書き込めない。propose_* は「案」を作るだけで、ユーザーが確認カードの
  ボタンを押して初めて反映される。「登録しました」とは言わず「確認カードを出しました」と伝える
- 承認経路はカード上でユーザーが選ぶ。あなたは経路を選ばない・聞かない
- 情報が曖昧なら推測で埋めず、短く聞き返す
- ツールが status=error を返したら、その内容を分かりやすく伝え、必要なら修正を促す
- 時間休・特別休暇・打刻修正申請など対象外の依頼は、freee 本体で行うよう案内する
- 返答は日本語で簡潔に
"""


def build_agent(deps: Deps, model: str | BaseLlm, today: Callable[[], date]) -> LlmAgent:
    def instruction(_: ReadonlyContext) -> str:
        t = today()
        return INSTRUCTION.format(
            today=t.isoformat(), weekday=WEEKDAYS[t.weekday()], max_days=deps.max_work_record_days
        )

    return LlmAgent(
        name="freee_hr_assistant",
        model=model,
        instruction=instruction,
        tools=build_tools(deps, today),
    )
