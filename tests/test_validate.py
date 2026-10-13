from datetime import date

import pytest

from freee_hr_bot.proposals.models import ExistingRecord, TimeRange, WorkRecordEntry
from freee_hr_bot.proposals.validate import (
    ValidationError,
    from_freee_datetime,
    resolve_breaks,
    to_freee_datetime,
    to_minutes,
    validate_overtime_range,
    validate_work_entries,
)

TODAY = date(2026, 9, 29)
DEFAULT_BREAK = TimeRange(start="12:00", end="13:00")


def entry(d=TODAY, cin="09:00", cout="18:00", breaks=(("12:00", "13:00"),)):
    return WorkRecordEntry(
        date=d,
        clock_in=cin,
        clock_out=cout,
        breaks=[TimeRange(start=s, end=e) for s, e in breaks],
    )


def test_to_minutes_accepts_overnight_notation():
    assert to_minutes("9:05") == 545
    assert to_minutes("25:30") == 25 * 60 + 30


@pytest.mark.parametrize("bad", ["9", "09:60", "48:00", "aa:bb"])
def test_to_minutes_rejects_bad_values(bad):
    with pytest.raises(ValidationError):
        to_minutes(bad)


def test_to_minutes_same_day_only():
    with pytest.raises(ValidationError):
        to_minutes("24:00", allow_overnight=False)


def test_freee_datetime_round_trip_across_midnight():
    assert to_freee_datetime(TODAY, "25:30") == "2026-09-30 01:30:00"
    assert from_freee_datetime(TODAY, "2026-09-30T01:30:00.000+09:00") == "25:30"
    assert from_freee_datetime(TODAY, "2026-09-29 09:00:00") == "09:00"
    assert from_freee_datetime(TODAY, None) is None


def test_valid_entries_pass():
    validate_work_entries([entry(), entry(date(2026, 9, 28))], today=TODAY, max_days=7)


def test_future_date_rejected():
    with pytest.raises(ValidationError, match="未来"):
        validate_work_entries([entry(date(2026, 9, 30))], today=TODAY, max_days=7)


def test_too_many_days_rejected():
    entries = [entry(date(2026, 9, d)) for d in range(20, 28)]
    with pytest.raises(ValidationError, match="7 日まで"):
        validate_work_entries(entries, today=TODAY, max_days=7)


def test_duplicate_dates_rejected():
    with pytest.raises(ValidationError, match="同じ日付"):
        validate_work_entries([entry(), entry()], today=TODAY, max_days=7)


def test_clock_out_before_clock_in_rejected():
    with pytest.raises(ValidationError, match="退勤"):
        validate_work_entries([entry(cin="18:00", cout="09:00")], today=TODAY, max_days=7)


def test_overnight_shift_allowed():
    validate_work_entries([entry(cin="20:00", cout="29:00", breaks=())], today=TODAY, max_days=7)


@pytest.mark.parametrize(
    "breaks",
    [
        (("08:00", "09:30"),),  # starts before clock-in
        (("17:30", "18:30"),),  # ends after clock-out
        (("12:00", "13:00"), ("12:30", "13:30")),  # overlap
        (("13:00", "12:00"),),  # inverted
    ],
)
def test_invalid_breaks_rejected(breaks):
    with pytest.raises(ValidationError):
        validate_work_entries([entry(breaks=breaks)], today=TODAY, max_days=7)


def test_overtime_range():
    validate_overtime_range("18:00", "21:00")
    with pytest.raises(ValidationError):
        validate_overtime_range("21:00", "18:00")
    with pytest.raises(ValidationError):
        validate_overtime_range("18:00", "25:00")


def bare(cin, cout, existing_breaks=()):
    return WorkRecordEntry(
        date=TODAY,
        clock_in=cin,
        clock_out=cout,
        existing=ExistingRecord(breaks=[TimeRange(start=s, end=e) for s, e in existing_breaks]),
    )


@pytest.mark.parametrize(
    "cin,cout,existing,expected",
    [
        ("09:00", "18:00", (), [("12:00", "13:00")]),
        # freee already has breaks: keep them instead of the default
        ("09:00", "19:00", (("12:30", "13:30"),), [("12:30", "13:30")]),
        # 6 hours or less: no default break
        ("09:00", "13:00", (), []),
        ("11:00", "14:00", (), []),
        ("12:00", "13:00", (), []),
        ("09:00", "15:00", (), []),
        ("09:00", "15:01", (), [("12:00", "13:00")]),
    ],
)
def test_resolve_breaks(cin, cout, existing, expected):
    got = resolve_breaks(bare(cin, cout, existing), DEFAULT_BREAK)
    assert [(b.start, b.end) for b in got] == expected


def test_resolve_breaks_returns_fresh_instances():
    assert resolve_breaks(bare("09:00", "18:00"), DEFAULT_BREAK)[0] is not DEFAULT_BREAK


@pytest.mark.parametrize(
    "cin,cout,existing,message",
    [
        ("13:00", "22:00", (), "休憩の指定がなく"),
        ("20:00", "29:00", (), "休憩の指定がなく"),
        ("14:00", "22:00", (("12:00", "13:00"),), "登録済みの休憩"),
    ],
)
def test_resolve_breaks_asks_when_nothing_fits(cin, cout, existing, message):
    with pytest.raises(ValidationError, match=message):
        resolve_breaks(bare(cin, cout, existing), DEFAULT_BREAK)
