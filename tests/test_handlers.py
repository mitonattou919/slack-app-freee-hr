"""Slack listener behaviour, driven directly with a fake Bolt app and a mocked Slack client."""

import json
from datetime import date, timedelta
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from freee_hr_bot.agent.runner import TurnResult
from freee_hr_bot.freee.oauth import TOKEN_URL
from freee_hr_bot.proposals.models import (
    OvertimeProposal,
    PaidLeaveProposal,
    RouteOption,
    WorkRecordEntry,
    WorkRecordProposal,
)
from freee_hr_bot.slack import blocks as B
from freee_hr_bot.slack.handlers import register
from tests.conftest import API, COMPANY_ID, EMPLOYEE_ID, USER

OTHER = "U999"
DM = "D1"
THREAD = "100.0"


class FakeApp:
    def __init__(self) -> None:
        self.listeners: dict[tuple[str, str], Any] = {}

    def _register(self, kind: str, key: str):
        def deco(fn):
            self.listeners[(kind, key)] = fn
            return fn

        return deco

    def event(self, name: str):
        return self._register("event", name)

    def action(self, action_id: str):
        return self._register("action", action_id)

    def view(self, callback_id: str):
        return self._register("view", callback_id)


@pytest.fixture
def slack():
    client = AsyncMock()
    client.chat_postMessage.return_value = {"ts": "200.0"}
    client.conversations_open.return_value = {"channel": {"id": DM}}
    return client


@pytest.fixture
def agent():
    runner = AsyncMock()
    runner.run_turn.return_value = TurnResult(text="了解")
    return runner


@pytest.fixture
def app(deps, oauth, agent):
    fake = FakeApp()
    register(fake, deps=deps, oauth=oauth, agent=agent)
    return fake


def listener(app: FakeApp, kind: str, key: str):
    return app.listeners[(kind, key)]


def dm_event(text: str, user: str = USER, **extra: Any) -> dict[str, Any]:
    return {"channel_type": "im", "user": user, "channel": DM, "ts": THREAD, "text": text, **extra}


def action_body(value: str, user: str = USER, blocks: list | None = None) -> dict[str, Any]:
    return {
        "user": {"id": user},
        "channel": {"id": DM},
        "trigger_id": "trig",
        "actions": [{"value": value}],
        "message": {"ts": "200.0", "blocks": blocks or []},
    }


async def make_proposal(deps, payload: dict, *, ttl: timedelta = timedelta(minutes=15)):
    row = await deps.repo.create_proposal(
        slack_user_id=USER,
        kind=payload["kind"],
        payload=payload,
        channel_id=DM,
        thread_ts=THREAD,
        ttl=ttl,
    )
    await deps.repo.update_proposal(row.id, message_ts="200.0")
    return row


def work_payload(clock_out: str = "18:00") -> dict:
    return WorkRecordProposal(
        entries=[WorkRecordEntry(date=date(2026, 9, 29), clock_in="09:00", clock_out=clock_out)]
    ).model_dump(mode="json")


def leave_payload(route_id: int | None) -> dict:
    return PaidLeaveProposal(
        date=date(2026, 10, 2),
        leave_type="full",
        routes=[RouteOption(id=5, name="A"), RouteOption(id=6, name="B")],
        route_id=route_id,
    ).model_dump(mode="json")


def texts(mock: AsyncMock) -> str:
    return json.dumps([c.kwargs for c in mock.call_args_list], ensure_ascii=False)


# --- DM messages --------------------------------------------------------------
@pytest.mark.parametrize(
    "event",
    [
        {**dm_event("hi"), "channel_type": "channel"},
        dm_event("hi", subtype="message_changed"),
        dm_event("hi", bot_id="B1"),
    ],
)
async def test_non_user_dm_messages_are_ignored(app, agent, event):
    say = AsyncMock()
    await listener(app, "event", "message")(event=event, client=AsyncMock(), say=say)
    say.assert_not_called()
    agent.run_turn.assert_not_called()


async def test_unlinked_user_gets_link_prompt(app, agent, slack):
    say = AsyncMock()
    await listener(app, "event", "message")(event=dm_event("hi", user=OTHER), client=slack, say=say)
    assert say.call_args.kwargs["blocks"] == B.link_prompt()
    agent.run_turn.assert_not_called()


async def test_unlink_removes_link(app, deps, slack):
    say = AsyncMock()
    await listener(app, "event", "message")(event=dm_event("連携解除"), client=slack, say=say)
    assert await deps.repo.get_link(USER) is None
    assert "解除しました" in say.call_args.kwargs["text"]


async def test_linked_user_message_runs_agent_and_posts_cards(app, deps, agent, slack):
    row = await make_proposal(deps, work_payload())
    agent.run_turn.return_value = TurnResult(text="確認カードを出しました", proposal_ids=[row.id])
    say = AsyncMock()
    await listener(app, "event", "message")(
        event=dm_event("今日 9-18", thread_ts="50.0"), client=slack, say=say
    )
    agent.run_turn.assert_awaited_once_with(
        user_id=USER, channel_id=DM, thread_ts="50.0", text="今日 9-18"
    )
    assert say.call_args.kwargs == {"text": "確認カードを出しました", "thread_ts": "50.0"}
    card = slack.chat_postMessage.call_args.kwargs
    assert card["thread_ts"] == THREAD
    assert any(b.get("block_id") == f"proposal:{row.id}" for b in card["blocks"])
    assert (await deps.repo.get_proposal(row.id)).message_ts == "200.0"


async def test_agent_failure_reports_error(app, agent, slack):
    agent.run_turn.side_effect = RuntimeError("boom")
    say = AsyncMock()
    await listener(app, "event", "message")(event=dm_event("x"), client=slack, say=say)
    assert "エラー" in say.call_args.kwargs["text"]


# --- linking --------------------------------------------------------------------
def link_view(code: str = "the-code") -> dict:
    return {"state": {"values": {"code": {"value": {"value": code}}}}}


@respx.mock
async def test_link_submit_stores_employee(app, deps, slack):
    await deps.repo.delete_link(USER)
    token = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "a", "refresh_token": "r", "expires_in": 21600}
        )
    )
    respx.get(f"{API}/users/me").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 7,
                "companies": [{"id": 999, "employee_id": 1}, {"id": COMPANY_ID, "employee_id": 55}],
            },
        )
    )
    ack = AsyncMock()
    await listener(app, "view", B.V_LINK_SUBMIT)(
        ack=ack, body={"user": {"id": USER}}, view=link_view(), client=slack
    )
    ack.assert_awaited_once()
    assert b"code=the-code" in token.calls[0].request.content
    link = await deps.repo.get_link(USER)
    assert (link.employee_id, link.freee_user_id) == (55, 7)
    assert "連携しました" in slack.chat_postMessage.call_args.kwargs["text"]


@respx.mock
async def test_link_rejected_when_not_employee_of_company(app, deps, slack):
    await deps.repo.delete_link(USER)
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "a", "refresh_token": "r", "expires_in": 21600}
        )
    )
    respx.get(f"{API}/users/me").mock(
        return_value=httpx.Response(
            200, json={"id": 7, "companies": [{"id": COMPANY_ID, "employee_id": None}]}
        )
    )
    await listener(app, "view", B.V_LINK_SUBMIT)(
        ack=AsyncMock(), body={"user": {"id": USER}}, view=link_view(), client=slack
    )
    assert await deps.repo.get_link(USER) is None
    assert "従業員として登録されていない" in slack.chat_postMessage.call_args.kwargs["text"]


@respx.mock
async def test_link_bad_code(app, deps, slack):
    await deps.repo.delete_link(USER)
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(401, json={"error": "invalid_grant"}))
    await listener(app, "view", B.V_LINK_SUBMIT)(
        ack=AsyncMock(), body={"user": {"id": USER}}, view=link_view(), client=slack
    )
    assert await deps.repo.get_link(USER) is None
    assert "失敗しました" in slack.chat_postMessage.call_args.kwargs["text"]


async def test_link_start_opens_modal_with_oob_url(app, slack):
    await listener(app, "action", B.A_LINK_START)(
        ack=AsyncMock(), body=action_body(""), client=slack
    )
    view = slack.views_open.call_args.kwargs["view"]
    assert view["callback_id"] == B.V_LINK_SUBMIT
    assert "urn%3Aietf%3Awg%3Aoauth%3A2.0%3Aoob" in json.dumps(view)


# --- confirm / cancel --------------------------------------------------------------
@respx.mock
async def test_confirm_executes_once_and_updates_card(app, deps, slack):
    put = respx.put(f"{API}/employees/{EMPLOYEE_ID}/work_records/2026-09-29").mock(
        return_value=httpx.Response(200, json={})
    )
    row = await make_proposal(deps, work_payload())
    confirm = listener(app, "action", B.A_CONFIRM)
    await confirm(ack=AsyncMock(), body=action_body(row.id), client=slack)
    await confirm(ack=AsyncMock(), body=action_body(row.id), client=slack)  # double click
    assert put.call_count == 1
    assert (await deps.repo.get_proposal(row.id)).status == "done"
    assert "✅ 09/29" in slack.chat_update.call_args.kwargs["text"]
    assert "既に処理済み" in slack.chat_postEphemeral.call_args.kwargs["text"]


@respx.mock
async def test_confirm_by_other_user_is_rejected(app, deps, slack):
    put = respx.put(f"{API}/employees/{EMPLOYEE_ID}/work_records/2026-09-29")
    row = await make_proposal(deps, work_payload())
    await listener(app, "action", B.A_CONFIRM)(
        ack=AsyncMock(), body=action_body(row.id, user=OTHER), client=slack
    )
    assert not put.called
    assert (await deps.repo.get_proposal(row.id)).status == "pending"
    assert slack.chat_postEphemeral.call_args.kwargs["user"] == OTHER


@respx.mock
async def test_confirm_after_expiry_is_rejected(app, deps, slack):
    put = respx.put(f"{API}/employees/{EMPLOYEE_ID}/work_records/2026-09-29")
    row = await make_proposal(deps, work_payload(), ttl=timedelta(seconds=-1))
    await listener(app, "action", B.A_CONFIRM)(
        ack=AsyncMock(), body=action_body(row.id), client=slack
    )
    assert not put.called
    assert (await deps.repo.get_proposal(row.id)).status == "expired"
    assert "有効期限切れ" in slack.chat_postEphemeral.call_args.kwargs["text"]
    assert "⌛" in slack.chat_update.call_args.kwargs["text"]


async def test_confirm_without_route_keeps_pending(app, deps, slack):
    row = await make_proposal(deps, leave_payload(None))
    await listener(app, "action", B.A_CONFIRM)(
        ack=AsyncMock(), body=action_body(row.id), client=slack
    )
    assert (await deps.repo.get_proposal(row.id)).status == "pending"
    assert "承認経路を選択" in slack.chat_postEphemeral.call_args.kwargs["text"]


@respx.mock
async def test_confirm_freee_error_marks_failed(app, deps, slack):
    respx.post(f"{API}/approval_requests/paid_holidays").mock(
        return_value=httpx.Response(400, json={"message": "有給残日数が不足しています"})
    )
    row = await make_proposal(deps, leave_payload(5))
    await listener(app, "action", B.A_CONFIRM)(
        ack=AsyncMock(), body=action_body(row.id), client=slack
    )
    assert (await deps.repo.get_proposal(row.id)).status == "failed"
    assert "有給残日数が不足しています" in slack.chat_update.call_args.kwargs["text"]


@respx.mock
async def test_confirm_suggests_overtime_when_past_schedule(app, deps, slack):
    respx.put(f"{API}/employees/{EMPLOYEE_ID}/work_records/2026-09-29").mock(
        return_value=httpx.Response(
            200,
            json={
                "normal_work_clock_out_at": "2026-09-29T18:00:00.000+09:00",
                "day_pattern": "normal_day",
            },
        )
    )
    row = await make_proposal(deps, work_payload(clock_out="21:00"))
    await listener(app, "action", B.A_CONFIRM)(
        ack=AsyncMock(), body=action_body(row.id), client=slack
    )
    posted = slack.chat_postMessage.call_args.kwargs
    assert posted["thread_ts"] == THREAD
    button = next(b["accessory"] for b in posted["blocks"] if "accessory" in b)
    assert json.loads(button["value"]) == {
        "date": "2026-09-29",
        "start": "18:00",
        "end": "21:00",
        "thread_ts": THREAD,
    }


async def test_cancel(app, deps, slack):
    row = await make_proposal(deps, work_payload())
    await listener(app, "action", B.A_CANCEL)(
        ack=AsyncMock(), body=action_body(row.id), client=slack
    )
    assert (await deps.repo.get_proposal(row.id)).status == "cancelled"
    assert "キャンセル" in slack.chat_update.call_args.kwargs["text"]


async def test_cancel_by_other_user_is_rejected(app, deps, slack):
    row = await make_proposal(deps, work_payload())
    await listener(app, "action", B.A_CANCEL)(
        ack=AsyncMock(), body=action_body(row.id, user=OTHER), client=slack
    )
    assert (await deps.repo.get_proposal(row.id)).status == "pending"


# --- route select ------------------------------------------------------------------
def route_body(proposal_id: str, value: str, user: str = USER) -> dict:
    body = action_body(
        "", user=user, blocks=[{"type": "actions", "block_id": f"proposal:{proposal_id}"}]
    )
    body["actions"] = [{"selected_option": {"value": value}}]
    return body


async def test_route_select_updates_payload(app, deps, slack):
    row = await make_proposal(deps, leave_payload(None))
    await listener(app, "action", B.A_ROUTE)(
        ack=AsyncMock(), body=route_body(row.id, "6"), client=slack
    )
    assert (await deps.repo.get_proposal(row.id)).payload["route_id"] == 6


@pytest.mark.parametrize("value,user", [("77", USER), ("6", OTHER)])
async def test_route_select_rejects_unknown_route_or_other_user(app, deps, slack, value, user):
    row = await make_proposal(deps, leave_payload(5))
    await listener(app, "action", B.A_ROUTE)(
        ack=AsyncMock(), body=route_body(row.id, value, user), client=slack
    )
    assert (await deps.repo.get_proposal(row.id)).payload["route_id"] == 5


# --- overtime follow-up ------------------------------------------------------------
async def test_overtime_suggest_opens_reason_modal(app, slack):
    value = json.dumps(
        {"date": "2026-09-29", "start": "18:00", "end": "21:00", "thread_ts": THREAD}
    )
    await listener(app, "action", B.A_OVERTIME_SUGGEST)(
        ack=AsyncMock(), body=action_body(value), client=slack
    )
    view = slack.views_open.call_args.kwargs["view"]
    assert json.loads(view["private_metadata"])["channel_id"] == DM


@respx.mock
async def test_overtime_reason_drafts_and_posts_card(app, deps, slack):
    respx.get(f"{API}/approval_flow_routes").mock(
        return_value=httpx.Response(200, json={"approval_flow_routes": [{"id": 5, "name": "標準"}]})
    )
    respx.get(f"{API}/approval_requests/overtime_works/setting").mock(
        return_value=httpx.Response(200, json={"should_reflect_in_work_record": False})
    )
    meta = {
        "date": "2026-09-29",
        "start": "18:00",
        "end": "21:00",
        "thread_ts": THREAD,
        "channel_id": DM,
    }
    view = {
        "private_metadata": json.dumps(meta),
        "state": {"values": {"reason": {"value": {"value": "リリース対応"}}}},
    }
    await listener(app, "view", B.V_OVERTIME_REASON)(
        ack=AsyncMock(), body={"user": {"id": USER}}, view=view, client=slack
    )
    card = slack.chat_postMessage.call_args.kwargs
    assert card["thread_ts"] == THREAD
    assert "残業申請の確認" in json.dumps(card["blocks"], ensure_ascii=False)
    proposal_id = next(
        b["block_id"] for b in card["blocks"] if b.get("block_id", "").startswith("proposal:")
    )
    row = await deps.repo.get_proposal(proposal_id.split(":", 1)[1])
    assert OvertimeProposal.model_validate(row.payload).comment == "リリース対応"


@respx.mock
async def test_concurrent_confirms_write_once(app, deps, slack):
    import asyncio

    put = respx.put(f"{API}/employees/{EMPLOYEE_ID}/work_records/2026-09-29").mock(
        return_value=httpx.Response(200, json={})
    )
    row = await make_proposal(deps, work_payload())
    confirm = listener(app, "action", B.A_CONFIRM)
    await asyncio.gather(
        *(confirm(ack=AsyncMock(), body=action_body(row.id), client=slack) for _ in range(5))
    )
    assert put.call_count == 1
