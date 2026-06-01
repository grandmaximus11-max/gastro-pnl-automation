"""Pure calculations — no I/O, no Telegram. Given a shift-data dict, compute everything."""
from collections import defaultdict
from datetime import datetime as _dt

# Czech weekday abbreviations, Mon(0)..Sun(6) — bot UI is Czech (CLAUDE.md).
_CZ_DOW = ["Po", "Út", "St", "Čt", "Pá", "So", "Ne"]


def czech_dow(datum_str: str) -> str:
    """DD.MM.YYYY → Czech weekday abbreviation (Po/Út/St/Čt/Pá/So/Ne)."""
    try:
        return _CZ_DOW[_dt.strptime(datum_str, "%d.%m.%Y").weekday()]
    except (ValueError, TypeError):
        return "?"


def expected_konec(smena: dict) -> int:
    """How much cash *should* be in the till at end of shift.

    Formula:
        hot_zac + trzba_pos_hot − naklady_celkem − zalohy_neuhrazene

    E-shop cash is NOT subtracted: e-shop is rung through Dotykačka, so its cash
    is already inside `trzba_pos_hot` AND physically stays in the till.
    Subtracting it would double-remove real cash and create a phantom surplus
    equal to the e-shop cash (a false tip/přebytek). `eshop_hotove` is still
    tracked separately for P&L income (PESH) and the bar-revenue split — just
    not in this physical-cash reconciliation.
    """
    return (
        int(smena["hot_zac_celkem"])
        + int(smena["trzba_pos_hot"])
        - int(smena.get("naklady_celkem", 0))
        - int(smena.get("zalohy_neuhrazene", 0))
    )


def rozdil_a_tip_hotove(rozdil: int) -> tuple[int, int]:
    """Given actual − expected, return (cash_tip, chyba_amount).

    Positive rozdíl → cash tip. Negative → chyba (shortage).
    """
    if rozdil > 0:
        return (rozdil, 0)
    if rozdil < 0:
        return (0, abs(rozdil))
    return (0, 0)


def _clean_int(v) -> int:
    """Parse a possibly ru_RU-formatted Smeny cell ('1 365', '10,5') to int.
    Stored cells are read with numericise_ignore=['all'], so they arrive as
    strings; `int()` alone would choke on thin spaces / decimal commas."""
    try:
        s = (str(v).replace("\xa0", "").replace(" ", "")
             .replace("Kč", "").replace("kč", "").replace(",", "."))
        return int(round(float(s))) if s else 0
    except (ValueError, TypeError):
        return 0


def pokladna_diff_from_row(row: dict) -> int:
    """Recompute the cash reconciliation gap (bar till vs POS-reported cash tip)
    straight from a stored Smeny row's RAW input columns, using the current
    canonical `expected_konec` formula.

    This deliberately ignores the row's stored `rozdil` / `expected_konec` /
    `status`, because those are derived fields that may have been written by an
    older formula (e.g. pre-e-shop-fix rows over-subtract eshop_hotove) or before
    discrepancy-logging existed. Raw input columns are the only trustworthy source.

    Returns bot_surplus − recorded_cash_tip:
      0    → till reconciles with the POS-reported cash tip
      < 0  → envelope is SHORT by that much (recorded tip exceeds real surplus)
      > 0  → unexplained extra cash left in the till
    """
    cleaned = {
        "hot_zac_celkem": _clean_int(row.get("hot_zac_celkem")),
        "trzba_pos_hot": _clean_int(row.get("trzba_pos_hot")),
        "naklady_celkem": _clean_int(row.get("naklady_celkem")),
        "zalohy_neuhrazene": _clean_int(row.get("zalohy_neuhrazene")),
    }
    bot_surplus = _clean_int(row.get("hot_kon_celkem")) - expected_konec(cleaned)
    return bot_surplus - _clean_int(row.get("spropitne_hotov"))


def tip_per_hour(smena: dict) -> int:
    """Total tips / total hours, rounded to whole Kč."""
    total_tips = int(smena.get("spropitne_karta", 0)) + int(smena.get("spropitne_hotov", 0))
    total_hours = sum(float(p["hodiny"]) for p in smena.get("lidi", []))
    if total_hours == 0:
        return 0
    return round(total_tips / total_hours)


def k_vyplate_per_person(smena: dict) -> list[dict]:
    """For each worker on the shift, compute plat/tip/k_vyplate.

    IMPORTANT: per-person tips are distributed PROPORTIONALLY from the
    total tips pool, NOT computed as `hours × rounded_rate`. The latter
    loses precision: e.g. 356 Kč / 9h = 39.56/h, rounded to 40/h, then
    9 × 40 = 360 Kč (not 356). Proportional + residual-distribution
    guarantees sum(per-person tips) == total tips exactly.

    No rounding is applied to k_vyplate at the shift level — rounding
    only happens in weekly /vyplata via round_to_100.
    """
    sazba = int(smena["sazba_h"])
    lidi = smena.get("lidi", [])

    # Phase 1: compute plat (exact integer when sazba×hours is integer-clean,
    # otherwise round to whole Kč). Compute proportional raw tips.
    total_tips = int(smena.get("spropitne_karta", 0)) + int(smena.get("spropitne_hotov", 0))
    total_hours = sum(float(p["hodiny"]) for p in lidi)

    plat_per_person: list[int] = []
    raw_tips: list[float] = []
    for p in lidi:
        hours = float(p["hodiny"])
        plat_per_person.append(round(hours * sazba))
        if total_hours > 0:
            raw_tips.append(total_tips * hours / total_hours)
        else:
            raw_tips.append(0.0)

    # Phase 2: round tips, fix drift so sum == total_tips exactly.
    # Strategy: round each, then distribute residual ±1 to people with the
    # largest fractional remainders (largest-remainder method).
    tips_int = [int(t) for t in raw_tips]  # floor (since all >=0)
    residual = total_tips - sum(tips_int)
    if residual != 0 and tips_int:
        # Sort indices by fractional part descending (most "owed" first)
        order = sorted(range(len(raw_tips)), key=lambda i: -(raw_tips[i] - int(raw_tips[i])))
        for i in range(abs(residual)):
            tips_int[order[i % len(order)]] += 1 if residual > 0 else -1

    rows = []
    for i, p in enumerate(lidi):
        hours = float(p["hodiny"])
        plat = plat_per_person[i]
        tip = tips_int[i]
        pers_ucet = int(p.get("pers_ucet", 0))
        zaloha = int(p.get("zaloha", 0))
        rows.append({
            "jmeno": p["jmeno"],
            "hodiny": hours,
            "plat": plat,
            "spropitne": tip,
            "pers_ucet": pers_ucet,
            "zaloha": zaloha,
            "k_vyplate": plat + tip - pers_ucet - zaloha,
        })
    return rows


def round_to_100(amount: int) -> int:
    """Round to nearest 100 Kč (classical rounding — half rounds away from zero).
    Used in weekly payouts. Difference becomes 'dluh' (debt) carried to next week.
    Examples:
        7250 → 7300 (half rounds up; −50 Kč overpaid)
        7249 → 7200 (below halfway; +49 Kč underpaid)
        3280 → 3300 (above halfway)
        3220 → 3200 (below halfway)
        11250 → 11300 (half rounds up)
        7200 → 7200 (exact, no debt)
    """
    if amount >= 0:
        return ((amount + 50) // 100) * 100
    return -(((-amount + 50) // 100) * 100)


# ── Payout method (used by weekly /vyplata confirmation) ─────────
# A DPP payout is split into převod (bank) + hotově (cash). Bank převod is
# capped by the 12 000 Kč/month legal limit; the remaining headroom for the
# CURRENT payout is `remaining_bank` (computed by the caller).

def split_for_method(paid: int, method: str, remaining_bank: int) -> tuple[int, int]:
    """Return (prevodem, hotove) for `paid` under a chosen method.

    - 'hotove'   → all cash.
    - 'prevodem' → all transfer (caller must ensure paid <= remaining_bank).
    - 'mix'      → transfer up to remaining bank headroom, rest cash.
    """
    if method == "hotove":
        return (0, paid)
    if method == "prevodem":
        return (paid, 0)
    p = min(paid, max(0, remaining_bank))
    return (p, paid - p)


def cycle_payout_method(current: str, paid: int, remaining_bank: int) -> str:
    """Next method in cycle mix→hotove→prevodem→mix, skipping 'prevodem' when
    paying it all by transfer would exceed the monthly DPP bank limit."""
    order = ["mix", "hotove", "prevodem"]
    base = current if current in order else "mix"
    nxt = order[(order.index(base) + 1) % len(order)]
    if nxt == "prevodem" and paid > max(0, remaining_bank):
        nxt = order[(order.index(nxt) + 1) % len(order)]  # skip → mix
    return nxt


def per_person_daily(dated_shifts: list[tuple]) -> dict:
    """Per-person daily breakdown for the weekly summary.

    Input: list of (datum_str, shift_dict) — one entry per Smeny row, datum in
    DD.MM.YYYY. Output: {jmeno: [{datum, dow, hodiny, k_vyplate}, ...]} sorted
    by date. Each day's k_vyplate is that shift's contribution (plat + tip −
    pers_ucet − zaloha); summing them gives the person's weekly raw.
    """
    out: dict[str, list] = defaultdict(list)
    for datum, smena in dated_shifts:
        for p in k_vyplate_per_person(smena):
            out[p["jmeno"]].append({
                "datum": datum,
                "dow": czech_dow(datum),
                "hodiny": p["hodiny"],
                "k_vyplate": p["k_vyplate"],
            })

    def _key(entry):
        try:
            return _dt.strptime(entry["datum"], "%d.%m.%Y")
        except (ValueError, TypeError):
            return _dt.min

    for jmeno in out:
        out[jmeno].sort(key=_key)
    return dict(out)


def aggregate_week(shifts: list[dict]) -> list[dict]:
    """Aggregate per-person totals across multiple shifts."""
    by_person: dict[str, dict] = defaultdict(lambda: {
        "total_hodiny": 0.0, "total_plat": 0,
        "total_spropitne": 0, "total_personal_ucet": 0,
        "total_zalohy": 0,
    })

    for smena in shifts:
        rows = k_vyplate_per_person(smena)
        for r in rows:
            agg = by_person[r["jmeno"]]
            agg["total_hodiny"] += r["hodiny"]
            agg["total_plat"] += r["plat"]
            agg["total_spropitne"] += r["spropitne"]
            agg["total_personal_ucet"] += r["pers_ucet"]
            agg["total_zalohy"] += r["zaloha"]

    out = []
    for jmeno, agg in by_person.items():
        agg["jmeno"] = jmeno
        agg["k_vyplate"] = (
            agg["total_plat"] + agg["total_spropitne"]
            - agg["total_personal_ucet"] - agg["total_zalohy"]
        )
        out.append(dict(agg))
    return out
