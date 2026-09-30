"""Tools exposed to the agent.

Read tools return data. propose_* tools only draft a proposal and return its id; the Slack
layer renders a confirmation card and writes to freee only after the user clicks the button.
Tool outputs contain dates/times/balances only - no names or emails reach the LLM.
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import date
from typing import Any, Literal

import httpx
from google.adk.tools.tool_context import ToolContext
from pydantic import BaseModel, Field, ValidationError

from freee_hr_bot.freee.client import FreeeAPIError, NotLinkedError
from freee_hr_bot.proposals import service
from freee_hr_bot.proposals.models import TimeRange, WorkRecordEntry
from freee_hr_bot.proposals.service import Deps, hr_for, parse_existing
from freee_hr_bot.proposals.validate import ValidationError as ProposalValidationError

PROPOSAL_KEY = "proposal_id"
logger = logging.getLogger(__name__)


class BreakInput(BaseModel):
    start: str = Field(description="休憩開始 HH:MM")
    end: str = Field(description="休憩終了 HH:MM")


class WorkDayInput(BaseModel):
    date: str = Field(description="対象日 YYYY-MM-DD")
    clock_in: str = Field(description="出勤時刻 HH:MM")
    clock_out: str = Field(
        description="退勤時刻 HH:MM。日付をまたぐ場合は 25:30 のように24以上で表す"
    )
    breaks: list[BreakInput] = Field(default=[], description="休憩時間帯のリスト")


def _error(e: Exception) -> dict[str, Any]:
    if isinstance(e, NotLinkedError):
        return {"status": "error", "message": "freee と未連携です"}
    if isinstance(e, FreeeAPIError):
        return {"status": "error", "message": " / ".join(e.messages) or f"HTTP {e.status}"}
    if isinstance(e, ProposalValidationError | ValidationError | ValueError):
        return {"status": "error", "message": str(e)}
    if isinstance(e, httpx.TimeoutException):
        logger.warning("freee API timed out: %r", e)
        return {
            "status": "error",
            "message": "freee の応答がタイムアウトしました。期間を短くして再度お試しください",
        }
    logger.exception("tool failed")
    return {
        "status": "error",
        "message": "内部エラーが発生しました。時間をおいて再度お試しください",
    }


def build_tools(deps: Deps, today: Callable[[], date]) -> list[Callable[..., Any]]:
    def where(ctx: ToolContext) -> tuple[str, str, str]:
        return ctx.user_id, ctx.state["channel_id"], ctx.state["thread_ts"]

    async def get_work_records(dates: list[str], tool_context: ToolContext) -> dict[str, Any]:
        """freee に現在登録されている勤怠を日付ごとに取得する(読み取りのみ)。

        Args:
            dates: 取得したい日付 YYYY-MM-DD のリスト(最大31日)
        """
        try:
            days = [date.fromisoformat(d) for d in dates[:31]]
            hr = await hr_for(deps, tool_context.user_id)
            records = await asyncio.gather(*(hr.get_work_record(d) for d in days))
            return {
                "status": "ok",
                "records": {
                    d.isoformat(): parse_existing(d, r).model_dump(mode="json")
                    for d, r in zip(days, records, strict=True)
                },
            }
        except Exception as e:
            return _error(e)

    async def get_paid_leave_balance(tool_context: ToolContext) -> dict[str, Any]:
        """有給休暇の残日数を取得する(読み取りのみ)。"""
        try:
            hr = await hr_for(deps, tool_context.user_id)
            t = today()
            summary = await hr.get_summary(t.year, t.month)
            return {
                "status": "ok",
                "days_left": summary.get("num_paid_holidays_left"),
                "days_and_hours_left": summary.get("num_paid_holidays_and_hours_left"),
            }
        except Exception as e:
            return _error(e)

    async def propose_work_records(
        days: list[WorkDayInput], tool_context: ToolContext
    ) -> dict[str, Any]:
        """勤怠(出退勤・休憩)の登録案を作る。freee にはまだ書き込まない。

        ユーザーに確認カードが表示され、ユーザーが「登録」を押したときだけ反映される。

        Args:
            days: 登録する日ごとの出勤・退勤・休憩
        """
        user, channel, thread = where(tool_context)
        try:
            entries = [
                WorkRecordEntry(
                    date=date.fromisoformat(d.date),
                    clock_in=d.clock_in,
                    clock_out=d.clock_out,
                    breaks=[TimeRange(start=b.start, end=b.end) for b in d.breaks],
                )
                for d in days
            ]
            p = await service.draft_work_records(
                deps,
                slack_user_id=user,
                entries=entries,
                today=today(),
                channel_id=channel,
                thread_ts=thread,
            )
        except Exception as e:
            return _error(e)
        return {
            "status": "drafted",
            PROPOSAL_KEY: p.id,
            "note": "確認カードを表示した。ユーザーのボタン操作を待つこと",
        }

    async def propose_paid_leave(
        target_date: str,
        leave_type: Literal["full", "morning", "afternoon"],
        tool_context: ToolContext,
        comment: str | None = None,
    ) -> dict[str, Any]:
        """有給休暇申請の案を作る。freee にはまだ申請しない。

        Args:
            target_date: 取得日 YYYY-MM-DD(複数日の場合は日ごとに呼ぶ)
            leave_type: full=全休, morning=午前休, afternoon=午後休
            comment: 申請理由(任意)
        """
        user, channel, thread = where(tool_context)
        try:
            p = await service.draft_paid_leave(
                deps,
                slack_user_id=user,
                day=date.fromisoformat(target_date),
                leave_type=leave_type,
                comment=comment,
                channel_id=channel,
                thread_ts=thread,
            )
        except Exception as e:
            return _error(e)
        return {
            "status": "drafted",
            PROPOSAL_KEY: p.id,
            "note": "確認カードを表示した。ユーザーのボタン操作を待つこと",
        }

    async def propose_overtime(
        target_date: str, start: str, end: str, comment: str, tool_context: ToolContext
    ) -> dict[str, Any]:
        """残業申請の案を作る。freee にはまだ申請しない。申請理由が不明なら先にユーザーに聞くこと。

        Args:
            target_date: 対象日 YYYY-MM-DD
            start: 残業開始 HH:MM(当日内)
            end: 残業終了 HH:MM(当日内、最大 23:59)
            comment: 申請理由(必須)
        """
        user, channel, thread = where(tool_context)
        try:
            p = await service.draft_overtime(
                deps,
                slack_user_id=user,
                day=date.fromisoformat(target_date),
                start=start,
                end=end,
                comment=comment,
                channel_id=channel,
                thread_ts=thread,
            )
        except Exception as e:
            return _error(e)
        return {
            "status": "drafted",
            PROPOSAL_KEY: p.id,
            "note": "確認カードを表示した。ユーザーのボタン操作を待つこと",
        }

    return [
        get_work_records,
        get_paid_leave_balance,
        propose_work_records,
        propose_paid_leave,
        propose_overtime,
    ]
