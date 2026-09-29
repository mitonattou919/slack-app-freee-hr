from datetime import date

import httpx
import pytest
import respx

from freee_hr_bot.proposals import service
from freee_hr_bot.proposals.models import TimeRange, WorkRecordEntry, load_proposal
from freee_hr_bot.proposals.validate import ValidationError
from tests.conftest import API, EMPLOYEE_ID, USER

TODAY = date(2026, 9, 29)
ROUTES = f"{API}/approval_flow_routes"


def wr_url(d: str) -> str:
    return f"{API}/employees/{EMPLOYEE_ID}/work_records/{d}"


@respx.mock
async def test_draft_work_records_captures_existing_state(deps):
    respx.get(wr_url("2026-09-28")).mock(
        return_value=httpx.Response(
            200,
            json={
                "work_record_segments": [
                    {
                        "clock_in_at": "2026-09-28T09:00:00.000+09:00",
                        "clock_out_at": "2026-09-28T18:00:00.000+09:00",
                    }
                ],
                "break_records": [],
                "is_editable": True,
            },
        )
    )
    respx.get(wr_url("2026-09-29")).mock(
        return_value=httpx.Response(
            200,
            json={
                "work_record_segments": [],
                "break_records": [],
                "is_editable": False,
            },
        )
    )
    entries = [
        WorkRecordEntry(date=date(2026, 9, 29), clock_in="09:00", clock_out="19:00"),
        WorkRecordEntry(
            date=date(2026, 9, 28),
            clock_in="09:00",
            clock_out="19:00",
            breaks=[TimeRange(start="12:00", end="13:00")],
        ),
    ]
    row = await service.draft_work_records(
        deps, slack_user_id=USER, entries=entries, today=TODAY, channel_id="D1", thread_ts="1.0"
    )
    p = load_proposal(row.payload)
    assert [e.date for e in p.entries] == [date(2026, 9, 28), date(2026, 9, 29)]  # sorted
    assert p.entries[0].existing.segments == [TimeRange(start="09:00", end="18:00")]
    assert not p.entries[0].existing.is_empty
    assert p.entries[1].existing.is_editable is False
    assert row.status == "pending" and row.slack_user_id == USER


async def test_draft_work_records_validates_before_calling_freee(deps):
    with pytest.raises(ValidationError):
        await service.draft_work_records(
            deps,
            slack_user_id=USER,
            today=TODAY,
            channel_id="D1",
            thread_ts="1.0",
            entries=[WorkRecordEntry(date=date(2026, 10, 1), clock_in="09:00", clock_out="18:00")],
        )


@respx.mock
async def test_single_route_is_auto_selected(deps):
    respx.get(ROUTES).mock(
        return_value=httpx.Response(200, json={"approval_flow_routes": [{"id": 5, "name": "標準"}]})
    )
    row = await service.draft_paid_leave(
        deps,
        slack_user_id=USER,
        day=date(2026, 10, 2),
        leave_type="morning",
        comment=None,
        channel_id="D1",
        thread_ts="1.0",
    )
    assert row.payload["route_id"] == 5


@respx.mock
async def test_multiple_routes_default_to_last_used(deps):
    respx.get(ROUTES).mock(
        return_value=httpx.Response(
            200, json={"approval_flow_routes": [{"id": 5, "name": "A"}, {"id": 6, "name": "B"}]}
        )
    )
    kwargs = dict(
        slack_user_id=USER,
        day=date(2026, 10, 2),
        leave_type="full",
        comment=None,
        channel_id="D1",
        thread_ts="1.0",
    )
    row = await service.draft_paid_leave(deps, **kwargs)
    assert row.payload["route_id"] is None  # user must choose on the card
    await deps.repo.set_pref(USER, service.ROUTE_PREF_KEY, "6")
    row = await service.draft_paid_leave(deps, **kwargs)
    assert row.payload["route_id"] == 6


@respx.mock
async def test_overtime_start_pinned_to_scheduled_end_when_reflected(deps):
    respx.get(ROUTES).mock(
        return_value=httpx.Response(200, json={"approval_flow_routes": [{"id": 5, "name": "標準"}]})
    )
    respx.get(f"{API}/approval_requests/overtime_works/setting").mock(
        return_value=httpx.Response(
            200,
            json={
                "should_reflect_in_work_record": True,
                "start_at": "2026-09-29T09:00:00.000+09:00",
                "end_at": "2026-09-29T18:00:00.000+09:00",
            },
        )
    )
    row = await service.draft_overtime(
        deps,
        slack_user_id=USER,
        day=date(2026, 9, 29),
        start="17:30",
        end="21:00",
        comment="リリース対応",
        channel_id="D1",
        thread_ts="1.0",
    )
    assert row.payload["start"] == "18:00"
    assert row.payload["reflect_in_work_record"] is True
