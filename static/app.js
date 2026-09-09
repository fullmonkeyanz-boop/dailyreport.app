function initEntryTable(apiUrl, employeeId) {
  const table = document.getElementById('entry-table');
  if (!table) return;
  const standardHours = parseFloat(table.dataset.standardHours || '8');
  const indicator = document.getElementById('save-indicator');

  function num(row, field) {
    const el = row.querySelector(`[data-field="${field}"]`);
    return el ? (parseFloat(el.value) || 0) : 0;
  }

  function recalcTotal(row) {
    const late = num(row, 'late_hours');
    const earlyLeave = num(row, 'early_leave_hours');
    const absence = num(row, 'absence_hours');
    const overtime = num(row, 'overtime_hours');
    const holidayWork = num(row, 'holiday_work_hours');
    let total = standardHours - late - earlyLeave - absence + overtime + holidayWork;
    if (total < 0) total = 0;
    total = Math.round(total * 100) / 100;
    const totalEl = row.querySelector('[data-field="total_hours"]');
    if (totalEl) totalEl.value = total;
  }

  function collectRow(row) {
    const fields = [
      'paid_leave_hours', 'late_hours', 'early_leave_hours', 'absence_hours',
      'overtime_hours', 'holiday_work_hours', 'total_hours',
      'travel_hours', 'job_content', 'note',
    ];
    const payload = { employee_id: employeeId, date: row.dataset.date };
    fields.forEach((f) => {
      const el = row.querySelector(`[data-field="${f}"]`);
      payload[f] = el ? el.value : '';
    });
    return payload;
  }

  function flashSaved(row) {
    row.classList.add('saved-flash');
    if (indicator) {
      indicator.textContent = '保存しました (' + row.dataset.date + ')';
      setTimeout(() => { indicator.textContent = ''; }, 2000);
    }
    setTimeout(() => row.classList.remove('saved-flash'), 400);
  }

  function saveRow(row) {
    fetch(apiUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(collectRow(row)),
    })
      .then((res) => res.json())
      .then(() => flashSaved(row))
      .catch(() => {
        if (indicator) indicator.textContent = '保存に失敗しました';
      });
  }

  table.querySelectorAll('tbody tr').forEach((row) => {
    row.querySelectorAll('.cell').forEach((input) => {
      input.addEventListener('change', () => {
        if (input.dataset.field !== 'total_hours') {
          recalcTotal(row);
        }
        saveRow(row);
      });
    });
  });
}
