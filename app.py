"""勤怠管理アプリ Flask エントリポイント。

起動: python app.py
社内LANの他PCからは http://<このPCのIPアドレス>:5000 でアクセス可能。
"""
import calendar
import socket
from datetime import date

from flask import Flask, jsonify, redirect, render_template, request, url_for

import db

app = Flask(__name__)


@app.before_request
def _ensure_db():
    db.init_db()


def _prev_next_month(year, month):
    prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
    return (prev_year, prev_month), (next_year, next_month)


@app.route("/")
def index():
    employees = db.list_employees()
    today = date.today()
    return render_template("index.html", employees=employees, today=today)


@app.route("/entry/<int:employee_id>/<int:year>/<int:month>")
def entry(employee_id, year, month):
    employee = db.get_employee(employee_id)
    if not employee:
        return redirect(url_for("index"))

    closing_day = db.get_closing_day()
    standard_hours = db.get_standard_daily_hours()
    records = db.get_records_for_month(employee_id, year, month)
    days_in_month = calendar.monthrange(year, month)[1]
    holiday_dates = db.holiday_dates_in_range(date(year, month, 1), date(year, month, days_in_month))

    weekday_names = ["月", "火", "水", "木", "金", "土", "日"]
    rows = []
    for day in range(1, days_in_month + 1):
        d = date(year, month, day)
        iso = d.isoformat()
        rec = records.get(iso, {})
        label, _, _ = db.get_reporting_period(d, closing_day)
        rows.append({
            "day": day,
            "iso": iso,
            "weekday": weekday_names[d.weekday()],
            "is_weekend": d.weekday() >= 5,
            "is_holiday": iso in holiday_dates,
            "is_closing_day": day == closing_day,
            "reporting_label": label,
            "late_hours": rec.get("late_hours", 0),
            "early_leave_hours": rec.get("early_leave_hours", 0),
            "overtime_hours": rec.get("overtime_hours", 0),
            "holiday_work_hours": rec.get("holiday_work_hours", 0),
            "paid_leave_hours": rec.get("paid_leave_hours", 0),
            "absence_hours": rec.get("absence_hours", 0),
            "total_hours": rec.get("total_hours", 0),
            "job_content": rec.get("job_content", ""),
            "travel_hours": rec.get("travel_hours", 0),
            "note": rec.get("note", ""),
        })

    (py, pm), (ny, nm) = _prev_next_month(year, month)

    return render_template(
        "entry.html",
        employee=employee, year=year, month=month, rows=rows,
        closing_day=closing_day, standard_hours=standard_hours,
        prev_year=py, prev_month=pm, next_year=ny, next_month=nm,
    )


@app.route("/api/record", methods=["POST"])
def api_record():
    data = request.get_json(force=True)
    employee_id = int(data["employee_id"])
    d = date.fromisoformat(data["date"])

    def f(key):
        try:
            return round(float(data.get(key, 0) or 0), 2)
        except (TypeError, ValueError):
            return 0.0

    values = {
        "late_hours": f("late_hours"),
        "early_leave_hours": f("early_leave_hours"),
        "overtime_hours": f("overtime_hours"),
        "holiday_work_hours": f("holiday_work_hours"),
        "paid_leave_hours": f("paid_leave_hours"),
        "absence_hours": f("absence_hours"),
        "total_hours": f("total_hours"),
        "job_content": str(data.get("job_content", "") or ""),
        "travel_hours": f("travel_hours"),
        "note": str(data.get("note", "") or ""),
    }
    merged = db.upsert_record(employee_id, d, values)
    return jsonify({"ok": True, "total_hours": merged["total_hours"]})


@app.route("/summary")
def summary():
    today = date.today()
    year = request.args.get("year", type=int) or today.year
    month = request.args.get("month", type=int) or today.month
    employee_id = request.args.get("employee_id", default="all")

    employees = db.list_employees()
    closing_day = db.get_closing_day()

    if employee_id == "all":
        targets = employees
    else:
        emp = db.get_employee(int(employee_id))
        targets = [emp] if emp else []

    results = []
    for emp in targets:
        rep = db.summarize_reporting_period(emp["id"], year, month, closing_day)
        travel_hours = db.summarize_calendar_travel(emp["id"], year, month)
        results.append({"employee": emp, "reporting": rep, "travel_hours": travel_hours})

    (py, pm), (ny, nm) = _prev_next_month(year, month)

    return render_template(
        "summary.html",
        employees=employees, results=results, year=year, month=month,
        employee_id=employee_id, closing_day=closing_day,
        prev_year=py, prev_month=pm, next_year=ny, next_month=nm,
    )


@app.route("/settings")
def settings():
    employees = db.list_employees(active_only=False)
    holidays = db.list_holidays()
    closing_day = db.get_closing_day()
    standard_hours = db.get_standard_daily_hours()
    return render_template(
        "settings.html", employees=employees, holidays=holidays,
        closing_day=closing_day, standard_hours=standard_hours,
    )


@app.route("/settings/general", methods=["POST"])
def settings_general():
    closing_day = request.form.get("closing_day", type=int)
    standard_hours = request.form.get("standard_daily_hours", type=float)
    if closing_day and 1 <= closing_day <= 31:
        db.set_setting("closing_day", closing_day)
    if standard_hours and standard_hours > 0:
        db.set_setting("standard_daily_hours", standard_hours)
    return redirect(url_for("settings"))


@app.route("/settings/holidays/add", methods=["POST"])
def settings_holidays_add():
    d = request.form.get("date")
    name = request.form.get("name", "")
    if d:
        db.add_holiday(date.fromisoformat(d), name)
    return redirect(url_for("settings"))


@app.route("/settings/holidays/add_range", methods=["POST"])
def settings_holidays_add_range():
    start = request.form.get("start_date")
    end = request.form.get("end_date")
    name = request.form.get("range_name", "")
    if start and end:
        start_d, end_d = date.fromisoformat(start), date.fromisoformat(end)
        if start_d <= end_d:
            db.add_holiday_range(start_d, end_d, name)
    return redirect(url_for("settings"))


@app.route("/settings/holidays/delete/<int:holiday_id>", methods=["POST"])
def settings_holidays_delete(holiday_id):
    db.delete_holiday(holiday_id)
    return redirect(url_for("settings"))


@app.route("/settings/employees/add", methods=["POST"])
def settings_employees_add():
    name = request.form.get("name", "").strip()
    if name:
        db.add_employee(name)
    return redirect(url_for("settings"))


@app.route("/settings/employees/toggle/<int:employee_id>", methods=["POST"])
def settings_employees_toggle(employee_id):
    emp = db.get_employee(employee_id)
    if emp:
        db.set_employee_active(employee_id, not emp["active"])
    return redirect(url_for("settings"))


def _local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


if __name__ == "__main__":
    db.init_db()
    ip = _local_ip()
    print("=" * 60)
    print(" 勤怠管理アプリを起動しました")
    print(f" このPCから:      http://localhost:5000")
    print(f" 他のPCから(LAN): http://{ip}:5000")
    print("=" * 60)
    app.run(host="0.0.0.0", port=5000, debug=False)
