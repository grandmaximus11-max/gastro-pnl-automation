"""Чистая логика скана целостности P&L. Без сети, без gspread.

pnl_row = (row_number:int, cells:list[str]); cells = A..V (0-indexed).
na_kontrolu_row = (row_number:int, cells:list[str]); 10-колоночная Na kontrolu.
"""
import re
from datetime import date, timedelta

COL_B, COL_C, COL_D, COL_H = 1, 2, 3, 7
COL_I, COL_K, COL_Q, COL_U, COL_V = 8, 10, 16, 20, 21
LONG_UNPAID_DAYS = 30
LATE_PAID_DAYS = 30
SOON_DAYS = 3


def _cell(cells, idx):
    return cells[idx].strip() if len(cells) > idx and cells[idx] else ""


def _parse_date(s):
    s = (s or "").strip().lstrip("✅").strip()
    m = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$", s)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    m = re.match(r"^(\d{1,2})\.(\d{1,2})$", s)
    if m:
        try:
            return date(date.today().year, int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    return None


def _is_unpaid(cells):
    return _cell(cells, COL_U).lower() != "zaplaceno"


def scan_anomalies(pnl_rows, na_kontrolu_rows, today, period_start=None):
    """Скан аномалий P&L.

    period_start (date|None): если задан — учитываем только строки с датой
    операции (C) >= period_start. Строки без распарсенной даты C тоже
    пропускаются. Это режим forward-only: легаси до go-live игнорируется,
    дашборд не тонет в старых хвостах.
    """
    out = {"overdue": [], "soon": [], "long_unpaid": [],
           "late_paid": [], "na_kontrolu": [], "no_document": []}

    for num, cells in pnl_rows:
        amount = _cell(cells, COL_D)
        desc = _cell(cells, COL_H) or "—"
        due = _parse_date(_cell(cells, COL_B))
        op_date = _parse_date(_cell(cells, COL_C))
        unpaid = _is_unpaid(cells)

        # forward-only: пропускаем легаси (до go-live или без даты операции)
        if period_start is not None and (op_date is None or op_date < period_start):
            continue

        if unpaid and due:
            if due < today:
                out["overdue"].append({"row": num, "amount": amount, "desc": desc,
                                        "due": _cell(cells, COL_B), "days_over": (today - due).days})
            elif today < due <= today + timedelta(days=SOON_DAYS):
                out["soon"].append({"row": num, "amount": amount, "desc": desc,
                                    "due": _cell(cells, COL_B), "days_left": (due - today).days})

        if unpaid and op_date and (today - op_date).days > LONG_UNPAID_DAYS:
            out["long_unpaid"].append({"row": num, "amount": amount, "desc": desc,
                                       "op_date": _cell(cells, COL_C),
                                       "days_old": (today - op_date).days})

        if not unpaid and op_date:
            v_date = _parse_date(_cell(cells, COL_V))
            if v_date and (v_date - op_date).days > LATE_PAID_DAYS:
                out["late_paid"].append({"row": num, "amount": amount, "desc": desc,
                                         "lag_days": (v_date - op_date).days})

        if amount and not _cell(cells, COL_I) and not _cell(cells, COL_K):
            out["no_document"].append({"row": num, "amount": amount, "desc": desc,
                                       "op_date": _cell(cells, COL_C)})

    for num, cells in na_kontrolu_rows:
        out["na_kontrolu"].append({"row": num, "amount": _cell(cells, 1),
                                   "who": _cell(cells, 3) or "—", "datum": _cell(cells, 0)})

    return out
