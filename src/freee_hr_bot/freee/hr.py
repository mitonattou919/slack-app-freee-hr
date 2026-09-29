from datetime import date
from typing import Any

import httpx

from freee_hr_bot.freee.client import FreeeClient, call_api

# freee accepts local wall-clock times in this format for work records.
DATETIME_FMT = "%Y-%m-%d %H:%M:%S"


async def get_me(http: httpx.AsyncClient, access_token: str) -> dict[str, Any]:
    return await call_api(http, access_token, "GET", "/api/v1/users/me")


class HRApi:
    """The subset of the freee HR API this bot uses, scoped to one employee."""

    def __init__(self, client: FreeeClient, company_id: int, employee_id: int) -> None:
        self.client = client
        self.company_id = company_id
        self.employee_id = employee_id

    async def get_work_record(self, day: date) -> dict[str, Any]:
        return await self.client.request(
            "GET",
            f"/api/v1/employees/{self.employee_id}/work_records/{day.isoformat()}",
            params={"company_id": self.company_id},
        )

    async def put_work_record(
        self,
        day: date,
        *,
        segments: list[dict[str, str]],
        breaks: list[dict[str, str]],
    ) -> dict[str, Any]:
        body = {
            "company_id": self.company_id,
            "work_record_segments": segments,
            "break_records": breaks,
        }
        return await self.client.request(
            "PUT",
            f"/api/v1/employees/{self.employee_id}/work_records/{day.isoformat()}",
            json=body,
        )

    async def get_summary(self, year: int, month: int) -> dict[str, Any]:
        return await self.client.request(
            "GET",
            f"/api/v1/employees/{self.employee_id}/work_record_summaries/{year}/{month}",
            params={"company_id": self.company_id},
        )

    async def list_attendance_routes(self) -> list[dict[str, Any]]:
        res = await self.client.request(
            "GET",
            "/api/v1/approval_flow_routes",
            params={"company_id": self.company_id, "usage": "AttendanceWorkflow"},
        )
        return res.get("approval_flow_routes", [])

    async def overtime_setting(self, day: date) -> dict[str, Any]:
        return await self.client.request(
            "GET",
            "/api/v1/approval_requests/overtime_works/setting",
            params={"company_id": self.company_id, "date": day.isoformat()},
        )

    async def create_paid_holiday(
        self, day: date, leave_type: str, route_id: int, comment: str | None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "company_id": self.company_id,
            "target_date": day.isoformat(),
            "values": [{"type": leave_type}],
            "approval_flow_route_id": route_id,
        }
        if comment:
            body["comment"] = comment
        return await self.client.request(
            "POST", "/api/v1/approval_requests/paid_holidays", json=body
        )

    async def create_overtime(
        self,
        day: date,
        *,
        start_at: str,
        end_at: str,
        reflect_in_work_record: bool,
        route_id: int,
        comment: str,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "company_id": self.company_id,
            "target_date": day.isoformat(),
            "approval_flow_route_id": route_id,
            "comment": comment,
        }
        # The parameter names depend on the company's "reflect in work record" setting.
        if reflect_in_work_record:
            body |= {"overtime_work_start_at": start_at, "overtime_work_end_at": end_at}
        else:
            body |= {"start_at": start_at, "end_at": end_at}
        return await self.client.request(
            "POST", "/api/v1/approval_requests/overtime_works", json=body
        )
