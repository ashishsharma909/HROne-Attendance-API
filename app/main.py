"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is fine; real env vars win)

All code lives in this one file as required by the assignment contract.
"""
import os
from calendar import monthrange
from contextlib import asynccontextmanager
from datetime import date as date_type
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

IST = timezone(timedelta(hours=5, minutes=30))

# ---------------------------------------------------------------------------
# MongoDB client – naive datetimes (tz_aware=False is PyMongo default)
# ---------------------------------------------------------------------------
_client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = _client[os.getenv("MONGO_DB", "attendance_db")]


# ---------------------------------------------------------------------------
# Index creation (idempotent – runs at import time so startup is fast)
# ---------------------------------------------------------------------------
def _create_indexes() -> None:
    # employees
    db.employees.create_index(
        [("emp_code", ASCENDING)], unique=True, name="employees_emp_code_u"
    )
    db.employees.create_index(
        [("department", ASCENDING)], name="employees_department"
    )
    db.employees.create_index(
        [("joined_on", ASCENDING)], name="employees_joined_on"
    )
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="employees_dept_joined",
    )
    # attendance_logs
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="logs_emp_code_date_u",
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("emp_code", ASCENDING)],
        name="logs_date_emp_code",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", ASCENDING)],
        name="logs_emp_code_punch_in",
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("status", ASCENDING)],
        name="logs_date_status",
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("late_minutes", ASCENDING)],
        name="logs_date_late_minutes",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)],
        name="logs_emp_date_status",
    )


_create_indexes()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
@asynccontextmanager
async def _lifespan(app: FastAPI):
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=_lifespan,
)


# ===========================================================================
# Constants
# ===========================================================================
EPOCH_MIN = 100_000_000_000    # smallest valid epoch-ms (year ~1973)
EPOCH_MAX = 4_102_444_800_000  # largest  valid epoch-ms (year ~2100)
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}


# ===========================================================================
# Datetime / epoch utilities
# ===========================================================================

def _validate_epoch_ms(v: Any) -> int:
    """Validate that v is a valid epoch-millisecond integer (not float/string/OOB)."""
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError("must be an integer (epoch milliseconds)")
    if not (EPOCH_MIN <= v <= EPOCH_MAX):
        raise ValueError(
            f"epoch ms must be between {EPOCH_MIN} and {EPOCH_MAX}"
        )
    return v


def ms_to_dt(ms: int) -> datetime:
    """Epoch ms → naive UTC datetime, truncated to whole seconds (R1)."""
    return datetime.utcfromtimestamp(ms // 1000)


def dt_to_ms(dt: datetime) -> int:
    """Naive-UTC or tz-aware datetime → epoch ms."""
    if dt.tzinfo is None:
        return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)
    return int(dt.timestamp() * 1000)


def _to_ist(dt: datetime) -> datetime:
    """Naive-UTC datetime → IST-aware datetime."""
    return dt.replace(tzinfo=timezone.utc).astimezone(IST)


def _now_truncated() -> datetime:
    """Current time as naive UTC, truncated to whole seconds (R1)."""
    return datetime.utcnow().replace(microsecond=0)


def _is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start


def _attendance_date(punch_in_naive_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: IST calendar date of punch_in.
    For overnight shifts (shift_end <= shift_start), a punch-in whose IST
    time is *earlier than* shift_end belongs to the shift that started the
    previous IST day.
    """
    punch_ist = _to_ist(punch_in_naive_utc)
    if _is_overnight(shift_start, shift_end):
        h_end, m_end = map(int, shift_end.split(":"))
        # If IST time < shift_end → previous IST day
        se_naive = punch_ist.replace(hour=h_end, minute=m_end, second=0, microsecond=0)
        if punch_ist.replace(tzinfo=None) < se_naive.replace(tzinfo=None):
            return (punch_ist - timedelta(days=1)).date().isoformat()
    return punch_ist.date().isoformat()


# ===========================================================================
# Business-rule calculations (R2-R5)
# ===========================================================================

def _half_up(value: float, decimals: int) -> float:
    """Half-up rounding to `decimals` places (R4, R8)."""
    quant = Decimal(10) ** -decimals
    return float(Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP))


def _compute_late_minutes(
    punch_in_naive_utc: datetime, shift_start: str, att_date: str
) -> int:
    """
    R2: Grace = 10 min *strict* (i.e. > 600 seconds from shift_start).
    Returns floor minutes from shift_start (not from grace-period end).
    att_date is the IST attendance date YYYY-MM-DD.
    Examples (shift 09:30):
      09:40:00 IST → 600 s → not > 600 → 0
      09:40:01 IST → 601 s → > 600    → floor(601/60) = 10
      10:15:59 IST → 2759 s → > 600   → floor(2759/60) = 45
    """
    h, m = map(int, shift_start.split(":"))
    shift_start_ist = datetime.fromisoformat(att_date).replace(
        hour=h, minute=m, second=0, microsecond=0, tzinfo=IST
    )
    shift_start_utc_naive = shift_start_ist.astimezone(timezone.utc).replace(tzinfo=None)
    diff_secs = (punch_in_naive_utc - shift_start_utc_naive).total_seconds()
    if diff_secs <= 600:   # within or at the 10-minute grace boundary
        return 0
    return int(diff_secs // 60)  # floor minutes from shift_start


def _compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded half-up to 2dp."""
    secs = (punch_out - punch_in).total_seconds()
    return _half_up(secs / 3600, 2)


def _compute_overtime(
    punch_out_naive_utc: datetime, shift_end: str, att_date: str, overnight: bool
) -> int:
    """
    R3: floor minutes from shift_end to punch_out; only counted if >= 30.
    For overnight shifts, shift_end is on the next IST calendar day.
    """
    h, m = map(int, shift_end.split(":"))
    base_date = datetime.fromisoformat(att_date)
    if overnight:
        base_date = base_date + timedelta(days=1)
    end_ist = base_date.replace(hour=h, minute=m, second=0, microsecond=0, tzinfo=IST)
    end_utc_naive = end_ist.astimezone(timezone.utc).replace(tzinfo=None)
    ot_secs = (punch_out_naive_utc - end_utc_naive).total_seconds()
    ot_min = int(ot_secs // 60)
    return ot_min if ot_min >= 30 else 0


def _recompute_derived(
    punch_in_naive: Optional[datetime],
    punch_out_naive: Optional[datetime],
    shift_start: str,
    shift_end: str,
    att_date: str,
) -> dict:
    """Compute late_minutes, work_hours, overtime_minutes, half_day for a record."""
    overnight = _is_overnight(shift_start, shift_end)
    if punch_in_naive is None:
        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    late = _compute_late_minutes(punch_in_naive, shift_start, att_date)
    if punch_out_naive is None:
        return {"late_minutes": late, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    wh = _compute_work_hours(punch_in_naive, punch_out_naive)
    ot = _compute_overtime(punch_out_naive, shift_end, att_date, overnight)
    return {
        "late_minutes": late,
        "work_hours": wh,
        "overtime_minutes": ot,
        "half_day": wh < 4.50,  # R5: based on the already-rounded value
    }


# ===========================================================================
# Serialisation helpers  (BSON datetime ↔ epoch ms at the API boundary)
# ===========================================================================

def _ser_employee(doc: dict) -> dict:
    doc.pop("_id", None)
    if isinstance(doc.get("created_at"), datetime):
        doc["created_at"] = dt_to_ms(doc["created_at"])
    return doc


def _ser_history_entry(entry: dict) -> dict:
    if isinstance(entry.get("at"), datetime):
        entry["at"] = dt_to_ms(entry["at"])
    for fld in ("punch_in", "punch_out"):
        ch = entry.get("changes", {}).get(fld)
        if ch:
            if isinstance(ch.get("from"), datetime):
                ch["from"] = dt_to_ms(ch["from"])
            if isinstance(ch.get("to"), datetime):
                ch["to"] = dt_to_ms(ch["to"])
    return entry


def _ser_attendance(doc: dict) -> dict:
    doc.pop("_id", None)
    for fld in ("punch_in", "punch_out"):
        if isinstance(doc.get(fld), datetime):
            doc[fld] = dt_to_ms(doc[fld])
    doc.setdefault("late_minutes", 0)
    doc.setdefault("overtime_minutes", 0)
    doc.setdefault("half_day", False)
    doc.setdefault("history", [])
    doc["history"] = [_ser_history_entry(e) for e in doc["history"]]
    return doc


def _changes_to_bson(changes: dict) -> dict:
    """Convert API-side changes dict (epoch ms) → BSON-friendly (naive UTC datetimes)."""
    out = {}
    for fld, diff in changes.items():
        if fld in ("punch_in", "punch_out"):
            out[fld] = {
                "from": ms_to_dt(diff["from"]) if diff["from"] is not None else None,
                "to":   ms_to_dt(diff["to"])   if diff["to"]   is not None else None,
            }
        else:
            out[fld] = diff
    return out


# ===========================================================================
# Pydantic request models
# ===========================================================================

class EmployeeCreate(BaseModel):
    emp_code:    str = Field(..., pattern=r"^EMP\d{4,6}$")
    name:        str = Field(..., min_length=1, max_length=100)
    email:       str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department:  str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end:   str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on:   str

    @model_validator(mode="after")
    def _check_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code:   str
    punched_at: Optional[int] = None
    status:     str = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def _val_pa(cls, v):
        return _validate_epoch_ms(v) if v is not None else v

    @field_validator("status")
    @classmethod
    def _val_status(cls, v):
        if v not in PRESENCE_STATUSES:
            raise ValueError(f"status must be one of {sorted(PRESENCE_STATUSES)}")
        return v


class PunchOutRequest(BaseModel):
    emp_code:   str
    punched_at: Optional[int] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def _val_pa(cls, v):
        return _validate_epoch_ms(v) if v is not None else v


class RegularizeRequest(BaseModel):
    status:         Optional[str] = None
    punch_in:       Optional[int] = None
    punch_out:      Optional[int] = None
    reason:         str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def _val_status(cls, v):
        if v is not None and v not in ALL_STATUSES:
            raise ValueError(f"status must be one of {sorted(ALL_STATUSES)}")
        return v

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def _val_punch(cls, v):
        return _validate_epoch_ms(v) if v is not None else v


# ===========================================================================
# Analytics helpers
# ===========================================================================

def _parse_month(month: str):
    """Return (year, mon, month_start_str, month_end_str)."""
    year, mon = int(month[:4]), int(month[5:])
    _, last_day = monthrange(year, mon)
    return year, mon, f"{year:04d}-{mon:02d}-01", f"{year:04d}-{mon:02d}-{last_day:02d}"


def _count_working_days(start_str: str, end_str: str) -> int:
    """Count Mon–Fri days in [start_str, end_str] inclusive."""
    cur = date_type.fromisoformat(start_str)
    end = date_type.fromisoformat(end_str)
    count = 0
    while cur <= end:
        if cur.weekday() < 5:
            count += 1
        cur += timedelta(days=1)
    return count


# $dayOfWeek: 1=Sun,2=Mon,...,7=Sat  →  Mon-Fri is [2,3,4,5,6]
_IS_WORKDAY_EXPR = {
    "$and": [
        {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]},
        {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]},
    ]
}

_PRESENT_VALUE_EXPR = {
    "$cond": [
        {
            "$and": [
                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                _IS_WORKDAY_EXPR,
            ]
        },
        {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1.0]},
        0.0,
    ]
}


# ===========================================================================
# ENDPOINTS – system
# ===========================================================================

@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")


# ===========================================================================
# ENDPOINTS – employees
# ===========================================================================

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = _now_truncated()          # naive UTC; stored as BSON date
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="emp_code already exists")
    return _ser_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page:      int = Query(default=1,  ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q: dict = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items":     [_ser_employee(d) for d in items],
        "total":     total,
        "page":      page,
        "page_size": page_size,
    }


# ===========================================================================
# ENDPOINTS – attendance
# ===========================================================================

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    # R1: truncate to whole seconds
    punch_in_utc = ms_to_dt(body.punched_at) if body.punched_at else _now_truncated()
    att_date = _attendance_date(punch_in_utc, emp["shift_start"], emp["shift_end"])
    late = _compute_late_minutes(punch_in_utc, emp["shift_start"], att_date)

    doc = {
        "emp_code":         body.emp_code,
        "date":             att_date,
        "status":           body.status,
        "punch_in":         punch_in_utc,   # naive UTC
        "punch_out":        None,
        "work_hours":       None,
        "late_minutes":     late,
        "overtime_minutes": 0,
        "half_day":         False,
        "history":          [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="Already punched in for this date")
    return _ser_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    punch_out_utc = ms_to_dt(body.punched_at) if body.punched_at else _now_truncated()

    # Find the most recent record with a punch-in
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None}},
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        raise HTTPException(404, detail="No punch-in found")

    punch_in_utc: datetime = record["punch_in"]   # stored as naive UTC

    # Validate timing (R4 pre-check)
    if punch_out_utc <= punch_in_utc:
        raise HTTPException(422, detail="punched_at must be after punch_in")
    if (punch_out_utc - punch_in_utc).total_seconds() > 86_400:
        raise HTTPException(422, detail="punched_at must be within 24 hours of punch_in")

    overnight = _is_overnight(emp["shift_start"], emp["shift_end"])
    derived = {
        "punch_out":        punch_out_utc,
        "work_hours":       _compute_work_hours(punch_in_utc, punch_out_utc),
        "overtime_minutes": _compute_overtime(
            punch_out_utc, emp["shift_end"], record["date"], overnight
        ),
    }
    derived["half_day"] = derived["work_hours"] < 4.50  # R5

    # Atomic: only update if punch_out is still null (concurrent safety)
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": derived},
        return_document=True,
    )
    if updated is None:
        raise HTTPException(409, detail="Record already punched out")
    return _ser_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code:  Optional[str] = None,
    date_from: Optional[str] = None,
    date_to:   Optional[str] = None,
    status:    Optional[str] = None,
    page:      int = Query(default=1,  ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if status and status not in ALL_STATUSES:
        raise HTTPException(422, detail=f"Invalid status '{status}'")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, detail="date_from must be <= date_to")

    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    skip  = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    docs  = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items":     [_ser_attendance(d) for d in docs],
        "total":     total,
        "page":      page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, detail="No attendance record for this date")

    # ---------- resolve final field values ----------
    new_status = body.status if body.status is not None else record["status"]

    # ABSENT/LEAVE cannot coexist with punch times in the request
    if new_status in ("ABSENT", "LEAVE") and (
        body.punch_in is not None or body.punch_out is not None
    ):
        raise HTTPException(
            422, detail="Cannot supply punch times when setting ABSENT or LEAVE"
        )

    # Resolve new punch times (None for ABSENT/LEAVE, else from body or kept)
    if new_status in ("ABSENT", "LEAVE"):
        new_pi: Optional[datetime] = None
        new_po: Optional[datetime] = None
    else:
        old_pi = record.get("punch_in")   # naive UTC or None
        old_po = record.get("punch_out")
        new_pi = ms_to_dt(body.punch_in)  if body.punch_in  is not None else old_pi
        new_po = ms_to_dt(body.punch_out) if body.punch_out is not None else old_po

    # Presence status needs punch_in
    if new_status in PRESENCE_STATUSES and new_pi is None:
        raise HTTPException(422, detail="Presence status requires punch_in")

    # punch_in must stay on the record's attendance date (R1)
    if new_pi is not None:
        computed_date = _attendance_date(new_pi, emp["shift_start"], emp["shift_end"])
        if computed_date != date:
            raise HTTPException(
                422, detail="punch_in would move the record to a different attendance date"
            )

    # punch_out constraints
    if new_po is not None and new_pi is not None:
        if new_po <= new_pi:
            raise HTTPException(422, detail="punch_out must be after punch_in")
        if (new_po - new_pi).total_seconds() > 86_400:
            raise HTTPException(422, detail="punch_out must be within 24 hours of punch_in")

    # ---------- recompute derived fields ----------
    derived = _recompute_derived(
        new_pi, new_po, emp["shift_start"], emp["shift_end"], date
    )

    # ---------- build changes object ----------
    old_pi_raw = record.get("punch_in")
    old_po_raw = record.get("punch_out")
    old_pi_ms  = dt_to_ms(old_pi_raw) if old_pi_raw else None
    old_po_ms  = dt_to_ms(old_po_raw) if old_po_raw else None
    new_pi_ms  = dt_to_ms(new_pi)     if new_pi     else None
    new_po_ms  = dt_to_ms(new_po)     if new_po     else None

    changes: dict = {}
    comparisons = [
        ("status",           record["status"],                            new_status),
        ("punch_in",         old_pi_ms,                                   new_pi_ms),
        ("punch_out",        old_po_ms,                                   new_po_ms),
        ("work_hours",       record.get("work_hours"),                    derived["work_hours"]),
        ("late_minutes",     record.get("late_minutes",     0),           derived["late_minutes"]),
        ("overtime_minutes", record.get("overtime_minutes", 0),           derived["overtime_minutes"]),
        ("half_day",         record.get("half_day",         False),       derived["half_day"]),
    ]
    for field, old_val, new_val in comparisons:
        if old_val != new_val:
            changes[field] = {"from": old_val, "to": new_val}

    if not changes:
        raise HTTPException(422, detail="Request would change nothing")

    # ---------- build history entry ----------
    history_entry_for_api = {
        "at":      dt_to_ms(_now_truncated()),
        "by":      body.regularized_by,
        "reason":  body.reason,
        "changes": changes,
    }
    # Store BSON version (punch datetimes as naive UTC)
    history_entry_bson = {
        "at":      _now_truncated(),
        "by":      body.regularized_by,
        "reason":  body.reason,
        "changes": _changes_to_bson(changes),
    }

    # ---------- atomic update with optimistic concurrency ----------
    history_len = len(record.get("history") or [])
    update_result = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "$expr": {
                "$eq": [
                    {"$size": {"$ifNull": ["$history", []]}},
                    history_len,
                ]
            },
        },
        {
            "$set": {
                "status":           new_status,
                "punch_in":         new_pi,
                "punch_out":        new_po,
                "work_hours":       derived["work_hours"],
                "late_minutes":     derived["late_minutes"],
                "overtime_minutes": derived["overtime_minutes"],
                "half_day":         derived["half_day"],
            },
            "$push": {"history": history_entry_bson},
        },
        return_document=True,
    )
    if update_result is None:
        raise HTTPException(409, detail="Concurrent modification – please retry")
    return _ser_attendance(update_result)


# ===========================================================================
# ENDPOINTS – analytics
# ===========================================================================

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    _, _, month_start, month_end = _parse_month(month)

    # Working days: Mon–Fri from max(joined_on, month_start) to month_end (R7)
    eff_start = max(emp["joined_on"], month_start)
    working_days = _count_working_days(eff_start, month_end) if eff_start <= month_end else 0

    pipeline = [
        {"$match": {
            "emp_code": emp_code,
            "date":     {"$gte": month_start, "$lte": month_end},
        }},
        {"$group": {
            "_id": None,
            "present_days":          {"$sum": _PRESENT_VALUE_EXPR},
            "leave_days":            {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count":            {"$sum": {"$cond": [{"$gt":  [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes":    {"$sum": {"$ifNull": ["$late_minutes",     0]}},
            "total_overtime_minutes":{"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]
    rows = list(db.attendance_logs.aggregate(pipeline))
    r = rows[0] if rows else {}

    present_days           = r.get("present_days",           0.0)
    leave_days             = r.get("leave_days",             0)
    late_count             = r.get("late_count",             0)
    total_late_minutes     = r.get("total_late_minutes",     0)
    total_overtime_minutes = r.get("total_overtime_minutes", 0)

    attendance_pct = (
        _half_up(present_days / working_days * 100, 2) if working_days > 0 else None
    )

    return {
        "emp_code":               emp_code,
        "month":                  month,
        "working_days":           working_days,
        "present_days":           present_days,
        "leave_days":             leave_days,
        "late_count":             late_count,
        "total_late_minutes":     total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct":         attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month:      str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    _, _, month_start, month_end = _parse_month(month)

    # ── headcount per department (R9): joined_on <= month_end ──
    emp_match: dict = {"joined_on": {"$lte": month_end}}
    if department:
        emp_match["department"] = department

    hc_pipeline = [
        {"$match": emp_match},
        {"$group": {
            "_id":      "$department",
            "headcount":  {"$sum": 1},
            "emp_codes": {"$push": "$emp_code"},
        }},
    ]
    hc_rows = {r["_id"]: r for r in db.employees.aggregate(hc_pipeline)}
    if not hc_rows:
        return {"month": month, "items": []}

    all_emp_codes = [ec for r in hc_rows.values() for ec in r["emp_codes"]]

    # ── attendance stats per department ──
    stats_pipeline = [
        {"$match": {
            "emp_code": {"$in": all_emp_codes},
            "date":     {"$gte": month_start, "$lte": month_end},
        }},
        {"$lookup": {
            "from":         "employees",
            "localField":   "emp_code",
            "foreignField": "emp_code",
            "as":           "_emp",
            "pipeline":     [{"$project": {"department": 1, "_id": 0}}],
        }},
        {"$unwind": "$_emp"},
        {"$addFields": {"department": "$_emp.department"}},
        {"$group": {
            "_id":                "$department",
            "present_days":       {"$sum": _PRESENT_VALUE_EXPR},
            "wh_sum":             {"$sum": {
                "$cond": [
                    {"$and": [
                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$ne": [{"$ifNull": ["$work_hours", None]}, None]},
                    ]},
                    "$work_hours",
                    0,
                ]
            }},
            "wh_count":           {"$sum": {
                "$cond": [
                    {"$and": [
                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$ne": [{"$ifNull": ["$work_hours", None]}, None]},
                    ]},
                    1,
                    0,
                ]
            }},
            "late_count":         {"$sum": {"$cond": [{"$gt":  [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "leave_count":        {"$sum": {"$cond": [{"$eq":  ["$status", "LEAVE"]},    1, 0]}},
            "on_duty_count":      {"$sum": {"$cond": [{"$eq":  ["$status", "ON_DUTY"]},  1, 0]}},
        }},
    ]
    stats_rows = {r["_id"]: r for r in db.attendance_logs.aggregate(stats_pipeline)}

    items = []
    for dept in sorted(hc_rows):
        hc = hc_rows[dept]["headcount"]
        if hc == 0:
            continue
        st = stats_rows.get(dept, {})
        wh_sum   = st.get("wh_sum",   0)
        wh_count = st.get("wh_count", 0)
        avg_wh   = _half_up(wh_sum / wh_count, 2) if wh_count > 0 else None
        items.append({
            "department":         dept,
            "headcount":          hc,
            "present_days":       st.get("present_days",       0.0),
            "avg_work_hours":     avg_wh,
            "late_count":         st.get("late_count",         0),
            "total_late_minutes": st.get("total_late_minutes", 0),
            "leave_count":        st.get("leave_count",        0),
            "on_duty_count":      st.get("on_duty_count",      0),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month:      str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit:      int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    _, _, month_start, month_end = _parse_month(month)

    pipeline: list = [
        {"$match": {
            "date":          {"$gte": month_start, "$lte": month_end},
            "late_minutes":  {"$gt": 0},
        }},
        {"$group": {
            "_id":                "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count":         {"$sum": 1},
        }},
        # Join employees (orphan logs dropped by $unwind)
        {"$lookup": {
            "from":         "employees",
            "localField":   "_id",
            "foreignField": "emp_code",
            "as":           "_emp",
        }},
        {"$unwind": "$_emp"},
    ]

    if department:
        pipeline.append({"$match": {"_emp.department": department}})

    pipeline += [
        # Standard competition ranking via $setWindowFields ($rank)
        {"$setWindowFields": {
            "sortBy":  {"total_late_minutes": -1},
            "output":  {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort":  {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id":                0,
            "rank":               1,
            "emp_code":           "$_id",
            "name":               "$_emp.name",
            "department":         "$_emp.department",
            "total_late_minutes": 1,
            "late_count":         1,
        }},
    ]

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date:  str = Query(..., alias="from"),
    to_date:    str = Query(..., alias="to"),
):
    # Validation (422)
    if to_date < from_date:
        raise HTTPException(422, detail="'to' must be >= 'from'")
    from_d = date_type.fromisoformat(from_date)
    to_d   = date_type.fromisoformat(to_date)
    if (to_d - from_d).days > 91:   # >92 days means 93+ rows
        raise HTTPException(422, detail="Date range cannot exceed 92 days")

    # 404 if department unknown
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, detail="Department not found")

    # All employees in dept (for headcount per day)
    all_emp = list(db.employees.find({"department": department}, {"emp_code": 1, "joined_on": 1, "_id": 0}))

    # ── attendance aggregation with $densify gap-filling ──
    # Bounds for $densify must be datetime objects (UTC)
    densify_lo = datetime.fromisoformat(from_date)          # naive UTC midnight
    densify_hi = datetime.fromisoformat(to_date) + timedelta(days=1)

    pipeline = [
        {"$match": {
            "emp_code": {"$in": [e["emp_code"] for e in all_emp]},
            "date":     {"$gte": from_date, "$lte": to_date},
        }},
        # Group by date to get per-day attendance stats
        {"$group": {
            "_id": {"$dateFromString": {"dateString": "$date"}},
            "present_count": {"$sum": {
                "$cond": [
                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1.0]},
                    0.0,
                ]
            }},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        # Gap-fill missing calendar days
        {"$densify": {
            "field": "_id",
            "range": {"step": 1, "unit": "day", "bounds": [densify_lo, densify_hi]},
        }},
        {"$addFields": {
            "present_count": {"$ifNull": ["$present_count", 0.0]},
            "late_count":    {"$ifNull": ["$late_count",    0]},
        }},
        {"$sort": {"_id": 1}},
    ]

    rows = list(db.attendance_logs.aggregate(pipeline))

    # ── build result: headcount per day + attendance_rate + moving_avg_7d ──
    # Spec: moving_avg_7d = mean of non-null attendance_rate values among the
    # current row and up to 6 preceding rows within the requested range.
    # The window is over rows (not just rate-days); null-rate rows occupy a slot
    # but don't contribute to the mean.  Old rates fall out as rows age past 7.
    items = []
    # Each slot holds either a float (rate) or None
    row_window: list[Optional[float]] = []

    for row in rows:
        day_dt     = row["_id"]                         # naive UTC midnight of that date
        day_str    = day_dt.strftime("%Y-%m-%d")
        day_date   = date_type.fromisoformat(day_str)
        is_workday = day_date.weekday() < 5             # Mon=0, Fri=4

        # headcount = employees with joined_on <= this day (R9)
        headcount = sum(1 for e in all_emp if e["joined_on"] <= day_str)

        pc = row["present_count"]
        lc = row["late_count"]

        rate: Optional[float]
        if is_workday and headcount > 0:
            rate = _half_up(pc / headcount, 4)
        else:
            rate = None

        # Advance sliding window (max 7 rows)
        row_window.append(rate)
        if len(row_window) > 7:
            row_window.pop(0)

        non_null = [r for r in row_window if r is not None]
        moving_avg: Optional[float] = (
            _half_up(sum(non_null) / len(non_null), 4) if non_null else None
        )

        items.append({
            "date":            day_str,
            "is_working_day":  is_workday,
            "headcount":       headcount,
            "present_count":   pc,
            "late_count":      lc,
            "attendance_rate": rate,
            "moving_avg_7d":   moving_avg,
        })

    return {"department": department, "items": items}


# ===========================================================================
# ENDPOINT – admin explain
# ===========================================================================

def _build_attendance_list_query(
    emp_code: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    status: Optional[str],
    page: int,
    page_size: int,
) -> tuple[dict, list, int]:
    """Returns (filter_query, sort, skip)."""
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q, [("date", DESCENDING), ("emp_code", ASCENDING)], (page - 1) * page_size


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint:   str,
    emp_code:   Optional[str] = None,
    month:      Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit:      int           = Query(default=10, ge=1, le=50),
    date_from:  Optional[str] = None,
    date_to:    Optional[str] = None,
    status:     Optional[str] = None,
    from_date:  Optional[str] = Query(default=None, alias="from"),
    to_date:    Optional[str] = Query(default=None, alias="to"),
    page:       int           = Query(default=1,  ge=1),
    page_size:  int           = Query(default=20, ge=1, le=100),
):
    VALID = {
        "attendance_list", "employee_monthly", "department_summary",
        "late_leaderboard", "department_trend",
    }
    if endpoint not in VALID:
        raise HTTPException(422, detail=f"endpoint must be one of {sorted(VALID)}")

    # ---------- attendance_list ----------
    if endpoint == "attendance_list":
        q, srt, skip = _build_attendance_list_query(
            emp_code, date_from, date_to, status, page, page_size
        )
        explained = db.command(
            "explain",
            {
                "find":        "attendance_logs",
                "filter":      q,
                "sort":        dict(srt),
                "skip":        skip,
                "limit":       page_size,
                "projection":  {"_id": 0},
            },
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explained}

    # ---------- employee_monthly ----------
    if endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(422, detail="emp_code is required for employee_monthly")
        if not month:
            raise HTTPException(422, detail="month is required for employee_monthly")
        _, _, month_start, month_end = _parse_month(month)
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
            {"$group": {"_id": None, "total": {"$sum": 1}}},
        ]
        explained = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explained}

    # ---------- department_summary ----------
    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, detail="month is required for department_summary")
        _, _, month_start, month_end = _parse_month(month)
        emp_match: dict = {"joined_on": {"$lte": month_end}}
        if department:
            emp_match["department"] = department
        pipeline = [
            {"$match": emp_match},
            {"$group": {"_id": "$department", "headcount": {"$sum": 1}}},
        ]
        explained = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "employees", "explain": explained}

    # ---------- late_leaderboard ----------
    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, detail="month is required for late_leaderboard")
        _, _, month_start, month_end = _parse_month(month)
        pipeline = [
            {"$match": {
                "date":         {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0},
            }},
            {"$group": {
                "_id":                "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count":         {"$sum": 1},
            }},
        ]
        explained = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explained}

    # ---------- department_trend ----------
    if endpoint == "department_trend":
        if not from_date or not to_date:
            raise HTTPException(422, detail="'from' and 'to' are required for department_trend")
        if not department:
            raise HTTPException(422, detail="department is required for department_trend")
        if to_date < from_date:
            raise HTTPException(422, detail="'to' must be >= 'from'")
        densify_lo = datetime.fromisoformat(from_date)
        densify_hi = datetime.fromisoformat(to_date) + timedelta(days=1)
        all_emp = list(
            db.employees.find({"department": department}, {"emp_code": 1, "_id": 0})
        )
        pipeline = [
            {"$match": {
                "emp_code": {"$in": [e["emp_code"] for e in all_emp]},
                "date":     {"$gte": from_date, "$lte": to_date},
            }},
            {"$group": {
                "_id":          {"$dateFromString": {"dateString": "$date"}},
                "present_count": {"$sum": 1},
            }},
            {"$densify": {
                "field": "_id",
                "range": {"step": 1, "unit": "day", "bounds": [densify_lo, densify_hi]},
            }},
        ]
        explained = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explained}
