"""Agent tools called directly (as ADK would), with freee mocked."""

import json
from datetime import date
from types import SimpleNamespace

import httpx
import pytest
import respx

from freee_hr_bot.agent.tools import PROPOSAL_KEY, build_tools
from tests.conftest import API, EMPLOYEE_ID, USER

TODAY = date(2026, 9, 29)


@pytest.fixture
def tools(deps):
    return {t.__name__: t for t in build_tools(deps, lambda: TODAY)}


@pytest.fixture
def ctx():
    return SimpleNamespace(user_id=USER, state={"channel_id": "D1", "thread_ts": "1.0"})


def routes(*ids):
    return respx.get(f"{API}/approval_flow_routes").mock(
        return_value=httpx.Response(
            200, json={"approval_flow_routes": [{"id": i, "name": f"R{i}"} for i in ids]}
        )
    )


@respx.mock
async def test_get_work_records(tools, ctx):
    respx.get(f"{API}/employees/{EMPLOYEE_ID}/work_records/2026-09-28").mock(
        return_value=httpx.Response(
            200, json={"paid_holidays": [{"type": "full"}], "is_editable": True}
        )
    )
    res = await tools["get_work_records"](["2026-09-28"], ctx)
    assert res["status"] == "ok"
    assert res["records"]["2026-09-28"]["paid_holiday"] == "full"


@respx.mock
async def test_get_paid_leave_balance_uses_current_month(tools, ctx):
    route = respx.get(f"{API}/employees/{EMPLOYEE_ID}/work_record_summaries/2026/9").mock(
        return_value=httpx.Response(
            200,
            json={"num_paid_holidays_left": 12.5, "num_paid_holidays_and_hours_left": "12日4時間"},
        )
    )
    res = await tools["get_paid_leave_balance"](ctx)
    assert route.called
    assert res == {"status": "ok", "days_left": 12.5, "days_and_hours_left": "12日4時間"}


async def test_tool_errors_are_returned_not_raised(tools, ctx):
    res = await tools["get_work_records"](["not-a-date"], ctx)
    assert res["status"] == "error"
    unlinked = SimpleNamespace(user_id="U_NONE", state=ctx.state)
    res = await tools["get_paid_leave_balance"](unlinked)
    assert res == {"status": "error", "message": "freee と未連携です"}


@respx.mock
async def test_freee_error_message_reaches_model(tools, ctx):
    respx.get(f"{API}/employees/{EMPLOYEE_ID}/work_records/2026-09-28").mock(
        return_value=httpx.Response(403, json={"message": "権限がありません"})
    )
    res = await tools["get_work_records"](["2026-09-28"], ctx)
    assert res == {"status": "error", "message": "権限がありません"}


@respx.mock
async def test_propose_paid_leave(tools, ctx, deps):
    routes(5)
    res = await tools["propose_paid_leave"]("2026-10-02", "morning", ctx, comment="通院")
    assert res["status"] == "drafted"
    row = await deps.repo.get_proposal(res[PROPOSAL_KEY])
    assert (row.payload["leave_type"], row.payload["route_id"], row.channel_id) == (
        "morning",
        5,
        "D1",
    )


@respx.mock
async def test_propose_paid_leave_without_routes(tools, ctx):
    routes()
    res = await tools["propose_paid_leave"]("2026-10-02", "full", ctx)
    assert res["status"] == "error"
    assert "承認経路" in res["message"]


@respx.mock
async def test_propose_overtime_rejects_overnight(tools, ctx):
    routes(5)
    respx.get(f"{API}/approval_requests/overtime_works/setting").mock(
        return_value=httpx.Response(200, json={"should_reflect_in_work_record": False})
    )
    res = await tools["propose_overtime"]("2026-09-29", "18:00", "25:00", "対応", ctx)
    assert res["status"] == "error"


@respx.mock
@pytest.mark.parametrize(
    "reflect,start_key,end_key",
    [
        (True, "overtime_work_start_at", "overtime_work_end_at"),
        (False, "start_at", "end_at"),
    ],
)
async def test_overtime_execution_uses_setting_specific_fields(deps, reflect, start_key, end_key):
    from freee_hr_bot.proposals.executor import execute
    from freee_hr_bot.proposals.models import OvertimeProposal, RouteOption

    post = respx.post(f"{API}/approval_requests/overtime_works").mock(
        return_value=httpx.Response(201, json={"overtime_work": {"id": 1}})
    )
    payload = OvertimeProposal(
        date=TODAY,
        start="18:00",
        end="21:00",
        comment="リリース",
        reflect_in_work_record=reflect,
        routes=[RouteOption(id=5, name="A")],
        route_id=5,
    ).model_dump(mode="json")
    result = await execute(deps, USER, payload)
    assert result.ok and "残業申請" in result.lines[0]
    body = json.loads(post.calls[0].request.content)
    assert (body[start_key], body[end_key]) == ("18:00", "21:00")
    other = {"start_at", "end_at", "overtime_work_start_at", "overtime_work_end_at"} - {
        start_key,
        end_key,
    }
    assert not other & body.keys()
