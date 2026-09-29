from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

LeaveType = Literal["full", "morning", "afternoon"]
LEAVE_LABELS: dict[str, str] = {"full": "全休", "morning": "午前休", "afternoon": "午後休"}


class TimeRange(BaseModel):
    start: str = Field(description="HH:MM")
    end: str = Field(description="HH:MM. Past midnight is written as 24:30, 25:00, ...")


class ExistingRecord(BaseModel):
    """Snapshot of what freee currently has for a day, shown as the 'before' side of the diff."""

    segments: list[TimeRange] = []
    breaks: list[TimeRange] = []
    paid_holiday: str | None = None
    is_absence: bool = False
    is_editable: bool = True

    @property
    def is_empty(self) -> bool:
        return not (self.segments or self.breaks or self.paid_holiday or self.is_absence)


class WorkRecordEntry(BaseModel):
    date: date
    clock_in: str
    clock_out: str
    breaks: list[TimeRange] = []
    existing: ExistingRecord | None = None


class WorkRecordProposal(BaseModel):
    kind: Literal["work_records"] = "work_records"
    entries: list[WorkRecordEntry]


class RouteOption(BaseModel):
    id: int
    name: str


class PaidLeaveProposal(BaseModel):
    kind: Literal["paid_leave"] = "paid_leave"
    date: date
    leave_type: LeaveType
    comment: str | None = None
    routes: list[RouteOption] = []
    route_id: int | None = None


class OvertimeProposal(BaseModel):
    kind: Literal["overtime"] = "overtime"
    date: date
    start: str
    end: str
    comment: str
    reflect_in_work_record: bool = False
    routes: list[RouteOption] = []
    route_id: int | None = None


AnyProposal = WorkRecordProposal | PaidLeaveProposal | OvertimeProposal


def load_proposal(payload: dict) -> AnyProposal:
    kind = payload.get("kind")
    model: type[AnyProposal] = {
        "work_records": WorkRecordProposal,
        "paid_leave": PaidLeaveProposal,
        "overtime": OvertimeProposal,
    }[kind]  # type: ignore[index]
    return model.model_validate(payload)
