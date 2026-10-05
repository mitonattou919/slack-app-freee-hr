"""Drafting proposals: validate what the agent asked for and enrich it with freee data.

Nothing here writes to freee. Writes happen in executor.py after the owner confirms.
"""

import asyncio
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import httpx

from freee_hr_bot.db.models import Proposal
from freee_hr_bot.db.repo import Repository
from freee_hr_bot.freee.client import FreeeClient, NotLinkedError, TokenProvider
from freee_hr_bot.freee.hr import HRApi
from freee_hr_bot.proposals.models import (
    ExistingRecord,
    OvertimeProposal,
    PaidLeaveProposal,
    RouteOption,
    TimeRange,
    WorkRecordEntry,
    WorkRecordProposal,
)
from freee_hr_bot.proposals.validate import (
    from_freee_datetime,
    validate_overtime_range,
    validate_work_entries,
)

ROUTE_PREF_KEY = "last_attendance_route_id"


@dataclass
class Deps:
    repo: Repository
    tokens: TokenProvider
    http: httpx.AsyncClient
    company_id: int
    ttl: timedelta
    max_work_record_days: int


async def hr_for(deps: Deps, slack_user_id: str) -> HRApi:
    link = await deps.repo.get_link(slack_user_id)
    if link is None:
        raise NotLinkedError(slack_user_id)
    client = FreeeClient(deps.http, deps.tokens, slack_user_id)
    return HRApi(client, deps.company_id, link.employee_id)


def parse_existing(day: date, record: dict[str, Any]) -> ExistingRecord:
    def ranges(items: list[dict[str, Any]] | None) -> list[TimeRange]:
        out = []
        for it in items or []:
            start = from_freee_datetime(day, it.get("clock_in_at"))
            end = from_freee_datetime(day, it.get("clock_out_at"))
            if start and end:
                out.append(TimeRange(start=start, end=end))
        return out

    paid = record.get("paid_holidays") or []
    return ExistingRecord(
        segments=ranges(record.get("work_record_segments")),
        breaks=ranges(record.get("break_records")),
        paid_holiday=paid[0]["type"] if paid else None,
        is_absence=bool(record.get("is_absence")),
        is_editable=record.get("is_editable", True),
    )


async def _routes(
    deps: Deps, hr: HRApi, slack_user_id: str
) -> tuple[list[RouteOption], int | None]:
    """Approval route candidates and the default choice (single option, else last used)."""
    raw = await hr.list_attendance_routes()
    routes = [RouteOption(id=r["id"], name=r.get("name") or f"経路 {r['id']}") for r in raw]
    if len(routes) == 1:
        return routes, routes[0].id
    last = await deps.repo.get_pref(slack_user_id, ROUTE_PREF_KEY)
    if last and any(str(r.id) == last for r in routes):
        return routes, int(last)
    return routes, None


async def draft_work_records(
    deps: Deps,
    *,
    slack_user_id: str,
    entries: list[WorkRecordEntry],
    today: date,
    channel_id: str,
    thread_ts: str,
) -> Proposal:
    validate_work_entries(entries, today=today, max_days=deps.max_work_record_days)
    hr = await hr_for(deps, slack_user_id)
    records = await asyncio.gather(*(hr.get_work_record(e.date) for e in entries))
    for entry, record in zip(entries, records, strict=True):
        entry.existing = parse_existing(entry.date, record)
    proposal = WorkRecordProposal(entries=sorted(entries, key=lambda e: e.date))
    return await deps.repo.create_proposal(
        slack_user_id=slack_user_id,
        kind=proposal.kind,
        payload=proposal.model_dump(mode="json"),
        channel_id=channel_id,
        thread_ts=thread_ts,
        ttl=deps.ttl,
    )


async def draft_paid_leave(
    deps: Deps,
    *,
    slack_user_id: str,
    day: date,
    leave_type: str,
    comment: str | None,
    channel_id: str,
    thread_ts: str,
) -> Proposal:
    hr = await hr_for(deps, slack_user_id)
    routes, route_id = await _routes(deps, hr, slack_user_id)
    if not routes:
        raise ValueError("利用できる勤怠申請の承認経路がありません。freee の設定を確認してください")
    proposal = PaidLeaveProposal(
        date=day, leave_type=leave_type, comment=comment, routes=routes, route_id=route_id
    )
    return await deps.repo.create_proposal(
        slack_user_id=slack_user_id,
        kind=proposal.kind,
        payload=proposal.model_dump(mode="json"),
        channel_id=channel_id,
        thread_ts=thread_ts,
        ttl=deps.ttl,
    )


async def draft_overtime(
    deps: Deps,
    *,
    slack_user_id: str,
    day: date,
    start: str,
    end: str,
    comment: str,
    channel_id: str,
    thread_ts: str,
) -> Proposal:
    hr = await hr_for(deps, slack_user_id)
    setting, (routes, route_id) = await asyncio.gather(
        hr.overtime_setting(day), _routes(deps, hr, slack_user_id)
    )
    reflect = bool(setting.get("should_reflect_in_work_record"))
    if reflect:
        # When requests are reflected in the work record, freee requires overtime to start
        # exactly at the scheduled clock-out time.
        scheduled_end = from_freee_datetime(day, setting.get("end_at"))
        if scheduled_end:
            start = scheduled_end
    validate_overtime_range(start, end)
    if not routes:
        raise ValueError("利用できる勤怠申請の承認経路がありません。freee の設定を確認してください")
    proposal = OvertimeProposal(
        date=day,
        start=start,
        end=end,
        comment=comment,
        reflect_in_work_record=reflect,
        routes=routes,
        route_id=route_id,
    )
    return await deps.repo.create_proposal(
        slack_user_id=slack_user_id,
        kind=proposal.kind,
        payload=proposal.model_dump(mode="json"),
        channel_id=channel_id,
        thread_ts=thread_ts,
        ttl=deps.ttl,
    )
