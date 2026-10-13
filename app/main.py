"""Employee Attendance & Analytics API, HROne contract v2.0."""
import calendar
import json
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal, Optional
from zoneinfo import ZoneInfo

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()
UTC = timezone.utc
IST = ZoneInfo("Asia/Kolkata")
PRESENT_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = (*PRESENT_STATUSES, "ABSENT", "LEAVE")
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB")
if not MONGO_URI or not MONGO_DB:
    raise RuntimeError("MONGO_URI and MONGO_DB must be configured in the environment or .env file")
client = MongoClient(MONGO_URI, tz_aware=True, tzinfo=UTC, serverSelectionTimeoutMS=3000)
db = client[MONGO_DB]
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")
_indexes_ready = False


def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def truncate_second(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0)


def epoch_to_dt(value: int) -> datetime:
    if type(value) is not int or not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
        raise ValueError("must be an integer epoch-millisecond timestamp in the supported range")
    return truncate_second(datetime.fromtimestamp(value / 1000, UTC))


def dt_to_epoch(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    return int(truncate_second(value).timestamp() * 1000)


def serialize_value(value):
    if isinstance(value, datetime):
        return dt_to_epoch(value)
    if isinstance(value, list):
        return [serialize_value(v) for v in value]
    if isinstance(value, dict):
        return {k: serialize_value(v) for k, v in value.items() if k != "_id"}
    return value


def serialize_employee(doc: dict) -> dict:
    return {k: serialize_value(v) for k, v in doc.items() if k != "_id"}


def serialize_attendance(doc: dict) -> dict:
    # API records intentionally have no MongoDB-generated identifier.
    result = {k: serialize_value(v) for k, v in doc.items() if k != "_id"}
    result.setdefault("punch_in", None)
    result.setdefault("punch_out", None)
    result.setdefault("work_hours", None)
    result.setdefault("late_minutes", 0)
    result.setdefault("overtime_minutes", 0)
    result.setdefault("half_day", False)
    result.setdefault("history", [])
    return result


def parse_date(value: str, field: str = "date") -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(422, f"{field} must be a valid YYYY-MM-DD date")
    if parsed.isoformat() != value:
        raise HTTPException(422, f"{field} must be a valid YYYY-MM-DD date")
    return parsed


def month_bounds(month: str) -> tuple[str, str]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or ""):
        raise HTTPException(422, "month must use YYYY-MM format")
    year, mon = map(int, month.split("-"))
    last = calendar.monthrange(year, mon)[1]
    return f"{year:04d}-{mon:02d}-01", f"{year:04d}-{mon:02d}-{last:02d}"


def working_days(start: date, end: date) -> int:
    if end < start:
        return 0
    days = (end - start).days + 1
    weeks, rem = divmod(days, 7)
    count = weeks * 5
    for offset in range(rem):
        if (start + timedelta(days=weeks * 7 + offset)).weekday() < 5:
            count += 1
    return count


def round_half_up(value, places: int = 2):
    if value is None:
        return None
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def ist_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(IST)


def shift_start_datetime(attendance_date: str, shift_start: str) -> datetime:
    day = date.fromisoformat(attendance_date)
    h, m = map(int, shift_start.split(":"))
    return datetime.combine(day, time(h, m), IST)


def shift_end_datetime(attendance_date: str, shift_start: str, shift_end: str) -> datetime:
    day = date.fromisoformat(attendance_date)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    end_day = day + timedelta(days=1) if (eh, em) <= (sh, sm) else day
    return datetime.combine(end_day, time(eh, em), IST)


def attendance_date_for(punch_in: datetime, employee: dict) -> str:
    local = ist_datetime(punch_in)
    sh_end = time(*map(int, employee["shift_end"].split(":")))
    sh_start = time(*map(int, employee["shift_start"].split(":")))
    day = local.date()
    if sh_end <= sh_start and local.time().replace(tzinfo=None) < sh_end:
        day -= timedelta(days=1)
    return day.isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_date: str) -> int:
    punch = ist_datetime(truncate_second(punch_in))
    start = shift_start_datetime(attendance_date, shift_start)
    elapsed = int((punch - start).total_seconds())
    return elapsed // 60 if elapsed > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((truncate_second(punch_out) - truncate_second(punch_in)).total_seconds())
    return round_half_up(Decimal(seconds) / Decimal(3600), 2)


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, attendance_date: str) -> int:
    end = shift_end_datetime(attendance_date, shift_start, shift_end)
    minutes = int((ist_datetime(truncate_second(punch_out)) - end).total_seconds()) // 60
    return minutes if minutes >= 30 else 0


def validate_presence_status(status: str):
    if status not in PRESENT_STATUSES:
        raise HTTPException(422, "status must be PRESENT, WFH, or ON_DUTY")


def create_indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employees_emp_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employees_department_joined")
    db.employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING)], name="ix_employees_joined_department")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_emp_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_emp")
    db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", ASCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_late_emp")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="ix_attendance_emp_punchin")


@app.on_event("startup")
def startup():
    global _indexes_ready
    try:
        create_indexes()
        _indexes_ready = True
    except PyMongoError:
        # Keep the process available so /health can report 503 while MongoDB is down.
        # A later successful readiness check retries index creation.
        _indexes_ready = False


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, value):
        try:
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError()
        except ValueError:
            raise ValueError("joined_on must be a valid YYYY-MM-DD date")
        return value

    @model_validator(mode="after")
    def distinct_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[Annotated[StrictInt, Field(ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @model_validator(mode="after")
    def reject_null_timestamp(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at cannot be null; omit it to use the current time")
        return self


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[Annotated[StrictInt, Field(ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]] = None

    @model_validator(mode="after")
    def reject_null_timestamp(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at cannot be null; omit it to use the current time")
        return self


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[Annotated[StrictInt, Field(ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]] = None
    punch_out: Optional[Annotated[StrictInt, Field(ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def reject_null_edit_values(self):
        if "status" in self.model_fields_set and self.status is None:
            raise ValueError("status cannot be null")
        if "punch_in" in self.model_fields_set and self.punch_in is None:
            raise ValueError("punch_in cannot be null")
        if "punch_out" in self.model_fields_set and self.punch_out is None:
            raise ValueError("punch_out cannot be null")
        return self


def checked_page(page: int, page_size: int):
    if page < 1:
        raise HTTPException(422, "page must be at least 1")
    if page_size < 1 or page_size > 100:
        raise HTTPException(422, "page_size must be between 1 and 100")


def month_log_match(start: str, end: str, extra: Optional[dict] = None) -> dict:
    q = {"date": {"$gte": start, "$lte": end}}
    if extra:
        q.update(extra)
    return q


def attendance_list_query(emp_code=None, date_from=None, date_to=None, status=None):
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        bounds = {}
        if date_from:
            bounds["$gte"] = date_from
        if date_to:
            bounds["$lte"] = date_to
        q["date"] = bounds
    if status:
        q["status"] = status
    return q


def monthly_employee_pipeline(emp_code: str, month: str):
    start, end = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {"$addFields": {"_date": {"$dateFromString": {"dateString": "$date"}}, "late_minutes": {"$ifNull": ["$late_minutes", 0]}, "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]}, "half_day": {"$ifNull": ["$half_day", False]}}},
        {"$group": {"_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENT_STATUSES)]}, {"$lt": [{"$dayOfWeek": {"date": "$_date", "timezone": "Asia/Kolkata"}}, 7]}, {"$gt": [{"$dayOfWeek": {"date": "$_date", "timezone": "Asia/Kolkata"}}, 1]}]}, {"$cond": ["$half_day", 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$late_minutes"}, "total_overtime_minutes": {"$sum": "$overtime_minutes"}}},
    ]


def department_summary_pipeline(month: str, department: Optional[str] = None):
    start, end = month_bounds(month)
    match = {"joined_on": {"$lte": end}}
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start]}, {"$lte": ["$date", end]}]}}},
            {"$addFields": {"_date": {"$dateFromString": {"dateString": "$date"}}, "late_minutes": {"$ifNull": ["$late_minutes", 0]}, "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]}, "half_day": {"$ifNull": ["$half_day", False]}}}
        ], "as": "logs"}},
        {"$set": {"emp_stats": {"$reduce": {"input": "$logs", "initialValue": {"present_days": 0.0, "late_count": 0, "total_late_minutes": 0, "leave_count": 0, "on_duty_count": 0, "work_sum": 0.0, "work_count": 0}, "in": {
            "present_days": {"$add": ["$$value.present_days", {"$cond": [{"$and": [{"$in": ["$$this.status", list(PRESENT_STATUSES)]}, {"$gt": [{"$dayOfWeek": {"date": "$$this._date", "timezone": "Asia/Kolkata"}}, 1]}, {"$lt": [{"$dayOfWeek": {"date": "$$this._date", "timezone": "Asia/Kolkata"}}, 7]}]}, {"$cond": ["$$this.half_day", 0.5, 1]}, 0]}]},
            "late_count": {"$add": ["$$value.late_count", {"$cond": [{"$gt": ["$$this.late_minutes", 0]}, 1, 0]}]},
            "total_late_minutes": {"$add": ["$$value.total_late_minutes", "$$this.late_minutes"]},
            "leave_count": {"$add": ["$$value.leave_count", {"$cond": [{"$eq": ["$$this.status", "LEAVE"]}, 1, 0]}]},
            "on_duty_count": {"$add": ["$$value.on_duty_count", {"$cond": [{"$eq": ["$$this.status", "ON_DUTY"]}, 1, 0]}]},
            "work_sum": {"$add": ["$$value.work_sum", {"$cond": [{"$and": [{"$in": ["$$this.status", list(PRESENT_STATUSES)]}, {"$ne": ["$$this.work_hours", None]}]}, "$$this.work_hours", 0]}]},
            "work_count": {"$add": ["$$value.work_count", {"$cond": [{"$and": [{"$in": ["$$this.status", list(PRESENT_STATUSES)]}, {"$ne": ["$$this.work_hours", None]}]}, 1, 0]}]}
        }}}}},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}, "present_days": {"$sum": "$emp_stats.present_days"}, "late_count": {"$sum": "$emp_stats.late_count"}, "total_late_minutes": {"$sum": "$emp_stats.total_late_minutes"}, "leave_count": {"$sum": "$emp_stats.leave_count"}, "on_duty_count": {"$sum": "$emp_stats.on_duty_count"}, "work_sum": {"$sum": "$emp_stats.work_sum"}, "work_count": {"$sum": "$emp_stats.work_count"}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1, "present_days": 1, "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1, "avg_work_hours": {"$cond": [{"$gt": ["$work_count", 0]}, {"$divide": ["$work_sum", "$work_count"]}, None]}}},
        {"$sort": {"department": 1}}
    ]


def leaderboard_pipeline(month: str, limit: int, department: Optional[str] = None):
    start, end = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}, "late_count": {"$sum": 1}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$set": {"emp_code": "$_id", "name": "$employee.name", "department": "$employee.department"}},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": 1, "name": 1, "department": 1, "total_late_minutes": 1, "late_count": 1}}
    ]
    return pipeline


def trend_pipeline(department: str, start: str, end: str):
    start_d = date.fromisoformat(start)
    end_d = date.fromisoformat(end)
    # Seed a single row from the indexed department match, then use $densify to fill every date.
    return [
        {"$match": {"department": department}},
        {"$group": {"_id": None}},
        {"$project": {"_id": 0, "date": {"$literal": datetime.combine(start_d, time.min, UTC)}}},
        {"$densify": {"field": "date", "range": {"step": 1, "unit": "day", "bounds": [datetime.combine(start_d, time.min, UTC), datetime.combine(end_d + timedelta(days=1), time.min, UTC)]}}},
        {"$set": {"date_str": {"$dateToString": {"date": "$date", "format": "%Y-%m-%d", "timezone": "Asia/Kolkata"}}, "is_working_day": {"$and": [{"$gt": [{"$dayOfWeek": {"date": "$date", "timezone": "Asia/Kolkata"}}, 1]}, {"$lt": [{"$dayOfWeek": {"date": "$date", "timezone": "Asia/Kolkata"}}, 7]}]}}},
        {"$lookup": {"from": "employees", "let": {"day": "$date_str"}, "pipeline": [
            {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day"]}}},
            {"$project": {"_id": 0, "emp_code": 1}}
        ], "as": "staff"}},
        {"$set": {"headcount": {"$size": "$staff"}}},
        {"$lookup": {"from": "attendance_logs", "let": {"day": "$date_str", "staff_codes": "$staff.emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$date", "$$day"]}, {"$in": ["$emp_code", "$$staff_codes"]}]}}},
            {"$group": {
                "_id": None,
                "present_count": {"$sum": {"$cond": [
                    {"$in": ["$status", list(PRESENT_STATUSES)]},
                    {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                    0
                ]}},
                "late_count": {"$sum": {"$cond": [
                    {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0
                ]}}
            }}
        ], "as": "daily"}},
        {"$set": {"present_count": {"$ifNull": [{"$arrayElemAt": ["$daily.present_count", 0]}, 0]}, "late_count": {"$ifNull": [{"$arrayElemAt": ["$daily.late_count", 0]}, 0]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$divide": ["$present_count", "$headcount"]}, None]}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": "$date_str", "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
        {"$sort": {"date": 1}}
    ]


@app.get("/health")
def health():
    global _indexes_ready
    try:
        db.command("ping")
        if not _indexes_ready:
            create_indexes()
            _indexes_ready = True
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = utc_now()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    items = list(db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size))
    return {"items": [serialize_employee(x) for x in items], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if not employee:
        raise HTTPException(404, "unknown employee")
    ts = epoch_to_dt(body.punched_at) if body.punched_at is not None else utc_now()
    date_key = attendance_date_for(ts, employee)
    doc = {"emp_code": body.emp_code, "date": date_key, "status": body.status, "punch_in": ts, "punch_out": None,
           "work_hours": None, "late_minutes": compute_late_minutes(ts, employee["shift_start"], date_key),
           "overtime_minutes": 0, "half_day": False, "history": []}
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_attendance(doc)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
                    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if date_from:
        parse_date(date_from, "date_from")
    if date_to:
        parse_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")
    query = attendance_list_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query)
    docs = list(db.attendance_logs.find(query, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size))
    return {"items": [serialize_attendance(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if not employee:
        raise HTTPException(404, "unknown employee")
    ts = epoch_to_dt(body.punched_at) if body.punched_at is not None else utc_now()
    doc = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)])
    if not doc:
        raise HTTPException(404, "no punch-in found")
    if doc.get("punch_out") is not None:
        raise HTTPException(409, "record already punched out")
    if ts <= doc["punch_in"] or ts - doc["punch_in"] > timedelta(hours=24):
        raise HTTPException(422, "punched_at must be after punch-in and within 24 hours")
    hours = compute_work_hours(doc["punch_in"], ts)
    overtime = compute_overtime(ts, employee["shift_start"], employee["shift_end"], doc["date"])
    half = hours < 4.50
    result = db.attendance_logs.update_one({"_id": doc["_id"], "$or": [{"punch_out": None}, {"punch_out": {"$exists": False}}]},
                                           {"$set": {"punch_out": ts, "work_hours": hours, "overtime_minutes": overtime, "half_day": half}})
    if result.modified_count != 1:
        raise HTTPException(409, "record already punched out")
    doc.update({"punch_out": ts, "work_hours": hours, "overtime_minutes": overtime, "half_day": half})
    return serialize_attendance(doc)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date)
    employee = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if not employee:
        raise HTTPException(404, "unknown employee")
    doc = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not doc:
        raise HTTPException(404, "attendance record not found")
    old = {"status": doc.get("status"), "punch_in": doc.get("punch_in"), "punch_out": doc.get("punch_out"),
           "work_hours": doc.get("work_hours"), "late_minutes": doc.get("late_minutes", 0),
           "overtime_minutes": doc.get("overtime_minutes", 0), "half_day": doc.get("half_day", False)}
    new = dict(old)
    supplied = body.model_fields_set
    if "status" in supplied and body.status is not None:
        new["status"] = body.status
    if "punch_in" in supplied:
        new["punch_in"] = epoch_to_dt(body.punch_in) if body.punch_in is not None else None
    if "punch_out" in supplied:
        new["punch_out"] = epoch_to_dt(body.punch_out) if body.punch_out is not None else None
    if new["status"] in ("ABSENT", "LEAVE"):
        if ("punch_in" in supplied and new["punch_in"] is not None) or ("punch_out" in supplied and new["punch_out"] is not None):
            raise HTTPException(422, "ABSENT/LEAVE cannot be supplied with punch times")
        new["punch_in"] = new["punch_out"] = None
    elif new["punch_in"] is None:
        raise HTTPException(422, "presence status requires punch_in")
    if new["punch_in"] is not None:
        if attendance_date_for(new["punch_in"], employee) != date:
            raise HTTPException(422, "punch_in must belong to the record attendance date")
        new["late_minutes"] = compute_late_minutes(new["punch_in"], employee["shift_start"], date)
    else:
        new["late_minutes"] = 0
    if new["punch_out"] is not None:
        if new["punch_in"] is None or new["punch_out"] <= new["punch_in"] or new["punch_out"] - new["punch_in"] > timedelta(hours=24):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
        new["work_hours"] = compute_work_hours(new["punch_in"], new["punch_out"])
        new["overtime_minutes"] = compute_overtime(new["punch_out"], employee["shift_start"], employee["shift_end"], date)
        new["half_day"] = new["work_hours"] < 4.50
    else:
        new["work_hours"] = None
        new["overtime_minutes"] = 0
        new["half_day"] = False
    changes = {}
    for field in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"):
        if old[field] != new[field]:
            changes[field] = {"from": old[field], "to": new[field]}
    if not changes:
        raise HTTPException(422, "request does not change the record")
    history_entry = {"at": utc_now(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    previous_history = doc.get("history", [])
    history_filter = {"history": previous_history} if "history" in doc else {"history": {"$exists": False}}
    filt = {"_id": doc["_id"], **history_filter, "status": doc.get("status")}
    if "punch_in" in doc:
        filt["punch_in"] = doc.get("punch_in")
    else:
        filt["punch_in"] = {"$exists": False}
    if "punch_out" in doc:
        filt["punch_out"] = doc.get("punch_out")
    else:
        filt["punch_out"] = {"$exists": False}
    sets = {k: new[k] for k in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")}
    result = db.attendance_logs.update_one(filt, {"$set": sets, "$push": {"history": history_entry}})
    if result.modified_count != 1:
        raise HTTPException(409, "record changed concurrently; retry correction")
    doc.update(sets)
    doc.setdefault("history", []).append(history_entry)
    return serialize_attendance(doc)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    start, end = month_bounds(month)
    employee = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if not employee:
        raise HTTPException(404, "unknown employee")
    rows = list(db.attendance_logs.aggregate(monthly_employee_pipeline(emp_code, month)))
    stats = rows[0] if rows else {"present_days": 0.0, "leave_days": 0, "late_count": 0, "total_late_minutes": 0, "total_overtime_minutes": 0}
    work_start = max(date.fromisoformat(start), date.fromisoformat(employee["joined_on"]))
    wd = working_days(work_start, date.fromisoformat(end))
    pct = round_half_up(Decimal(str(stats["present_days"])) * 100 / wd, 4) if wd else None
    return {"emp_code": emp_code, "month": month, "working_days": wd, "present_days": round_half_up(stats["present_days"], 2),
            "leave_days": stats["leave_days"], "late_count": stats["late_count"], "total_late_minutes": stats["total_late_minutes"],
            "total_overtime_minutes": stats["total_overtime_minutes"], "attendance_pct": pct}


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):
    rows = list(db.employees.aggregate(department_summary_pipeline(month, department)))
    items = []
    for row in rows:
        if row["headcount"] <= 0:
            continue
        row["present_days"] = round_half_up(row["present_days"], 2)
        row["avg_work_hours"] = round_half_up(row["avg_work_hours"], 2)
        items.append(row)
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    return {"month": month, "items": list(db.attendance_logs.aggregate(leaderboard_pipeline(month, limit, department)))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(alias="from"), to_date: str = Query(alias="to")):
    start = parse_date(from_date, "from")
    end = parse_date(to_date, "to")
    if end < start:
        raise HTTPException(422, "to must be on or after from")
    if (end - start).days + 1 > 92:
        raise HTTPException(422, "date range must not exceed 92 days")
    if not db.employees.find_one({"department": department}, {"_id": 1}):
        raise HTTPException(404, "unknown department")
    rows = list(db.employees.aggregate(trend_pipeline(department, start.isoformat(), end.isoformat())))
    for row in rows:
        row["attendance_rate"] = round_half_up(row["attendance_rate"], 4)
        row["moving_avg_7d"] = round_half_up(row["moving_avg_7d"], 4)
        row["present_count"] = round_half_up(row["present_count"], 2)
    return {"department": department, "items": rows}


def explain_command(collection: Optional[str], pipeline=None, query=None, sort=None, skip=0, limit=None):
    if pipeline is not None:
        command = {"aggregate": collection if collection is not None else 1, "pipeline": pipeline, "cursor": {}}
    else:
        find_cmd = {"find": collection, "filter": query or {}, "sort": sort or {}}
        if skip:
            find_cmd["skip"] = skip
        if limit:
            find_cmd["limit"] = limit
        command = find_cmd
    try:
        explain = db.command("explain", command, verbosity="executionStats")
    except PyMongoError as exc:
        raise HTTPException(503, f"MongoDB explain failed: {exc.__class__.__name__}")
    return json.loads(json_util.dumps(explain))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
                     emp_code: Optional[str] = None, month: Optional[str] = None, department: Optional[str] = None,
                     limit: int = Query(10, ge=1, le=50), date_from: Optional[str] = None, date_to: Optional[str] = None,
                     status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
                     from_date: Optional[str] = Query(None, alias="from"), to_date: Optional[str] = Query(None, alias="to"),
                     page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if endpoint == "attendance_list":
        if date_from:
            parse_date(date_from, "date_from")
        if date_to:
            parse_date(date_to, "date_to")
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must be <= date_to")
        q = attendance_list_query(emp_code, date_from, date_to, status)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_command("attendance_logs", query=q, sort={"date": -1, "emp_code": 1}, skip=(page - 1) * page_size, limit=page_size)}
    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        month_bounds(month)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_command("attendance_logs", pipeline=monthly_employee_pipeline(emp_code, month))}
    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        month_bounds(month)
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_command("employees", pipeline=department_summary_pipeline(month, department))}
    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        month_bounds(month)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_command("attendance_logs", pipeline=leaderboard_pipeline(month, limit, department))}
    if endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from and to are required")
        start = parse_date(from_date, "from")
        end = parse_date(to_date, "to")
        if end < start or (end - start).days + 1 > 92:
            raise HTTPException(422, "invalid trend date range")
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_command("employees", pipeline=trend_pipeline(department, start.isoformat(), end.isoformat()))}
    raise HTTPException(422, "unsupported endpoint")
