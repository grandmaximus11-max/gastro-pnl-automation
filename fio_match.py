"""Чистая логика FIO-матчинга. Без сети, без gspread — только данные.

row = (row_number:int, cells:list[str])  где cells — A..V (0-indexed).
Колонки: 1=B 2=C 3=D 7=H 16=Q 18=S(VS) 19=T(doklad) 20=U(stav) 21=V.
"""
from datetime import date, timedelta
import re

COL_B, COL_C, COL_D, COL_H = 1, 2, 3, 7
COL_Q, COL_S, COL_T, COL_U, COL_V = 16, 18, 19, 20, 21
WINDOW_DAYS = 90


def _amount(s):
    cleaned = re.sub(r"[^\d.]", "", (s or "").replace(",", ".").replace("\xa0", ""))
    try:
        return round(float(cleaned))
    except (ValueError, TypeError):
        return None


def _cell(cells, idx):
    return cells[idx].strip() if len(cells) > idx and cells[idx] else ""


def _is_unpaid(cells):
    return _cell(cells, COL_U).lower() != "zaplaceno"


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


def group_unpaid_by_doklad(rows):
    groups = {}
    for num, cells in rows:
        if not _is_unpaid(cells):
            continue
        t = _cell(cells, COL_T)
        if not t:
            continue
        amt = _amount(_cell(cells, COL_D)) or 0
        g = groups.setdefault(t, {"total": 0, "row_numbers": []})
        g["total"] += amt
        g["row_numbers"].append(num)
    return groups


def match_payment(payment, rows):
    def orphan():
        return {"kind": "orphan", "row_numbers": []}

    if payment.get("signed", 0) >= 0:
        return orphan()

    pay_amount = _amount(payment["amount"])
    pay_vs = (payment.get("var_symbol") or "").strip()
    pay_dod = (payment.get("dodavatel") or "").strip().lower()
    pay_date = payment["date"]

    unpaid = [(n, c) for (n, c) in rows if _is_unpaid(c)]

    # Phase 0: VS exact
    if pay_vs:
        hits = [n for (n, c) in unpaid if _cell(c, COL_S) and _cell(c, COL_S) == pay_vs]
        if len(hits) == 1:
            return {"kind": "paid", "row_numbers": hits}
        if len(hits) >= 2:
            return {"kind": "ambiguous", "row_numbers": hits}

    def vs_guard_ok(cells):
        row_vs = _cell(cells, COL_S)
        return not (row_vs and pay_vs and row_vs != pay_vs)

    # Phase 1: číslo dokladu group (respect VS guard)
    vs_ok_unpaid = [(n, c) for (n, c) in unpaid if vs_guard_ok(c)]
    groups = group_unpaid_by_doklad(vs_ok_unpaid)
    t_hits = [g["row_numbers"] for g in groups.values() if g["total"] == pay_amount]
    if len(t_hits) == 1:
        return {"kind": "paid", "row_numbers": sorted(t_hits[0])}
    if len(t_hits) >= 2:
        return {"kind": "ambiguous", "row_numbers": sorted(sum(t_hits, []))}

    def amount_ok(cells):
        return _amount(_cell(cells, COL_D)) == pay_amount

    def within_window(cells):
        c = _parse_date(_cell(cells, COL_C)) or _parse_date(_cell(cells, COL_B))
        if not c:
            return True
        return abs((pay_date - c).days) <= WINDOW_DAYS

    base = [
        (n, c) for (n, c) in unpaid
        if not _cell(c, COL_T) and amount_ok(c) and vs_guard_ok(c) and within_window(c)
    ]

    # Phase 2: dodavatel + amount
    if pay_dod:
        dod_hits = [n for (n, c) in base if _cell(c, COL_Q).lower() == pay_dod]
        if len(dod_hits) == 1:
            return {"kind": "paid", "row_numbers": dod_hits}
        if len(dod_hits) >= 2:
            return {"kind": "ambiguous", "row_numbers": dod_hits}

    # Phase 3: amount only
    amt_hits = [n for (n, c) in base]
    if len(amt_hits) == 1:
        return {"kind": "paid", "row_numbers": amt_hits}
    if len(amt_hits) >= 2:
        return {"kind": "ambiguous", "row_numbers": amt_hits}

    return orphan()
