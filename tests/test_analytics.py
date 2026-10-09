"""Independent Python reference calculations verify the database pipelines."""

import random
from datetime import date, timedelta

import pytest

from tests.test_api import employee, rounded


def insert_log(api_module, code, day, status="PRESENT", hours=8.0, late=0, overtime=0, half=False):
    row = {
        "emp_code": code,
        "date": day,
        "status": status,
        "work_hours": hours,
        "late_minutes": late,
        "overtime_minutes": overtime,
    }
    if half is not None:
        row["half_day"] = half
    api_module.db.attendance_logs.insert_one(row)
    return row


def test_no_logs_joiners_and_empty_departments(http):
    employee(http, joined_on="2026-07-15")
    employee(http, "EMP0002", joined_on="2026-08-01", department="Future")
    r = http.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"})
    assert r.status_code == 200, r.text
    assert r.json() == {
        "emp_code": "EMP0001",
        "month": "2026-07",
        "working_days": 13,
        "present_days": 0,
        "leave_days": 0,
        "late_count": 0,
        "total_late_minutes": 0,
        "total_overtime_minutes": 0,
        "attendance_pct": 0.0,
    }
    r = http.get("/analytics/departments/summary", params={"month": "2026-07"})
    assert r.status_code == 200, r.text
    assert r.json()["items"] == [
        {
            "department": "Engineering",
            "headcount": 1,
            "present_days": 0,
            "late_count": 0,
            "total_late_minutes": 0,
            "leave_count": 0,
            "on_duty_count": 0,
            "avg_work_hours": None,
        }
    ]
    assert (
        http.get("/analytics/employees/EMP0002/monthly", params={"month": "2026-07"}).json()[
            "attendance_pct"
        ]
        is None
    )
    assert (
        http.get("/analytics/employees/EMP9999/monthly", params={"month": "2026-07"}).status_code
        == 404
    )
    assert (
        http.get(
            "/analytics/departments/summary", params={"month": "2026-07", "department": "missing"}
        ).json()["items"]
        == []
    )


def test_weekends_stored_values_record_weighted_average(http, api_module):
    employee(http)
    employee(http, "EMP0002")
    employee(http, "EMP0003")  # No logs, still headcount.
    insert_log(api_module, "EMP0001", "2026-07-06", hours=2.0, late=10, half=True)
    insert_log(api_module, "EMP0001", "2026-07-07", status="ON_DUTY", hours=4.0, half=None)
    insert_log(api_module, "EMP0002", "2026-07-06", status="WFH", hours=9.0)
    insert_log(api_module, "EMP0002", "2026-07-11", hours=7.5, late=15, overtime=40)
    insert_log(api_module, "EMP0002", "2026-07-12", status="LEAVE", hours=None)
    insert_log(api_module, "EMP0002", "2026-07-08", hours=None)
    row = http.get("/analytics/departments/summary", params={"month": "2026-07"}).json()["items"][0]
    assert row == {
        "department": "Engineering",
        "headcount": 3,
        "present_days": 3.5,
        "avg_work_hours": 5.63,
        "late_count": 2,
        "total_late_minutes": 25,
        "leave_count": 1,
        "on_duty_count": 1,
    }
    row = http.get("/analytics/employees/EMP0002/monthly", params={"month": "2026-07"}).json()
    assert (
        row["present_days"] == 2
        and row["total_late_minutes"] == 15
        and row["total_overtime_minutes"] == 40
    )
    assert row["working_days"] == 23 and row["attendance_pct"] == 8.70


def test_rank_ties_cutoff_and_orphans(http, api_module):
    for index, total in enumerate([100, 50, 50, 20], 1):
        code = f"EMP{index:04d}"
        employee(http, code, department="B" if index == 1 else "A")
        insert_log(api_module, code, "2026-07-06", late=total)
    insert_log(api_module, "EMP9999", "2026-07-06", late=500)
    params = {"month": "2026-07", "limit": 2}
    r = http.get("/analytics/leaderboard/late", params=params)
    assert r.status_code == 200, r.text
    rows = r.json()["items"]
    assert [(x["emp_code"], x["rank"]) for x in rows] == [
        ("EMP0001", 1),
        ("EMP0002", 2),
        ("EMP0003", 2),
    ]
    rows = http.get("/analytics/leaderboard/late", params={**params, "limit": 3}).json()["items"]
    assert len(rows) == 3
    rows = http.get(
        "/analytics/leaderboard/late", params={**params, "department": "A", "limit": 1}
    ).json()["items"]
    assert [(x["emp_code"], x["rank"]) for x in rows] == [("EMP0002", 1), ("EMP0003", 1)]


def test_trend_calendar_headcount_rates_and_window(http, api_module):
    employee(http)
    employee(http, "EMP0002", joined_on="2026-07-08")
    employee(http, "EMP0003", joined_on="2026-08-01")
    insert_log(api_module, "EMP0001", "2026-07-06", half=True, late=10)
    insert_log(api_module, "EMP0001", "2026-07-08")
    insert_log(api_module, "EMP0002", "2026-07-08", status="ON_DUTY")
    insert_log(api_module, "EMP0001", "2026-07-11", status="WFH")
    r = http.get(
        "/analytics/departments/Engineering/trend",
        params={"from": "2026-07-06", "to": "2026-07-14"},
    )
    assert r.status_code == 200, r.text
    rows = r.json()["items"]
    assert len(rows) == 9
    assert [x["headcount"] for x in rows] == [1, 1, 2, 2, 2, 2, 2, 2, 2]
    assert [x["attendance_rate"] for x in rows] == [0.5, 0.0, 1.0, 0.0, 0.0, None, None, 0.0, 0.0]
    assert [x["moving_avg_7d"] for x in rows] == [0.5, 0.25, 0.5, 0.375, 0.3, 0.3, 0.3, 0.2, 0.2]
    assert rows[5]["present_count"] == 1 and rows[5]["is_working_day"] is False
    assert rows[0]["late_count"] == 1


def test_trend_all_empty_future_department_and_range_validation(http):
    employee(http, joined_on="2026-08-01", department="Future")
    r = http.get(
        "/analytics/departments/Future/trend", params={"from": "2026-07-01", "to": "2026-07-03"}
    )
    assert r.status_code == 200, r.text
    assert len(r.json()["items"]) == 3
    assert all(
        x["headcount"] == 0 and x["attendance_rate"] is None and x["moving_avg_7d"] is None
        for x in r.json()["items"]
    )
    params = {"from": "2026-07-01", "to": "2026-09-30"}
    assert len(http.get("/analytics/departments/Future/trend", params=params).json()["items"]) == 92
    for params in [
        {"from": "2026-07-01", "to": "2026-10-01"},
        {"from": "2026-07-02", "to": "2026-07-01"},
        {"from": "bad", "to": "2026-07-01"},
    ]:
        assert http.get("/analytics/departments/Future/trend", params=params).status_code == 422
    assert (
        http.get(
            "/analytics/departments/Missing/trend",
            params={"from": "2026-07-01", "to": "2026-07-01"},
        ).status_code
        == 404
    )


@pytest.mark.parametrize("month", ["2026-13", "2026-7", "0000-01", "2026-07-01"])
def test_bad_months(http, month):
    for path in [
        "/analytics/departments/summary",
        "/analytics/leaderboard/late",
        "/analytics/employees/EMP0001/monthly",
    ]:
        assert http.get(path, params={"month": month}).status_code == 422


def test_randomized_analytics_against_independent_reference(http, api_module):
    rng = random.Random(731)
    people = {}
    logs = []
    for i in range(1, 31):
        code = f"EMP{i:04d}"
        joined = date(2026, 7, 1) + timedelta(days=rng.choice([-20, 0, 10, 20, 40]))
        people[code] = employee(
            http, code, department="A" if i % 2 else "B", joined_on=joined.isoformat()
        )
        for offset in range(31):
            day = date(2026, 7, 1) + timedelta(days=offset)
            if day < joined or i % 7 == 0 or rng.random() < 0.4:
                continue
            status = rng.choice(["PRESENT", "WFH", "ON_DUTY", "LEAVE", "ABSENT"])
            hours = (
                rng.choice([None, 1.25, 4.49, 4.5, 8.13, 9.42])
                if status in ["PRESENT", "WFH", "ON_DUTY"]
                else None
            )
            logs.append(
                insert_log(
                    api_module,
                    code,
                    day.isoformat(),
                    status,
                    hours,
                    late=rng.choice([0, 10, 25, 45]) if hours is not None else 0,
                    overtime=rng.choice([0, 30, 40]) if hours is not None else 0,
                    half=rng.choice([False, True, None]),
                )
            )

    def is_present(x):
        return x["status"] in ["PRESENT", "WFH", "ON_DUTY"]

    def weight(x):
        return (0.5 if x.get("half_day", False) else 1) if is_present(x) else 0

    def weekdays(x):
        return date.fromisoformat(x["date"]).weekday() < 5

    for code, person in people.items():
        own = [x for x in logs if x["emp_code"] == code]
        working = sum(
            (date(2026, 7, 1) + timedelta(days=i)).weekday() < 5
            and (date(2026, 7, 1) + timedelta(days=i)).isoformat() >= person["joined_on"]
            for i in range(31)
        )
        present = sum(weight(x) for x in own if weekdays(x))
        expected = {
            "emp_code": code,
            "month": "2026-07",
            "working_days": working,
            "present_days": present,
            "leave_days": sum(x["status"] == "LEAVE" for x in own),
            "late_count": sum(x["late_minutes"] > 0 for x in own),
            "total_late_minutes": sum(x["late_minutes"] for x in own),
            "total_overtime_minutes": sum(x["overtime_minutes"] for x in own),
            "attendance_pct": rounded(present / working * 100) if working else None,
        }
        actual = http.get(f"/analytics/employees/{code}/monthly", params={"month": "2026-07"})
        assert actual.status_code == 200, actual.text
        assert actual.json() == expected
    for department in ["A", "B"]:
        eligible = {
            code
            for code, person in people.items()
            if person["department"] == department and person["joined_on"] <= "2026-07-31"
        }
        dept_logs = [x for x in logs if x["emp_code"] in eligible]
        hours = [
            x["work_hours"] for x in dept_logs if is_present(x) and x["work_hours"] is not None
        ]
        expected = {
            "department": department,
            "headcount": len(eligible),
            "present_days": sum(weight(x) for x in dept_logs if weekdays(x)),
            "avg_work_hours": rounded(sum(hours) / len(hours)) if hours else None,
            "late_count": sum(x["late_minutes"] > 0 for x in dept_logs),
            "total_late_minutes": sum(x["late_minutes"] for x in dept_logs),
            "leave_count": sum(x["status"] == "LEAVE" for x in dept_logs),
            "on_duty_count": sum(x["status"] == "ON_DUTY" for x in dept_logs),
        }
        assert http.get(
            "/analytics/departments/summary", params={"month": "2026-07", "department": department}
        ).json()["items"] == [expected]
        expected_trend = []
        for offset in range(31):
            day = date(2026, 7, 1) + timedelta(days=offset)
            own = [x for x in dept_logs if x["date"] == day.isoformat()]
            headcount = sum(
                person["department"] == department and person["joined_on"] <= day.isoformat()
                for person in people.values()
            )
            present = sum(weight(x) for x in own)
            rate = rounded(present / headcount, 4) if headcount and day.weekday() < 5 else None
            window = [x["attendance_rate"] for x in expected_trend[-6:]] + [rate]
            values = [x for x in window if x is not None]
            expected_trend.append(
                {
                    "date": day.isoformat(),
                    "is_working_day": day.weekday() < 5,
                    "headcount": headcount,
                    "present_count": present,
                    "late_count": sum(x["late_minutes"] > 0 for x in own),
                    "attendance_rate": rate,
                    "moving_avg_7d": rounded(sum(values) / len(values), 4) if values else None,
                }
            )
        actual = http.get(
            f"/analytics/departments/{department}/trend",
            params={"from": "2026-07-01", "to": "2026-07-31"},
        )
        assert actual.status_code == 200, actual.text
        assert actual.json()["items"] == expected_trend
    totals = sorted(
        [(sum(x["late_minutes"] for x in logs if x["emp_code"] == code), code) for code in people],
        key=lambda x: (-x[0], x[1]),
    )
    expected = [
        {
            "emp_code": code,
            "rank": 1 + sum(other > total for other, _ in totals),
            "total_late_minutes": total,
            "late_count": sum(x["late_minutes"] > 0 for x in logs if x["emp_code"] == code),
            "name": people[code]["name"],
            "department": people[code]["department"],
        }
        for total, code in totals
        if total > 0
    ]
    assert (
        http.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 50}).json()[
            "items"
        ]
        == expected
    )


def test_explain_real_queries_and_validation(http, api_module):
    employee(http)
    insert_log(api_module, "EMP0001", "2026-07-06", late=10)
    params_by_endpoint = {
        "attendance_list": {"emp_code": "EMP0001", "status": "PRESENT", "page": 2, "page_size": 5},
        "employee_monthly": {"emp_code": "EMP0001", "month": "2026-07"},
        "department_summary": {"month": "2026-07"},
        "late_leaderboard": {"month": "2026-07", "limit": 2},
        "department_trend": {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-07"},
    }
    for endpoint, params in params_by_endpoint.items():
        r = http.get("/admin/explain/" + endpoint, params=params)
        assert r.status_code == 200, r.text
        result = r.json()
        assert result["endpoint"] == endpoint and "IXSCAN" in str(result["explain"])
        assert "COLLSCAN" not in str(result["explain"])
        assert "executionStats" in str(result["explain"]) or "executionTimeMillis" in str(
            result["explain"]
        )
    for endpoint in [
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
        "bad",
    ]:
        assert http.get("/admin/explain/" + endpoint).status_code == 422


def test_decimal_rounding_midpoint_in_aggregation(http, api_module):
    employee(http)
    insert_log(api_module, "EMP0001", "2026-07-06", hours=1.24)
    insert_log(api_module, "EMP0001", "2026-07-07", hours=1.25)
    row = http.get("/analytics/departments/summary", params={"month": "2026-07"}).json()["items"][0]
    assert row["avg_work_hours"] == 1.25
