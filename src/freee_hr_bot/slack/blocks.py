import json
from datetime import date
from typing import Any

from freee_hr_bot.proposals.models import (
    LEAVE_LABELS,
    AnyProposal,
    ExistingRecord,
    OvertimeProposal,
    PaidLeaveProposal,
    RouteOption,
    TimeRange,
    WorkRecordProposal,
)

WEEKDAYS = "月火水木金土日"

A_LINK_START = "link_start"
V_LINK_SUBMIT = "link_submit"
A_CONFIRM = "proposal_confirm"
A_CANCEL = "proposal_cancel"
A_ROUTE = "route_select"
A_OVERTIME_SUGGEST = "overtime_suggest"
V_OVERTIME_REASON = "overtime_reason"

Blocks = list[dict[str, Any]]


def _section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _context(text: str) -> dict[str, Any]:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _day(d: date) -> str:
    return f"{d.strftime('%m/%d')}({WEEKDAYS[d.weekday()]})"


def _ranges(ranges: list[TimeRange]) -> str:
    return ", ".join(f"{r.start}〜{r.end}" for r in ranges) or "なし"


def describe_existing(ex: ExistingRecord) -> str:
    if ex.is_empty:
        return "未登録"
    parts = []
    if ex.segments:
        parts.append(f"勤務 {_ranges(ex.segments)} / 休憩 {_ranges(ex.breaks)}")
    if ex.paid_holiday:
        parts.append(f"有給({ex.paid_holiday})")
    if ex.is_absence:
        parts.append("欠勤")
    return " / ".join(parts)


# --- linking ----------------------------------------------------------------
def link_prompt() -> Blocks:
    return [
        _section(
            "freee人事労務 とまだ連携されていません。連携すると、この DM で勤怠登録や申請ができます。"
        ),
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": A_LINK_START,
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "freee と連携する"},
                }
            ],
        },
    ]


def link_modal(authorize_url: str) -> dict[str, Any]:
    return {
        "type": "modal",
        "callback_id": V_LINK_SUBMIT,
        "title": {"type": "plain_text", "text": "freee と連携"},
        "submit": {"type": "plain_text", "text": "連携する"},
        "close": {"type": "plain_text", "text": "キャンセル"},
        "blocks": [
            _section(
                f"1. <{authorize_url}|こちらから freee にログインして許可>してください\n"
                "2. 画面に表示された *認可コード* をコピーして下に貼り付けてください"
            ),
            {
                "type": "input",
                "block_id": "code",
                "label": {"type": "plain_text", "text": "認可コード"},
                "element": {"type": "plain_text_input", "action_id": "value"},
            },
        ],
    }


# --- proposal cards ---------------------------------------------------------
def _route_select(routes: list[RouteOption], selected: int | None) -> dict[str, Any]:
    def opt(r: RouteOption) -> dict[str, Any]:
        return {"text": {"type": "plain_text", "text": r.name[:75]}, "value": str(r.id)}

    element: dict[str, Any] = {
        "type": "static_select",
        "action_id": A_ROUTE,
        "placeholder": {"type": "plain_text", "text": "承認経路を選択"},
        "options": [opt(r) for r in routes[:100]],
    }
    chosen = next((r for r in routes if r.id == selected), None)
    if chosen:
        element["initial_option"] = opt(chosen)
    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": "*承認経路*"},
        "accessory": element,
    }


def _buttons(proposal_id: str, confirm_label: str, disabled: bool = False) -> dict[str, Any]:
    elements: list[dict[str, Any]] = []
    if not disabled:
        elements.append(
            {
                "type": "button",
                "action_id": A_CONFIRM,
                "style": "primary",
                "value": proposal_id,
                "text": {"type": "plain_text", "text": confirm_label},
            }
        )
    elements.append(
        {
            "type": "button",
            "action_id": A_CANCEL,
            "value": proposal_id,
            "text": {"type": "plain_text", "text": "キャンセル"},
        }
    )
    return {"type": "actions", "block_id": f"proposal:{proposal_id}", "elements": elements}


def proposal_card(proposal_id: str, p: AnyProposal, ttl_minutes: int) -> Blocks:
    blocks: Blocks
    if isinstance(p, WorkRecordProposal):
        blocks = [_section("*勤怠登録の確認*")]
        registrable = 0
        for e in p.entries:
            new = f"{e.clock_in}〜{e.clock_out} / 休憩 {_ranges(e.breaks)}"
            ex = e.existing or ExistingRecord()
            if not ex.is_editable:
                blocks.append(
                    _section(
                        f"*{_day(e.date)}* 🚫 締め済みなどで編集できません\n現在: {describe_existing(ex)}"
                    )
                )
                continue
            registrable += 1
            badge = "" if ex.is_empty else " ⚠️ *上書き*"
            blocks.append(
                _section(f"*{_day(e.date)}*{badge}\n現在: {describe_existing(ex)}\n→ 新規: {new}")
            )
        blocks.append(_buttons(proposal_id, "登録する", disabled=registrable == 0))
    elif isinstance(p, PaidLeaveProposal):
        text = f"*有給休暇申請の確認*\n日付: {_day(p.date)}\n区分: {LEAVE_LABELS[p.leave_type]}"
        if p.comment:
            text += f"\n理由: {p.comment}"
        blocks = [
            _section(text),
            _route_select(p.routes, p.route_id),
            _buttons(proposal_id, "申請する"),
        ]
    elif isinstance(p, OvertimeProposal):
        text = (
            f"*残業申請の確認*\n日付: {_day(p.date)}\n時間: {p.start}〜{p.end}\n理由: {p.comment}"
        )
        if p.reflect_in_work_record:
            text += "\n_承認後に勤怠へ反映されます(開始は所定退勤時刻になります)_"
        blocks = [
            _section(text),
            _route_select(p.routes, p.route_id),
            _buttons(proposal_id, "申請する"),
        ]
    else:
        raise TypeError(p)
    blocks.append(_context(f"この確認は {ttl_minutes} 分で無効になります。"))
    return blocks


def finished_card(original: Blocks, status_lines: list[str]) -> Blocks:
    """Replace the action/context blocks of a card with the outcome."""
    kept = [
        b
        for b in original
        if b.get("type") not in ("actions", "context")
        and b.get("accessory", {}).get("action_id") != A_ROUTE
    ]
    return [*kept, _section("\n".join(status_lines))]


def overtime_suggestion_blocks(suggestions: list[dict[str, str]]) -> Blocks:
    blocks: Blocks = [_section("所定の退勤時刻を超えている日があります。残業申請も出しますか?")]
    for s in suggestions:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"{s['date']} {s['start']}〜{s['end']}"},
                "accessory": {
                    "type": "button",
                    "action_id": A_OVERTIME_SUGGEST,
                    "value": json.dumps(s),
                    "text": {"type": "plain_text", "text": "残業申請を作る"},
                },
            }
        )
    return blocks


def overtime_reason_modal(meta: dict[str, str]) -> dict[str, Any]:
    return {
        "type": "modal",
        "callback_id": V_OVERTIME_REASON,
        "private_metadata": json.dumps(meta),
        "title": {"type": "plain_text", "text": "残業申請"},
        "submit": {"type": "plain_text", "text": "確認へ"},
        "blocks": [
            _section(f"{meta['date']} {meta['start']}〜{meta['end']}"),
            {
                "type": "input",
                "block_id": "reason",
                "label": {"type": "plain_text", "text": "申請理由"},
                "element": {"type": "plain_text_input", "action_id": "value", "multiline": True},
            },
        ],
    }
