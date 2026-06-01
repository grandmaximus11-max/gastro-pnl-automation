"""Чистая логика дедупликации записей P&L. Без сети, без gspread.

row = (row_number:int, cells:list[str]); cells = A..V (0-indexed).
"""
import re
from datetime import date

COL_A, COL_C, COL_D, COL_H = 0, 2, 3, 7
COL_K, COL_N, COL_O, COL_P = 10, 13, 14, 15
COL_Q, COL_S, COL_T = 16, 18, 19
WINDOW_DAYS = 3


def _amount(s):
    cleaned = re.sub(r"[^\d.]", "", (s or "").replace(",", ".").replace("\xa0", ""))
    try:
        return round(float(cleaned))
    except (ValueError, TypeError):
        return None


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


def _enrichment_fields(new: dict, cells: list) -> dict:
    """Поля, которые new добавляет к старой строке (которых там пусто). {col_index: value}."""
    out = {}
    candidates = [
        (COL_S, str(new.get("var_symbol", "")).strip()),
        (COL_T, str(new.get("cislo_dokladu", "")).strip()),
        (COL_K, str(new.get("drive_url", "")).strip()),
        (COL_Q, str(new.get("dodavatel", "")).strip()),
        (COL_N, str(new.get("sazba_dph", "")).strip()),
        (COL_O, str(new.get("zaklad_dph", "")).strip()),
        (COL_P, str(new.get("dph", "")).strip()),
    ]
    for idx, val in candidates:
        if val and not _cell(cells, idx):
            out[idx] = val
    return out


def classify_entry(new: dict, rows: list) -> dict:
    new_vs = str(new.get("var_symbol", "")).strip()
    new_doklad = str(new.get("cislo_dokladu", "")).strip()

    # ── strong: VS / číslo dokladu ──
    for num, cells in rows:
        if new_vs and _cell(cells, COL_S) == new_vs:
            return {"kind": "strong_dup", "row_number": num, "matched_on": "vs"}
        if new_doklad and _cell(cells, COL_T) == new_doklad:
            return {"kind": "strong_dup", "row_number": num, "matched_on": "doklad"}

    # ── weak: amount + (dodavatel OR date±window), VS-guard ──
    new_amount = _amount(new.get("amount", ""))
    new_date = _parse_date(new.get("date", ""))
    new_dod = str(new.get("dodavatel", "")).strip().lower()

    for num, cells in rows:
        if new_amount is None or _amount(_cell(cells, COL_D)) != new_amount:
            continue
        row_vs = _cell(cells, COL_S)
        if new_vs and row_vs and new_vs != row_vs:
            continue
        row_dod = _cell(cells, COL_Q).lower()
        row_date = _parse_date(_cell(cells, COL_C))
        dod_ok = bool(new_dod and row_dod and new_dod == row_dod)
        date_ok = bool(new_date and row_date and abs((new_date - row_date).days) <= WINDOW_DAYS)
        if not (dod_ok or date_ok):
            continue
        missing = _enrichment_fields(new, cells)
        if missing:
            return {"kind": "weak_enrich", "row_number": num, "missing": missing}
        return {"kind": "weak_no_enrich", "row_number": num}

    return {"kind": "clean"}
