"""Authentication & authorization for kasa_bot — pure logic over employee dicts."""
from __future__ import annotations

import secrets
from datetime import datetime
from typing import Iterable

ROLE_ORDER = {"bartender": 1, "manager": 2, "majitel": 3}


def _norm_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().upper() in ("TRUE", "1", "YES", "ANO")


def find_by_tg_user_id(rows: Iterable[dict], tg_user_id: int) -> dict | None:
    for r in rows:
        try:
            if int(r.get("tg_user_id") or 0) == tg_user_id:
                return r
        except (TypeError, ValueError):
            continue
    return None


def validate_activation_code(rows: Iterable[dict], code: str) -> dict | None:
    """Find employee whose unused activation code matches. Empty codes are not matched."""
    code = code.strip()
    if not code:
        return None
    for r in rows:
        stored = (r.get("aktivacni_kod") or "").strip()
        if stored and stored == code:
            return r
    return None


def is_blocked(emp: dict) -> bool:
    raw = (emp.get("zablokovan_do") or "").strip()
    if not raw:
        return False
    try:
        until = datetime.strptime(raw, "%d.%m.%Y %H:%M")
    except ValueError:
        return False
    return datetime.now() < until


def has_role(emp: dict, required: str) -> bool:
    return ROLE_ORDER.get(emp.get("role", ""), 0) >= ROLE_ORDER.get(required, 99)


def is_active(emp: dict) -> bool:
    return _norm_bool(emp.get("aktivni"))


def generate_activation_code(jmeno: str) -> str:
    """DEMO-<JMENO>-<4 random digits>. Jmeno upper-cased and ASCII-only."""
    import unicodedata
    safe = unicodedata.normalize("NFKD", jmeno).encode("ascii", "ignore").decode().upper()
    rand = secrets.randbelow(10_000)
    return f"DEMO-{safe}-{rand:04d}"
