import json
from datetime import date

from freee_hr_bot.proposals.models import (
    ExistingRecord,
    PaidLeaveProposal,
    RouteOption,
    TimeRange,
    WorkRecordEntry,
    WorkRecordProposal,
)
from freee_hr_bot.slack import blocks as B


def test_work_card_marks_overwrite_and_locked_days():
    p = WorkRecordProposal(
        entries=[
            WorkRecordEntry(
                date=date(2026, 9, 28),
                clock_in="09:00",
                clock_out="19:00",
                existing=ExistingRecord(segments=[TimeRange(start="09:00", end="18:00")]),
            ),
            WorkRecordEntry(
                date=date(2026, 9, 29),
                clock_in="09:00",
                clock_out="19:00",
                existing=ExistingRecord(is_editable=False),
            ),
        ]
    )
    text = json.dumps(B.proposal_card("pid", p, 15), ensure_ascii=False)
    assert "09/28(月)* ⚠️ *上書き*" in text
    assert "09/29(火)* 🚫" in text
    assert B.A_CONFIRM in text


def test_confirm_hidden_when_nothing_registrable():
    p = WorkRecordProposal(
        entries=[
            WorkRecordEntry(
                date=date(2026, 9, 29),
                clock_in="09:00",
                clock_out="19:00",
                existing=ExistingRecord(is_editable=False),
            )
        ]
    )
    assert B.A_CONFIRM not in json.dumps(B.proposal_card("pid", p, 15))


def test_leave_card_preselects_route():
    p = PaidLeaveProposal(
        date=date(2026, 10, 2),
        leave_type="morning",
        routes=[RouteOption(id=5, name="A"), RouteOption(id=6, name="B")],
        route_id=6,
    )
    card = B.proposal_card("pid", p, 15)
    select = next(
        b["accessory"] for b in card if b.get("accessory", {}).get("action_id") == B.A_ROUTE
    )
    assert select["initial_option"]["value"] == "6"


def test_finished_card_drops_controls():
    p = PaidLeaveProposal(
        date=date(2026, 10, 2), leave_type="full", routes=[RouteOption(id=5, name="A")], route_id=5
    )
    done = B.finished_card(B.proposal_card("pid", p, 15), ["✅ done"])
    kinds = [b["type"] for b in done]
    assert "actions" not in kinds and "context" not in kinds
    assert all(b.get("accessory", {}).get("action_id") != B.A_ROUTE for b in done)
