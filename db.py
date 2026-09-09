"""SQLite アクセスと集計ロジック。"""
import calendar
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent / "attendance.db"

DEFAULT_EMPLOYEES = [
    "木村秀美", "三好", "大須賀", "塩井", "松村",
    "矢嶋", "樋笠正直", "樋笠正道", "樋笠幸三", "木村勲", "佐々木",
]

DEFAULT_SETTINGS = {
    "closing_day": "20",
    "standard_daily_hours": "8",
}

RECORD_FIELDS = [
    "late_hours", "early_leave_hours", "overtime_hours",
    "holiday_work_hours", "paid_leave_hours", "absence_hours",
    "total_hours", "job_content", "travel_hours", "note",
]


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS employees (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                sort_order INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS holidays (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS daily_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id INTEGER NOT NULL REFERENCES employees(id),
                date TEXT NOT NULL,
                late_hours REAL NOT NULL DEFAULT 0,
                early_leave_hours REAL NOT NULL DEFAULT 0,
                overtime_hours REAL NOT NULL DEFAULT 0,
                holiday_work_hours REAL NOT NULL DEFAULT 0,
                paid_leave_hours REAL NOT NULL DEFAULT 0,
                absence_hours REAL NOT NULL DEFAULT 0,
                total_hours REAL NOT NULL DEFAULT 0,
                job_content TEXT NOT NULL DEFAULT '',
                travel_hours REAL NOT NULL DEFAULT 0,
                note TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                UNIQUE(employee_id, date)
            );
            """
        )

        # 移行: 旧バージョンでは現場往復を「分」で保持していた列(travel_minutes)が
        # 残っている場合、時間(travel_hours)に変換して引き継ぐ。
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(daily_records)")}
        if "travel_minutes" in cols and "travel_hours" not in cols:
            conn.execute("ALTER TABLE daily_records ADD COLUMN travel_hours REAL NOT NULL DEFAULT 0")
            conn.execute("UPDATE daily_records SET travel_hours = ROUND(travel_minutes / 60.0, 2)")

        if conn.execute("SELECT COUNT(*) FROM employees").fetchone()[0] == 0:
            for i, name in enumerate(DEFAULT_EMPLOYEES):
                conn.execute(
                    "INSERT INTO employees (name, sort_order, active) VALUES (?, ?, 1)",
                    (name, i),
                )

        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )


# ---------- settings ----------

def get_setting(key, default=None):
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def get_closing_day():
    return int(get_setting("closing_day", "20"))


def get_standard_daily_hours():
    return float(get_setting("standard_daily_hours", "8"))


def set_setting(key, value):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


# ---------- employees ----------

def list_employees(active_only=True):
    with get_conn() as conn:
        q = "SELECT * FROM employees"
        if active_only:
            q += " WHERE active = 1"
        q += " ORDER BY sort_order, id"
        return [dict(r) for r in conn.execute(q).fetchall()]


def get_employee(employee_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM employees WHERE id = ?", (employee_id,)).fetchone()
        return dict(row) if row else None


def add_employee(name):
    with get_conn() as conn:
        max_order = conn.execute("SELECT COALESCE(MAX(sort_order), -1) FROM employees").fetchone()[0]
        conn.execute(
            "INSERT INTO employees (name, sort_order, active) VALUES (?, ?, 1)",
            (name.strip(), max_order + 1),
        )


def set_employee_active(employee_id, active):
    with get_conn() as conn:
        conn.execute("UPDATE employees SET active = ? WHERE id = ?", (1 if active else 0, employee_id))


# ---------- holidays ----------

def list_holidays(year=None, month=None):
    with get_conn() as conn:
        if year and month:
            prefix = f"{year:04d}-{month:02d}-"
            rows = conn.execute(
                "SELECT * FROM holidays WHERE date LIKE ? ORDER BY date", (prefix + "%",)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM holidays ORDER BY date").fetchall()
        return [dict(r) for r in rows]


def holiday_dates_in_range(start: date, end: date):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT date FROM holidays WHERE date BETWEEN ? AND ?",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        return {r["date"] for r in rows}


def add_holiday(d: date, name: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO holidays (date, name) VALUES (?, ?)",
            (d.isoformat(), name),
        )


def add_holiday_range(start: date, end: date, name: str):
    with get_conn() as conn:
        d = start
        while d <= end:
            conn.execute(
                "INSERT OR IGNORE INTO holidays (date, name) VALUES (?, ?)",
                (d.isoformat(), name),
            )
            d += timedelta(days=1)


def delete_holiday(holiday_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM holidays WHERE id = ?", (holiday_id,))


# ---------- reporting period logic ----------

def _clamp_day(year, month, day):
    last = calendar.monthrange(year, month)[1]
    return min(day, last)


def get_reporting_period(d: date, closing_day: int):
    """遅刻・早退・早出残業・有休・欠勤・休出 の集計に使う『締め月』を返す。

    d.day <= closing_day ならその月が締め月、それより後なら翌月が締め月。
    戻り値: (締め月ラベル "YYYY-MM", 期間開始日, 期間終了日)
    """
    if d.day <= closing_day:
        rep_year, rep_month = d.year, d.month
    else:
        rep_year, rep_month = d.year, d.month
        rep_month += 1
        if rep_month == 13:
            rep_month = 1
            rep_year += 1

    period_end = date(rep_year, rep_month, _clamp_day(rep_year, rep_month, closing_day))

    prev_month = rep_month - 1
    prev_year = rep_year
    if prev_month == 0:
        prev_month = 12
        prev_year -= 1
    period_start = date(prev_year, prev_month, _clamp_day(prev_year, prev_month, closing_day + 1))

    label = f"{rep_year:04d}-{rep_month:02d}"
    return label, period_start, period_end


def reporting_period_for_label(year: int, month: int, closing_day: int):
    """締め月ラベル(year, month)から、その集計期間の開始日・終了日を返す。"""
    period_end = date(year, month, _clamp_day(year, month, closing_day))
    prev_month = month - 1
    prev_year = year
    if prev_month == 0:
        prev_month = 12
        prev_year -= 1
    period_start = date(prev_year, prev_month, _clamp_day(prev_year, prev_month, closing_day + 1))
    return period_start, period_end


def calendar_month_label(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


# ---------- daily records ----------

def calc_total_hours(standard_hours, late, early_leave, absence, overtime, holiday_work):
    total = standard_hours - late - early_leave - absence + overtime + holiday_work
    return max(0.0, round(total, 2))


def get_records_for_month(employee_id, year, month):
    """カレンダー月(1日〜末日)の日別レコードを日付文字列キーの辞書で返す。"""
    start = date(year, month, 1)
    end = date(year, month, calendar.monthrange(year, month)[1])
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM daily_records WHERE employee_id = ? AND date BETWEEN ? AND ?",
            (employee_id, start.isoformat(), end.isoformat()),
        ).fetchall()
    return {r["date"]: dict(r) for r in rows}


def upsert_record(employee_id, d: date, values: dict):
    """values には RECORD_FIELDS のうち送られてきたキーのみ入っている想定。"""
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT * FROM daily_records WHERE employee_id = ? AND date = ?",
            (employee_id, d.isoformat()),
        ).fetchone()
        merged = dict(existing) if existing else {f: (0 if f not in ("job_content", "note") else "") for f in RECORD_FIELDS}
        merged.update(values)

        now = datetime.now().isoformat(timespec="seconds")
        if existing:
            conn.execute(
                f"""UPDATE daily_records SET
                    late_hours = ?, early_leave_hours = ?, overtime_hours = ?,
                    holiday_work_hours = ?, paid_leave_hours = ?, absence_hours = ?,
                    total_hours = ?, job_content = ?, travel_hours = ?, note = ?,
                    updated_at = ?
                    WHERE employee_id = ? AND date = ?""",
                (
                    merged["late_hours"], merged["early_leave_hours"], merged["overtime_hours"],
                    merged["holiday_work_hours"], merged["paid_leave_hours"], merged["absence_hours"],
                    merged["total_hours"], merged["job_content"], merged["travel_hours"], merged["note"],
                    now, employee_id, d.isoformat(),
                ),
            )
        else:
            conn.execute(
                f"""INSERT INTO daily_records
                    (employee_id, date, late_hours, early_leave_hours, overtime_hours,
                     holiday_work_hours, paid_leave_hours, absence_hours, total_hours,
                     job_content, travel_hours, note, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    employee_id, d.isoformat(),
                    merged["late_hours"], merged["early_leave_hours"], merged["overtime_hours"],
                    merged["holiday_work_hours"], merged["paid_leave_hours"], merged["absence_hours"],
                    merged["total_hours"], merged["job_content"], merged["travel_hours"], merged["note"],
                    now,
                ),
            )
        return merged


# ---------- summary ----------

CLOSING_TRACKED_FIELDS = [
    "late_hours", "early_leave_hours", "overtime_hours",
    "holiday_work_hours", "paid_leave_hours", "absence_hours", "total_hours",
]


def summarize_reporting_period(employee_id, year, month, closing_day):
    """締め期間((closing_day+1)日〜翌月closing_day日)の 遅刻/早退/早出残業/休出/有休/欠勤/時間計 合計。"""
    start, end = reporting_period_for_label(year, month, closing_day)
    with get_conn() as conn:
        row = conn.execute(
            f"""SELECT
                    COALESCE(SUM(late_hours), 0) AS late_hours,
                    COALESCE(SUM(early_leave_hours), 0) AS early_leave_hours,
                    COALESCE(SUM(overtime_hours), 0) AS overtime_hours,
                    COALESCE(SUM(holiday_work_hours), 0) AS holiday_work_hours,
                    COALESCE(SUM(paid_leave_hours), 0) AS paid_leave_hours,
                    COALESCE(SUM(absence_hours), 0) AS absence_hours,
                    COALESCE(SUM(total_hours), 0) AS total_hours
                FROM daily_records
                WHERE employee_id = ? AND date BETWEEN ? AND ?""",
            (employee_id, start.isoformat(), end.isoformat()),
        ).fetchone()
    result = dict(row)
    result["period_start"] = start.isoformat()
    result["period_end"] = end.isoformat()
    return result


def summarize_calendar_travel(employee_id, year, month):
    """現場往復(勤務時間外往復)は暦月(1日〜末日)集計、繰り越さない。"""
    start = date(year, month, 1)
    end = date(year, month, calendar.monthrange(year, month)[1])
    with get_conn() as conn:
        row = conn.execute(
            """SELECT COALESCE(SUM(travel_hours), 0) AS travel_hours
               FROM daily_records WHERE employee_id = ? AND date BETWEEN ? AND ?""",
            (employee_id, start.isoformat(), end.isoformat()),
        ).fetchone()
    return row["travel_hours"]
