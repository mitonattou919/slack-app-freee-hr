"""Pure validation/conversion helpers. Everything the LLM proposes passes through here."""

import re
from datetime import date, datetime, time, timedelta

from freee_hr_bot.proposals.models import TimeRange, WorkRecordEntry

_HHMM = re.compile(r"^(\d{1,2}):(\d{2})$")
MAX_SHIFT_MINUTES = 24 * 60


class ValidationError(ValueError):
    pass


def to_minutes(hhmm: str, *, allow_overnight: bool = True) -> int:
    m = _HHMM.match(hhmm.strip())
    if not m:
        raise ValidationError(f"時刻の形式が不正です: {hhmm!r}(HH:MM で指定)")
    h, mi = int(m.group(1)), int(m.group(2))
    limit = 48 if allow_overnight else 24
    if mi >= 60 or h >= limit:
        raise ValidationError(f"時刻の範囲が不正です: {hhmm!r}")
    return h * 60 + mi


def fmt_minutes(mins: int) -> str:
    return f"{mins // 60:02d}:{mins % 60:02d}"


def to_freee_datetime(day: date, hhmm: str) -> str:
    """'25:30' on 9/29 becomes '2026-09-30 01:30:00'."""
    mins = to_minutes(hhmm)
    dt = datetime.combine(day, time()) + timedelta(minutes=mins)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def from_freee_datetime(day: date, value: str | None) -> str | None:
    """Inverse of to_freee_datetime; accepts ISO strings with or without offsets."""
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace(" ", "T"))
    delta = datetime.combine(dt.date(), dt.time()) - datetime.combine(day, time())
    return fmt_minutes(int(delta.total_seconds() // 60))


def validate_work_entry(entry: WorkRecordEntry, *, today: date) -> None:
    if entry.date > today:
        raise ValidationError(f"{entry.date} は未来の日付なので勤怠登録できません")
    start = to_minutes(entry.clock_in)
    end = to_minutes(entry.clock_out)
    if start >= 24 * 60:
        raise ValidationError(f"{entry.date}: 出勤時刻は 23:59 までで指定してください")
    if end <= start:
        raise ValidationError(
            f"{entry.date}: 退勤({entry.clock_out})が出勤({entry.clock_in})以前です"
        )
    if end - start > MAX_SHIFT_MINUTES:
        raise ValidationError(f"{entry.date}: 勤務時間が24時間を超えています")
    ranges = sorted((to_minutes(b.start), to_minutes(b.end)) for b in entry.breaks)
    prev_end = start
    for b_start, b_end in ranges:
        if b_end <= b_start:
            raise ValidationError(f"{entry.date}: 休憩の終了が開始以前です")
        if b_start < prev_end or b_end > end:
            raise ValidationError(f"{entry.date}: 休憩が勤務時間外、または休憩同士が重なっています")
        prev_end = b_end


def validate_work_entries(entries: list[WorkRecordEntry], *, today: date, max_days: int) -> None:
    if not entries:
        raise ValidationError("登録する日がありません")
    if len(entries) > max_days:
        raise ValidationError(f"一度に登録できるのは {max_days} 日までです({len(entries)} 日指定)")
    days = [e.date for e in entries]
    if len(set(days)) != len(days):
        raise ValidationError("同じ日付が複数回指定されています")
    for e in entries:
        validate_work_entry(e, today=today)


def validate_overtime_range(start: str, end: str) -> None:
    # The overtime request API only accepts same-day HH:MM values.
    s = to_minutes(start, allow_overnight=False)
    e = to_minutes(end, allow_overnight=False)
    if e <= s:
        raise ValidationError(f"残業の終了({end})が開始({start})以前です")


def break_payload(day: date, breaks: list[TimeRange]) -> list[dict[str, str]]:
    return [
        {
            "clock_in_at": to_freee_datetime(day, b.start),
            "clock_out_at": to_freee_datetime(day, b.end),
        }
        for b in breaks
    ]
