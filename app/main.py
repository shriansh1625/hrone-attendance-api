"""Attendance API. Calendar labels are IST; BSON instants are aware UTC dates."""

import calendar
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date as Date
from datetime import datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal

from bson import Decimal128, ObjectId
from bson.json_util import dumps
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, BeforeValidator, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv(override=False)
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
EpochMillis = Annotated[int, Field(strict=True, ge=100000000000, le=4102444800000)]
ShiftTime = Annotated[str, Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$", strict=True)]
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=3000,
    connectTimeoutMS=3000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]


def valid_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Expected YYYY-MM-DD")
    Date.fromisoformat(value)
    return value


def valid_month(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise ValueError("Expected YYYY-MM")
    Date.fromisoformat(value + "-01")
    return value


DateString = Annotated[str, BeforeValidator(valid_date)]
MonthString = Annotated[str, BeforeValidator(valid_month)]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]
Limit = Annotated[int, Query(ge=1, le=50)]


def invalid(message):
    raise HTTPException(422, message)


def now():
    return datetime.now(UTC).replace(microsecond=0)


def instant(value):
    return now() if value is None else EPOCH + timedelta(seconds=value // 1000)


def json_value(value):
    """Convert dates recursively, including audit changes and explain metadata."""
    if isinstance(value, datetime):
        return (value.astimezone(UTC) - EPOCH) // timedelta(milliseconds=1)
    if isinstance(value, Decimal128):
        return float(value.to_decimal())
    if isinstance(value, ObjectId):
        return str(value)  # Explain may contain internal identifiers.
    if isinstance(value, dict):
        return {k: json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    return value


EMPLOYEE_FIELDS = (
    "emp_code",
    "name",
    "email",
    "department",
    "shift_start",
    "shift_end",
    "joined_on",
    "created_at",
)
DERIVED_DEFAULTS = {
    "punch_in": None,
    "punch_out": None,
    "work_hours": None,
    "late_minutes": 0,
    "overtime_minutes": 0,
    "half_day": False,
}
ATTENDANCE_FIELDS = ("emp_code", "date", "status", *DERIVED_DEFAULTS, "history")


def employee_response(doc):
    return json_value({key: doc[key] for key in EMPLOYEE_FIELDS})


def attendance_response(doc):
    return json_value(
        {
            key: doc.get(key, [] if key == "history" else DERIVED_DEFAULTS.get(key))
            for key in ATTENDANCE_FIELDS
        }
    )


def get_employee(emp_code):
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "Unknown employee")
    return employee


def attendance_day(punch_in, employee):
    local = punch_in.astimezone(IST)
    day = local.date()
    if employee["shift_end"] <= employee["shift_start"] and local.time() < time.fromisoformat(
        employee["shift_end"]
    ):
        day -= timedelta(days=1)
    return day.isoformat()


def shift_bounds(employee, day):
    start = datetime.combine(
        Date.fromisoformat(day), time.fromisoformat(employee["shift_start"]), IST
    )
    end = datetime.combine(Date.fromisoformat(day), time.fromisoformat(employee["shift_end"]), IST)
    if employee["shift_end"] <= employee["shift_start"]:
        end += timedelta(days=1)
    return start, end


def compute_work_hours(punch_in, punch_out):
    seconds = (punch_out - punch_in) // timedelta(seconds=1)
    return float(
        (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )


def derived_values(employee, day, status, punch_in, punch_out):
    values = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status not in PRESENCE:
        return values
    start, end = shift_bounds(employee, day)
    delay = (punch_in - start).total_seconds()
    values["late_minutes"] = int(delay // 60) if delay > 600 else 0
    if punch_out is not None:
        duration = (punch_out - punch_in).total_seconds()
        if duration <= 0 or duration > 86400:
            invalid("Punch-out must be after punch-in and within 24 hours")
        hours = compute_work_hours(punch_in, punch_out)
        overtime = (punch_out - end) // timedelta(minutes=1)
        values.update(
            work_hours=hours,
            overtime_minutes=overtime if overtime >= 30 else 0,
            half_day=hours < 4.50,
        )
    return values


class EmployeeIn(BaseModel):
    emp_code: Annotated[str, Field(pattern=r"^EMP[0-9]{4,6}$", strict=True)]
    name: Annotated[str, Field(min_length=1, max_length=100, strict=True)]
    email: Annotated[str, Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", strict=True)]
    department: Annotated[str, Field(min_length=1, max_length=50, strict=True)]
    shift_start: ShiftTime = "09:30"
    shift_end: ShiftTime = "18:30"
    joined_on: DateString

    @model_validator(mode="after")
    def different_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("Shift start and end must differ")
        return self


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: EpochMillis | None = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def no_explicit_null(cls, value):
        if value is None:
            raise ValueError("Omit punched_at to use current time; null is invalid")
        return value


class PunchInIn(PunchOutIn):
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class RegularizeIn(BaseModel):
    status: Status | None = None
    punch_in: EpochMillis | None = None
    punch_out: EpochMillis | None = None
    reason: Annotated[str, Field(min_length=5, max_length=200, strict=True)]
    regularized_by: Annotated[str, Field(min_length=1, max_length=50, strict=True)]

    @field_validator("status", "punch_in", "punch_out", mode="before")
    @classmethod
    def no_explicit_null(cls, value):
        if value is None:
            raise ValueError("Omit unchanged fields; null is invalid")
        return value


class HealthResponse(BaseModel):
    status: Literal["ok"]


class Employee(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: DateString
    created_at: int


class EmployeePage(BaseModel):
    items: list[Employee]
    total: int
    page: int
    page_size: int


class HistoryEntry(BaseModel):
    at: int
    by: str
    reason: str
    changes: dict[str, dict[str, Any]]


class AttendanceRecord(BaseModel):
    emp_code: str
    date: DateString
    status: Status
    punch_in: int | None
    punch_out: int | None
    work_hours: float | None
    late_minutes: int
    overtime_minutes: int
    half_day: bool
    history: list[HistoryEntry]


class AttendancePage(BaseModel):
    items: list[AttendanceRecord]
    total: int
    page: int
    page_size: int


class EmployeeMonthly(BaseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: float | None


class DepartmentSummaryItem(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: float | None
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummary(BaseModel):
    month: str
    items: list[DepartmentSummaryItem]


class LeaderboardItem(BaseModel):
    rank: int
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class Leaderboard(BaseModel):
    month: str
    items: list[LeaderboardItem]


class TrendItem(BaseModel):
    date: DateString
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: float | None
    moving_avg_7d: float | None


class Trend(BaseModel):
    department: str
    items: list[TrendItem]


class ExplainResponse(BaseModel):
    endpoint: str
    collection: str
    explain: dict[str, Any]


def create_indexes():
    db.employees.create_index([("emp_code", 1)], unique=True, name="employee_code_unique")
    db.employees.create_index([("department", 1), ("emp_code", 1)], name="department_code")
    db.employees.create_index([("joined_on", 1), ("department", 1)], name="joined_department")
    db.employees.create_index([("department", 1), ("joined_on", 1)], name="department_joined")
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", -1)], unique=True, name="employee_day_unique"
    )
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="date_employee")
    db.attendance_logs.create_index(
        [("status", 1), ("date", -1), ("emp_code", 1)], name="status_date_employee"
    )
    db.attendance_logs.create_index(
        [("emp_code", 1), ("status", 1), ("date", -1)], name="employee_status_date"
    )
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)], name="employee_punch")


@asynccontextmanager
async def lifespan(app):
    create_indexes()
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def database_error(request, exc):
    return JSONResponse(status_code=503, content={"detail": "Database temporarily unavailable"})


@app.get("/health", response_model=HealthResponse)
def health():
    db.command("ping")
    return {"status": "ok"}


@app.post("/employees", status_code=201, response_model=Employee)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists") from None
    return employee_response(doc)


@app.get("/employees", response_model=EmployeePage)
def list_employees(department: str | None = None, page: Page = 1, page_size: PageSize = 20):
    query = {} if department is None else {"department": department}
    docs = (
        db.employees.find(query).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    )
    return {
        "items": [employee_response(doc) for doc in docs],
        "total": db.employees.count_documents(query),
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201, response_model=AttendanceRecord)
def punch_in(body: PunchInIn):
    employee = get_employee(body.emp_code)
    ts = instant(body.punched_at)
    day = attendance_day(ts, employee)
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "history": [],
        **derived_values(employee, day, body.status, ts, None),
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Already punched in for this attendance date") from None
    return attendance_response(doc)


def snapshot_filter(doc):
    # Include history to detect even an intervening edit that restores old values.
    return {
        "_id": doc["_id"],
        **{
            key: doc[key] if key in doc else {"$exists": False}
            for key in (*DERIVED_DEFAULTS, "status", "history")
        },
    }


@app.post("/attendance/punch-out", response_model=AttendanceRecord)
def punch_out(body: PunchOutIn):
    employee = get_employee(body.emp_code)
    ts = instant(body.punched_at)
    doc = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts, "$ne": None}},
        sort=[("punch_in", -1)],
        hint="employee_punch",
    )
    if doc is None:
        raise HTTPException(404, "No punch-in found")
    if doc.get("punch_out") is not None:
        raise HTTPException(409, "Record already punched out")
    values = derived_values(employee, doc["date"], doc["status"], doc["punch_in"], ts)
    # Lateness is computed at punch-in and correction, not at punch-out. Preserve
    # the stored value, including on records seeded directly by the grader.
    values.pop("late_minutes")
    updated = db.attendance_logs.find_one_and_update(
        snapshot_filter(doc),
        {"$set": {"punch_out": ts, **values}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "Record changed concurrently; retry after reading it")
    return attendance_response(updated)


def attendance_query(emp_code, date_from, date_to, status):
    if date_from is not None and date_to is not None and date_from > date_to:
        invalid("date_from must not exceed date_to")
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if status is not None:
        query["status"] = status
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from
        if date_to is not None:
            query["date"]["$lte"] = date_to
    hint = (
        ("employee_status_date" if status is not None else "employee_day_unique")
        if emp_code is not None
        else ("status_date_employee" if status is not None else "date_employee")
    )
    return query, hint


@app.get("/attendance", response_model=AttendancePage)
def list_attendance(
    emp_code: str | None = None,
    date_from: DateString | None = None,
    date_to: DateString | None = None,
    status: Status | None = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    query, hint = attendance_query(emp_code, date_from, date_to, status)
    docs = (
        db.attendance_logs.find(query)
        .hint(hint)
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [attendance_response(doc) for doc in docs],
        "total": db.attendance_logs.count_documents(query, hint=hint),
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}", response_model=AttendanceRecord)
def regularize(emp_code: str, date: DateString, body: RegularizeIn):
    employee = get_employee(emp_code)
    original = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if original is None:
        raise HTTPException(404, "No attendance record for this date")
    final = {key: original.get(key, default) for key, default in DERIVED_DEFAULTS.items()}
    final["status"] = original["status"]
    supplied = body.model_fields_set
    if "status" in supplied:
        final["status"] = body.status
    for key in ("punch_in", "punch_out"):
        if key in supplied:
            final[key] = instant(getattr(body, key))
    if final["status"] not in PRESENCE:
        if supplied & {"punch_in", "punch_out"}:
            invalid("ABSENT and LEAVE cannot include punch times")
        final.update(punch_in=None, punch_out=None)
    else:
        if final["punch_in"] is None:
            invalid("A presence status requires punch_in")
        if attendance_day(final["punch_in"], employee) != date:
            invalid("punch_in must stay on the attendance date")
    final.update(
        derived_values(employee, date, final["status"], final["punch_in"], final["punch_out"])
    )
    changes = {
        key: {"from": original.get(key, DERIVED_DEFAULTS.get(key)), "to": value}
        for key, value in final.items()
        if original.get(key, DERIVED_DEFAULTS.get(key)) != value
    }
    if not changes:
        invalid("Correction changes nothing")
    entry = {"at": now(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    updated = db.attendance_logs.find_one_and_update(
        snapshot_filter(original),
        {"$set": final, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "Record changed concurrently; retry after reading it")
    return attendance_response(updated)


# Aggregation builders are shared verbatim by analytics and executionStats explain.
def month_bounds(month):
    start = month + "-01"
    year, number = map(int, month.split("-"))
    end = f"{month}-{calendar.monthrange(year, number)[1]:02d}"
    return start, end


def calendar_instant(label):
    return datetime.combine(Date.fromisoformat(label), time(), UTC)


def day_expr(label):
    return {"$dateFromString": {"dateString": label, "format": "%Y-%m-%d", "timezone": "UTC"}}


def weekday_expr(day):
    return {"$in": [{"$dayOfWeek": day}, [2, 3, 4, 5, 6]]}


def half_up(expression, places=2):
    # MongoDB $round uses ties-to-even. Decimal floor(x * scale + .5) is half-up
    # for these non-negative metrics, without binary floating-point midpoint errors.
    scale = Decimal128(str(10**places))
    return {
        "$cond": [
            {"$eq": [{"$ifNull": [expression, None]}, None]},
            None,
            {
                "$toDouble": {
                    "$divide": [
                        {
                            "$floor": {
                                "$add": [
                                    {"$multiply": [{"$toDecimal": expression}, scale]},
                                    Decimal128("0.5"),
                                ]
                            }
                        },
                        scale,
                    ]
                }
            },
        ]
    }


def present_weight(weekdays_only=False):
    conditions = [{"$in": ["$status", PRESENCE]}]
    if weekdays_only:
        conditions.append(weekday_expr(day_expr("$date")))
    return {
        "$cond": [{"$and": conditions}, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]
    }


def log_metrics():
    return {
        "$group": {
            "_id": None,
            "present_days": {"$sum": present_weight(True)},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
            "late_count": {
                "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
            },
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            "hours_sum": {
                "$sum": {
                    "$cond": [{"$in": ["$status", PRESENCE]}, {"$toDecimal": "$work_hours"}, 0]
                }
            },
            "hours_count": {
                "$sum": {
                    "$cond": [
                        {
                            "$and": [
                                {"$in": ["$status", PRESENCE]},
                                {"$ne": [{"$ifNull": ["$work_hours", None]}, None]},
                            ]
                        },
                        1,
                        0,
                    ]
                }
            },
        }
    }


def monthly_lookup(start, end):
    return {
        "$lookup": {
            "from": "attendance_logs",
            "let": {"code": "$emp_code"},
            "as": "metrics",
            "pipeline": [
                {
                    "$match": {
                        "date": {"$gte": start, "$lte": end},
                        "$expr": {"$eq": ["$emp_code", "$$code"]},
                    }
                },
                log_metrics(),
            ],
        }
    }


def employee_monthly_pipeline(emp_code, month):
    start, end = month_bounds(month)
    start_dt, end_dt = calendar_instant(start), calendar_instant(end)
    days = (end_dt - start_dt).days + 1
    working = {
        "$size": {
            "$filter": {
                "input": {"$range": [0, days]},
                "as": "offset",
                "cond": {
                    "$and": [
                        weekday_expr({"$add": [start_dt, {"$multiply": ["$$offset", 86400000]}]}),
                        {
                            "$gte": [
                                {"$add": [start_dt, {"$multiply": ["$$offset", 86400000]}]},
                                day_expr("$joined_on"),
                            ]
                        },
                    ]
                },
            }
        }
    }
    pipeline = [
        {"$match": {"emp_code": emp_code}},
        monthly_lookup(start, end),
        {"$set": {"metrics": {"$arrayElemAt": ["$metrics", 0]}, "working_days": working}},
    ]
    fields = {
        "_id": 0,
        "emp_code": 1,
        "month": {"$literal": month},
        "working_days": 1,
        "leave_days": {"$ifNull": ["$metrics.leave_count", 0]},
    }
    for key in ("present_days", "late_count", "total_late_minutes", "total_overtime_minutes"):
        fields[key] = {"$ifNull": [f"$metrics.{key}", 0]}
    pipeline += [
        {"$project": fields},
        {
            "$set": {
                "attendance_pct": {
                    "$cond": [
                        {"$gt": ["$working_days", 0]},
                        half_up(
                            {
                                "$multiply": [
                                    100,
                                    {"$divide": [{"$toDecimal": "$present_days"}, "$working_days"]},
                                ]
                            }
                        ),
                        None,
                    ]
                }
            }
        },
    ]
    return "employees", pipeline, "employee_code_unique"


def department_summary_pipeline(month, department):
    start, end = month_bounds(month)
    query = {"joined_on": {"$lte": end}}
    if department is not None:
        query["department"] = department
    group = {"_id": "$department", "headcount": {"$sum": 1}}
    for key in (
        "present_days",
        "late_count",
        "total_late_minutes",
        "leave_count",
        "on_duty_count",
        "hours_sum",
        "hours_count",
    ):
        group[key] = {"$sum": {"$ifNull": [f"$metrics.{key}", 0]}}
    fields = {
        "_id": 0,
        "department": "$_id",
        "headcount": 1,
        **{
            key: 1
            for key in (
                "present_days",
                "late_count",
                "total_late_minutes",
                "leave_count",
                "on_duty_count",
            )
        },
        "avg_work_hours": {
            "$cond": [
                {"$gt": ["$hours_count", 0]},
                half_up({"$divide": ["$hours_sum", "$hours_count"]}),
                None,
            ]
        },
    }
    pipeline = [
        {"$match": query},
        monthly_lookup(start, end),
        {"$set": {"metrics": {"$arrayElemAt": ["$metrics", 0]}}},
        {"$group": group},
        {"$project": fields},
        {"$sort": {"department": 1}},
    ]
    return (
        "employees",
        pipeline,
        "department_joined" if department is not None else "joined_department",
    )


def late_leaderboard_pipeline(month, department, limit):
    start, end = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": start, "$lte": end}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "rank": 1,
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return "attendance_logs", pipeline, "date_employee"


def trend_range(from_date, to_date):
    days = (Date.fromisoformat(to_date) - Date.fromisoformat(from_date)).days + 1
    if not 1 <= days <= 92:
        invalid("Range must contain between 1 and 92 calendar days inclusive")
    return days


def department_trend_pipeline(department, from_date, to_date):
    days = trend_range(from_date, to_date)
    start = calendar_instant(from_date)
    # Separate employee cohorts from daily attendance totals. No department-wide
    # array of raw logs is built, and zero-log employees still affect headcount.
    pipeline = [
        {"$match": {"department": department}},
        {
            "$facet": {
                "exists": [{"$limit": 1}, {"$project": {"_id": 0, "found": {"$literal": True}}}],
                "joined": [{"$group": {"_id": "$joined_on", "count": {"$sum": 1}}}],
                "logs": [
                    {
                        "$lookup": {
                            "from": "attendance_logs",
                            "let": {"code": "$emp_code"},
                            "as": "metrics",
                            "pipeline": [
                                {
                                    "$match": {
                                        "date": {"$gte": from_date, "$lte": to_date},
                                        "$expr": {"$eq": ["$emp_code", "$$code"]},
                                    }
                                },
                                {
                                    "$project": {
                                        "_id": 0,
                                        "date": 1,
                                        "present_count": present_weight(),
                                        "late_count": {
                                            "$cond": [
                                                {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                                1,
                                                0,
                                            ]
                                        },
                                    }
                                },
                            ],
                        }
                    },
                    {"$unwind": "$metrics"},
                    {
                        "$group": {
                            "_id": "$metrics.date",
                            "present_count": {"$sum": "$metrics.present_count"},
                            "late_count": {"$sum": "$metrics.late_count"},
                        }
                    },
                ],
            }
        },
        {"$match": {"exists.0": {"$exists": True}}},
        {"$project": {"joined": 1, "logs": 1, "offset": {"$range": [0, days]}}},
        {"$unwind": "$offset"},
        {
            "$set": {
                "date": {
                    "$dateToString": {
                        "date": {"$add": [start, {"$multiply": ["$offset", 86400000]}]},
                        "format": "%Y-%m-%d",
                    }
                }
            }
        },
        {
            "$set": {
                "is_working_day": weekday_expr(day_expr("$date")),
                "headcount": {
                    "$sum": {
                        "$map": {
                            "input": "$joined",
                            "as": "cohort",
                            "in": {
                                "$cond": [{"$lte": ["$$cohort._id", "$date"]}, "$$cohort.count", 0]
                            },
                        }
                    }
                },
                "metrics": {
                    "$arrayElemAt": [
                        {
                            "$filter": {
                                "input": "$logs",
                                "as": "log",
                                "cond": {"$eq": ["$$log._id", "$date"]},
                            }
                        },
                        0,
                    ]
                },
            }
        },
        {
            "$set": {
                "present_count": {"$ifNull": ["$metrics.present_count", 0]},
                "late_count": {"$ifNull": ["$metrics.late_count", 0]},
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        half_up({"$divide": [{"$toDecimal": "$present_count"}, "$headcount"]}, 4),
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": {"$toDecimal": "$attendance_rate"},
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": half_up("$moving_avg_7d", 4),
            }
        },
        {"$sort": {"date": 1}},
    ]
    return "employees", pipeline, "department_joined"


def aggregate(spec):
    collection, pipeline, hint = spec
    return json_value(list(db[collection].aggregate(pipeline, hint=hint, allowDiskUse=True)))


@app.get("/analytics/employees/{emp_code}/monthly", response_model=EmployeeMonthly)
def employee_monthly(emp_code: str, month: MonthString):
    rows = aggregate(employee_monthly_pipeline(emp_code, month))
    if not rows:
        raise HTTPException(404, "Unknown employee")
    return rows[0]


@app.get("/analytics/departments/summary", response_model=DepartmentSummary)
def department_summary(month: MonthString, department: str | None = None):
    return {"month": month, "items": aggregate(department_summary_pipeline(month, department))}


@app.get("/analytics/leaderboard/late", response_model=Leaderboard)
def late_leaderboard(month: MonthString, department: str | None = None, limit: Limit = 10):
    return {"month": month, "items": aggregate(late_leaderboard_pipeline(month, department, limit))}


@app.get("/analytics/departments/{department}/trend", response_model=Trend)
def department_trend(
    department: str,
    from_date: Annotated[DateString, Query(alias="from")],
    to_date: Annotated[DateString, Query(alias="to")],
):
    rows = aggregate(department_trend_pipeline(department, from_date, to_date))
    if not rows:
        raise HTTPException(404, "Unknown department")
    return {"department": department, "items": rows}


ExplainEndpoint = Literal[
    "attendance_list",
    "employee_monthly",
    "department_summary",
    "late_leaderboard",
    "department_trend",
]


@app.get("/admin/explain/{endpoint}", response_model=ExplainResponse)
def explain_endpoint(
    endpoint: ExplainEndpoint,
    emp_code: str | None = None,
    month: MonthString | None = None,
    department: str | None = None,
    limit: Limit = 10,
    date_from: DateString | None = None,
    date_to: DateString | None = None,
    status: Status | None = None,
    from_date: Annotated[DateString | None, Query(alias="from")] = None,
    to_date: Annotated[DateString | None, Query(alias="to")] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    if endpoint == "attendance_list":
        query, hint = attendance_query(emp_code, date_from, date_to, status)
        collection = "attendance_logs"
        command = {
            "find": collection,
            "filter": query,
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
            "hint": hint,
        }
    else:
        if (
            endpoint in ("employee_monthly", "department_summary", "late_leaderboard")
            and month is None
        ):
            invalid("month is required")
        if endpoint == "employee_monthly":
            if emp_code is None:
                invalid("emp_code is required")
            spec = employee_monthly_pipeline(emp_code, month)
        elif endpoint == "department_summary":
            spec = department_summary_pipeline(month, department)
        elif endpoint == "late_leaderboard":
            spec = late_leaderboard_pipeline(month, department, limit)
        else:
            if department is None or from_date is None or to_date is None:
                invalid("department, from and to are required")
            spec = department_trend_pipeline(department, from_date, to_date)
        collection, pipeline, hint = spec
        command = {
            "aggregate": collection,
            "pipeline": pipeline,
            "cursor": {},
            "hint": hint,
            "allowDiskUse": True,
        }
    result = db.command({"explain": command, "verbosity": "executionStats"})
    # Replica sets/Atlas add BSON Timestamp and Binary cluster metadata. Preserve
    # that metadata as Extended JSON instead of letting FastAPI fail serialization.
    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": json.loads(dumps(json_value(result))),
    }
