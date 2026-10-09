from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from threading import Barrier
from unittest.mock import patch

import pytest
from pymongo.errors import ServerSelectionTimeoutError

IST = timezone(timedelta(hours=5, minutes=30))


def ms(day, clock):
    return int(datetime.fromisoformat(day + "T" + clock).replace(tzinfo=IST).timestamp() * 1000)


def employee(http, code="EMP0001", **overrides):
    body = {
        "emp_code": code,
        "name": code,
        "email": code + "@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01",
        **overrides,
    }
    response = http.post("/employees", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def punch(http, code="EMP0001", day="2026-07-06", clock="09:30:00", **extras):
    return http.post(
        "/attendance/punch-in", json={"emp_code": code, "punched_at": ms(day, clock), **extras}
    )


def test_health(http, api_module):
    assert http.get("/health").json() == {"status": "ok"}
    with patch.object(api_module.db, "command", side_effect=ServerSelectionTimeoutError("down")):
        assert http.get("/health").status_code == 503


@pytest.mark.parametrize(
    "field,value",
    [
        ("emp_code", "EMP123"),
        ("emp_code", "0001"),
        ("emp_code", "EMP1234567"),
        ("name", ""),
        ("name", "a" * 101),
        ("email", "x@x"),
        ("email", "a b@x.com"),
        ("department", ""),
        ("department", "a" * 51),
        ("joined_on", "2026-02-30"),
        ("joined_on", "2026-2-03"),
        ("shift_start", "24:00"),
        ("shift_end", "9:30"),
        ("shift_end", "09:30"),
        ("name", 1),
        ("joined_on", "0000-01-01"),
    ],
)
def test_employee_validation(http, field, value):
    body = {
        "emp_code": "EMP0001",
        "name": "Asha",
        "email": "a@b.com",
        "department": "Engineering",
        "joined_on": "2026-01-01",
        field: value,
    }
    assert http.post("/employees", json=body).status_code == 422


def test_employee_pages_and_server_fields(http):
    for code in ["EMP0003", "EMP0001", "EMP0002"]:
        employee(http, code, department="A" if code != "EMP0003" else "a", created_at=0, _id="fake")
    row = http.get("/employees", params={"department": "A", "page_size": 1, "page": 2}).json()
    assert row["total"] == 2 and row["items"][0]["emp_code"] == "EMP0002"
    assert (
        isinstance(row["items"][0]["created_at"], int)
        and row["items"][0]["created_at"] > 100000000000
    )
    assert "_id" not in row["items"][0]
    assert (
        http.post(
            "/employees", json={k: v for k, v in row["items"][0].items() if k != "created_at"}
        ).status_code
        == 409
    )
    assert http.get("/employees", params={"page": 0}).status_code == 422
    assert http.get("/employees", params={"page_size": 101}).status_code == 422


@pytest.mark.parametrize(
    "value",
    [
        1783312500,
        1783312500000.0,
        "1783312500000",
        True,
        None,
        99999999999,
        4102444800001,
        -1,
        10**100,
    ],
)
def test_timestamp_validation(http, value):
    employee(http)
    for path in ["punch-in", "punch-out"]:
        assert (
            http.post(
                "/attendance/" + path, json={"emp_code": "EMP0001", "punched_at": value}
            ).status_code
            == 422
        )


@pytest.mark.parametrize(
    "clock,expected",
    [("09:40:00", 0), ("09:40:00.999", 0), ("09:40:01", 10), ("10:15:59", 45), ("09:00:00", 0)],
)
def test_grace_and_truncation(http, clock, expected):
    employee(http)
    response = punch(http, clock=clock, late_minutes=999)
    assert response.status_code == 201
    doc = response.json()
    assert doc["late_minutes"] == expected and doc["punch_in"] % 1000 == 0
    assert doc["history"] == [] and doc["work_hours"] is None and "id" not in doc


@pytest.mark.parametrize(
    "clock,hours,overtime,half",
    [
        ("13:59:41", 4.49, 0, True),
        ("13:59:42", 4.50, 0, False),
        ("09:48:54", 0.32, 0, True),
        ("18:59:59", 9.50, 0, False),
        ("19:00:00", 9.50, 30, False),
    ],
)
def test_work_rounding_and_overtime(http, clock, hours, overtime, half):
    employee(http)
    assert punch(http).status_code == 201
    r = http.post(
        "/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06", clock)}
    )
    assert r.status_code == 200, r.text
    row = r.json()
    assert (row["work_hours"], row["overtime_minutes"], row["half_day"]) == (hours, overtime, half)
    assert row["history"] == []
    assert (
        http.post(
            "/attendance/punch-out",
            json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06", clock)},
        ).status_code
        == 409
    )


def test_overnight(http):
    employee(http, shift_start="22:00", shift_end="06:00")
    r = punch(http, day="2026-07-07", clock="01:00:00")
    assert r.json()["date"] == "2026-07-06" and r.json()["late_minutes"] == 180
    r = http.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0001", "punched_at": ms("2026-07-07", "06:40:00")},
    )
    assert r.status_code == 200 and r.json()["overtime_minutes"] == 40
    assert punch(http, day="2026-07-07", clock="06:00:00").json()["date"] == "2026-07-07"


def test_ist_calendar_differs_from_utc(http):
    employee(http)
    r = punch(http, clock="00:30:00")
    assert r.json()["date"] == "2026-07-06"


def test_punch_errors_and_latest_record(http):
    assert punch(http).status_code == 404
    employee(http)
    assert http.post("/attendance/punch-out", json={"emp_code": "EMP0001"}).status_code == 404
    assert punch(http, status="ABSENT").status_code == 422
    assert punch(http).status_code == 201
    assert punch(http).status_code == 409
    for ts in [ms("2026-07-06", "09:30:00"), ms("2026-07-07", "09:30:01")]:
        assert (
            http.post(
                "/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ts}
            ).status_code
            == 422
        )
    assert (
        http.post(
            "/attendance/punch-out",
            json={"emp_code": "EMP0001", "punched_at": ms("2026-07-07", "09:30:00")},
        ).status_code
        == 200
    )
    assert punch(http, day="2026-07-07", clock="10:00:00").status_code == 201
    r = http.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0001", "punched_at": ms("2026-07-07", "18:30:00")},
    )
    assert r.status_code == 200 and r.json()["date"] == "2026-07-07"


def test_regularize_and_audit(http, api_module):
    employee(http)
    punch(http, clock="10:05:00")
    endpoint = "/attendance/EMP0001/2026-07-06"
    base = {"reason": "Biometric glitch", "regularized_by": "hr.admin"}
    r = http.patch(
        endpoint,
        json={
            **base,
            "punch_in": ms("2026-07-06", "09:28:00"),
            "punch_out": ms("2026-07-06", "18:30:00"),
            "work_hours": 999,
        },
    )
    assert r.status_code == 200, r.text
    row = r.json()
    assert row["work_hours"] == 9.03 and row["late_minutes"] == 0 and len(row["history"]) == 1
    changes = row["history"][0]["changes"]
    assert changes["punch_in"]["from"] == ms("2026-07-06", "10:05:00")
    assert isinstance(
        api_module.db.attendance_logs.find_one({})["history"][0]["changes"]["punch_in"]["to"],
        datetime,
    )
    assert http.patch(endpoint, json=base).status_code == 422
    assert (
        http.patch(
            endpoint, json={**base, "status": "LEAVE", "punch_in": ms("2026-07-06", "09:30:00")}
        ).status_code
        == 422
    )
    assert (
        http.patch(endpoint, json={**base, "punch_in": ms("2026-07-07", "09:30:00")}).status_code
        == 422
    )
    assert http.patch(endpoint, json={**base, "punch_out": None}).status_code == 422
    assert http.patch(endpoint, json={**base, "reason": "bad", "status": "WFH"}).status_code == 422
    r = http.patch(endpoint, json={**base, "status": "LEAVE"})
    assert r.status_code == 200 and len(r.json()["history"]) == 2
    assert (
        r.json()["punch_in"] is None and r.json()["work_hours"] is None and not r.json()["half_day"]
    )
    assert http.patch(endpoint, json={**base, "status": "PRESENT"}).status_code == 422
    assert (
        http.patch(
            endpoint, json={**base, "status": "PRESENT", "punch_in": ms("2026-07-06", "09:30:00")}
        ).status_code
        == 200
    )
    assert http.patch("/attendance/EMP0001/2026-07-07", json=base).status_code == 404


def test_legacy_defaults(http, api_module):
    employee(http)
    api_module.db.attendance_logs.insert_one(
        {"emp_code": "EMP0001", "date": "2026-07-06", "status": "LEAVE"}
    )
    row = http.get("/attendance").json()["items"][0]
    assert row["history"] == [] and row["half_day"] is False and row["punch_in"] is None
    r = http.patch(
        "/attendance/EMP0001/2026-07-06",
        json={
            "status": "WFH",
            "punch_in": ms("2026-07-06", "09:30:00"),
            "regularized_by": "hr",
            "reason": "Approved work",
        },
    )
    assert r.status_code == 200 and len(r.json()["history"]) == 1


def test_concurrent_creates_and_punches(http):
    body = {
        "emp_code": "EMP0001",
        "name": "A",
        "email": "a@b.com",
        "department": "A",
        "joined_on": "2026-01-01",
    }
    with ThreadPoolExecutor(max_workers=12) as pool:
        statuses = list(
            pool.map(lambda _: http.post("/employees", json=body).status_code, range(12))
        )
    assert statuses.count(201) == 1 and statuses.count(409) == 11
    with ThreadPoolExecutor(max_workers=12) as pool:
        statuses = list(pool.map(lambda _: punch(http).status_code, range(12)))
    assert statuses.count(201) == 1 and statuses.count(409) == 11
    with ThreadPoolExecutor(max_workers=12) as pool:
        statuses = list(
            pool.map(
                lambda _: (
                    http.post(
                        "/attendance/punch-out",
                        json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06", "18:30:00")},
                    ).status_code
                ),
                range(12),
            )
        )
    assert statuses.count(200) == 1 and statuses.count(409) == 11


def test_concurrent_regularizations_keep_every_success(http):
    employee(http)
    punch(http)
    with ThreadPoolExecutor(max_workers=12) as pool:
        responses = list(
            pool.map(
                lambda i: http.patch(
                    "/attendance/EMP0001/2026-07-06",
                    json={
                        "punch_in": ms("2026-07-06", f"10:{i:02d}:00"),
                        "regularized_by": str(i),
                        "reason": "Corrected time",
                    },
                ),
                range(12),
            )
        )
    assert all(r.status_code in [200, 409] for r in responses)
    history = http.get("/attendance").json()["items"][0]["history"]
    assert len(history) == sum(r.status_code == 200 for r in responses)
    assert len({entry["by"] for entry in history}) == len(history)


def test_attendance_filter_and_sort(http):
    employee(http)
    employee(http, "EMP0002")
    punch(http, "EMP0002", status="WFH")
    punch(http)
    punch(http, day="2026-07-07")
    row = http.get("/attendance", params={"page_size": 2}).json()
    assert [(x["date"], x["emp_code"]) for x in row["items"]] == [
        ("2026-07-07", "EMP0001"),
        ("2026-07-06", "EMP0001"),
    ]
    assert http.get("/attendance", params={"status": "WFH"}).json()["total"] == 1
    assert (
        http.get(
            "/attendance",
            params={"emp_code": "EMP0001", "date_from": "2026-07-07", "date_to": "2026-07-07"},
        ).json()["total"]
        == 1
    )
    for params in [
        {"date_from": "2026-07-08", "date_to": "2026-07-06"},
        {"date_from": "2026-02-30"},
        {"status": "bad"},
        {"page_size": 0},
    ]:
        assert http.get("/attendance", params=params).status_code == 422


def rounded(value, digits=2):
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP))


def test_forced_stale_corrections_conflict(http, api_module):
    """Force all readers to see the same snapshot; only one may commit."""
    from pymongo.synchronous.collection import Collection

    employee(http)
    punch(http)
    barrier = Barrier(8)
    original_find = Collection.find_one

    def simultaneous_read(collection, *args, **kwargs):
        doc = original_find(collection, *args, **kwargs)
        if collection.name == "attendance_logs":
            barrier.wait(timeout=10)
        return doc

    with (
        patch.object(Collection, "find_one", simultaneous_read),
        ThreadPoolExecutor(max_workers=8) as pool,
    ):
        responses = list(
            pool.map(
                lambda i: http.patch(
                    "/attendance/EMP0001/2026-07-06",
                    json={
                        "punch_in": ms("2026-07-06", f"10:{i:02d}:00"),
                        "regularized_by": str(i),
                        "reason": "Corrected time",
                    },
                ),
                range(8),
            )
        )
    assert sum(r.status_code == 200 for r in responses) == 1
    assert sum(r.status_code == 409 for r in responses) == 7
    assert len(http.get("/attendance").json()["items"][0]["history"]) == 1


def test_replica_metadata_explain_serialization(http, api_module):
    from bson import Binary, Timestamp

    raw = {
        "queryPlanner": {"winningPlan": {"stage": "IXSCAN"}},
        "$clusterTime": {
            "clusterTime": Timestamp(1783312500, 1),
            "signature": {"hash": Binary(b"test")},
        },
    }
    with patch.object(api_module.db, "command", return_value=raw):
        response = http.get("/admin/explain/attendance_list")
    assert response.status_code == 200
    assert response.json()["explain"]["$clusterTime"]["clusterTime"] == {
        "$timestamp": {"t": 1783312500, "i": 1}
    }
