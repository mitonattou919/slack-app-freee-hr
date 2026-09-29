"""Executes a confirmed proposal against freee. Only reached from a Slack button click."""

from dataclasses import dataclass, field
from datetime import date

from freee_hr_bot.freee.client import FreeeAPIError
from freee_hr_bot.proposals.models import (
    OvertimeProposal,
    PaidLeaveProposal,
    TimeRange,
    WorkRecordProposal,
    load_proposal,
)
from freee_hr_bot.proposals.service import ROUTE_PREF_KEY, Deps, hr_for
from freee_hr_bot.proposals.validate import (
    break_payload,
    from_freee_datetime,
    to_freee_datetime,
    to_minutes,
)


@dataclass
class OvertimeSuggestion:
    date: date
    start: str
    end: str


@dataclass
class ExecutionResult:
    ok: bool
    lines: list[str] = field(default_factory=list)
    overtime_suggestions: list[OvertimeSuggestion] = field(default_factory=list)


class MissingRouteError(ValueError):
    pass


def _error_text(e: Exception) -> str:
    if isinstance(e, FreeeAPIError):
        return " / ".join(e.messages) or f"HTTP {e.status}"
    return str(e)


def overtime_suggestion(day: date, clock_out: str, response: dict) -> OvertimeSuggestion | None:
    """Suggest an overtime request when the registered clock-out is past the scheduled end."""
    scheduled_end = from_freee_datetime(day, response.get("normal_work_clock_out_at"))
    if not scheduled_end or response.get("day_pattern", "normal_day") != "normal_day":
        return None
    end = min(to_minutes(clock_out), 23 * 60 + 59)  # overtime requests are same-day only
    if end <= to_minutes(scheduled_end):
        return None
    return OvertimeSuggestion(date=day, start=scheduled_end, end=f"{end // 60:02d}:{end % 60:02d}")


async def execute(deps: Deps, slack_user_id: str, payload: dict) -> ExecutionResult:
    proposal = load_proposal(payload)
    hr = await hr_for(deps, slack_user_id)

    if isinstance(proposal, WorkRecordProposal):
        result = ExecutionResult(ok=True)
        for entry in proposal.entries:
            label = entry.date.strftime("%m/%d")
            if entry.existing and not entry.existing.is_editable:
                result.ok = False
                result.lines.append(f"❌ {label}: 締め済みなどで編集できないためスキップしました")
                continue
            try:
                segment = TimeRange(start=entry.clock_in, end=entry.clock_out)
                response = await hr.put_work_record(
                    entry.date,
                    segments=[
                        {
                            "clock_in_at": to_freee_datetime(entry.date, segment.start),
                            "clock_out_at": to_freee_datetime(entry.date, segment.end),
                        }
                    ],
                    breaks=break_payload(entry.date, entry.breaks),
                )
            except Exception as e:  # report per day, keep going
                result.ok = False
                result.lines.append(f"❌ {label}: {_error_text(e)}")
                continue
            result.lines.append(f"✅ {label}: {entry.clock_in}〜{entry.clock_out} を登録しました")
            suggestion = overtime_suggestion(entry.date, entry.clock_out, response or {})
            if suggestion:
                result.overtime_suggestions.append(suggestion)
        return result

    if isinstance(proposal, PaidLeaveProposal | OvertimeProposal):
        if proposal.route_id is None:
            raise MissingRouteError("承認経路を選択してください")
        try:
            if isinstance(proposal, PaidLeaveProposal):
                await hr.create_paid_holiday(
                    proposal.date, proposal.leave_type, proposal.route_id, proposal.comment
                )
                done = "有給申請を提出しました"
            else:
                await hr.create_overtime(
                    proposal.date,
                    start_at=proposal.start,
                    end_at=proposal.end,
                    reflect_in_work_record=proposal.reflect_in_work_record,
                    route_id=proposal.route_id,
                    comment=proposal.comment,
                )
                done = "残業申請を提出しました"
        except Exception as e:
            return ExecutionResult(ok=False, lines=[f"❌ 申請に失敗しました: {_error_text(e)}"])
        await deps.repo.set_pref(slack_user_id, ROUTE_PREF_KEY, str(proposal.route_id))
        return ExecutionResult(ok=True, lines=[f"✅ {proposal.date.strftime('%m/%d')} の{done}"])

    raise TypeError(f"unknown proposal: {proposal!r}")
