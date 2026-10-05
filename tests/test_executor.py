import json
from datetime import date

import httpx
import pytest
import respx

from freee_hr_bot.proposals.executor import MissingRouteError, execute, overtime_suggestion
from freee_hr_bot.proposals.models import (
    ExistingRecord,
    PaidLeaveProposal,
    RouteOption,
    TimeRange,
    WorkRecordEntry,
    WorkRecordProposal,
)
from freee_hr_bot.proposals.service import ROUTE_PREF_KEY
from tests.conftest import API, COMPANY_ID, EMPLOYEE_ID, USER

D = date(2026, 9, 29)
SCHEDULED = {
    "normal_work_clock_out_at": "2026-09-29T18:00:00.000+09:00",
    "day_pattern": "normal_day",
}


def test_overtime_suggestion_when_past_scheduled_end():
    s = overtime_suggestion(D, "21:00", SCHEDULED)
    assert (s.start, s.end) == ("18:00", "21:00")


def test_no_overtime_suggestion_within_schedule_or_on_holiday():
    assert overtime_suggestion(D, "18:00", SCHEDULED) is None
    assert (
        overtime_suggestion(D, "21:00", {**SCHEDULED, "day_pattern": "prescribed_holiday"}) is None
    )
    assert overtime_suggestion(D, "21:00", {}) is None


def test_overtime_suggestion_capped_at_midnight():
    assert overtime_suggestion(D, "26:00", SCHEDULED).end == "23:59"


def work_payload(*entries: WorkRecordEntry) -> dict:
    return WorkRecordProposal(entries=list(entries)).model_dump(mode="json")


@respx.mock
async def test_work_records_put_with_freee_format(deps):
    put = respx.put(f"{API}/employees/{EMPLOYEE_ID}/work_records/2026-09-29").mock(
        return_value=httpx.Response(200, json=SCHEDULED)
    )
    entry = WorkRecordEntry(
        date=D, clock_in="09:00", clock_out="21:00", breaks=[TimeRange(start="12:00", end="13:00")]
    )
    result = await execute(deps, USER, work_payload(entry))
    assert result.ok
    body = json.loads(put.calls[0].request.content)
    assert body == {
        "company_id": COMPANY_ID,
        "work_record_segments": [
            {"clock_in_at": "2026-09-29 09:00:00", "clock_out_at": "2026-09-29 21:00:00"}
        ],
        "break_records": [
            {"clock_in_at": "2026-09-29 12:00:00", "clock_out_at": "2026-09-29 13:00:00"}
        ],
    }
    assert [(s.start, s.end) for s in result.overtime_suggestions] == [("18:00", "21:00")]


@respx.mock
async def test_work_records_partial_failure_and_locked_day(deps):
    respx.put(f"{API}/employees/{EMPLOYEE_ID}/work_records/2026-09-28").mock(
        return_value=httpx.Response(400, json={"message": "不正な時刻です"})
    )
    put_locked = respx.put(f"{API}/employees/{EMPLOYEE_ID}/work_records/2026-09-27")
    respx.put(f"{API}/employees/{EMPLOYEE_ID}/work_records/2026-09-29").mock(
        return_value=httpx.Response(200, json={})
    )
    result = await execute(
        deps,
        USER,
        work_payload(
            WorkRecordEntry(
                date=date(2026, 9, 27),
                clock_in="09:00",
                clock_out="18:00",
                existing=ExistingRecord(is_editable=False),
            ),
            WorkRecordEntry(date=date(2026, 9, 28), clock_in="09:00", clock_out="18:00"),
            WorkRecordEntry(date=D, clock_in="09:00", clock_out="18:00"),
        ),
    )
    assert not result.ok
    assert not put_locked.called
    assert result.lines[0].startswith("❌ 09/27")
    assert "不正な時刻です" in result.lines[1]
    assert result.lines[2].startswith("✅ 09/29")


def leave_payload(route_id):
    return PaidLeaveProposal(
        date=D, leave_type="afternoon", routes=[RouteOption(id=5, name="A")], route_id=route_id
    ).model_dump(mode="json")


async def test_paid_leave_requires_route(deps):
    with pytest.raises(MissingRouteError):
        await execute(deps, USER, leave_payload(None))


@respx.mock
async def test_paid_leave_submitted_and_route_remembered(deps):
    post = respx.post(f"{API}/approval_requests/paid_holidays").mock(
        return_value=httpx.Response(201, json={"paid_holiday": {"id": 1}})
    )
    result = await execute(deps, USER, leave_payload(5))
    assert result.ok
    body = json.loads(post.calls[0].request.content)
    assert body == {
        "company_id": COMPANY_ID,
        "target_date": "2026-09-29",
        "values": [{"type": "afternoon"}],
        "approval_flow_route_id": 5,
    }
    assert await deps.repo.get_pref(USER, ROUTE_PREF_KEY) == "5"
