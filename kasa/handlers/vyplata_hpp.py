"""/vyplata_hpp — monthly payout for HPP (full-time contract) employees. Manager+ only.

How HPP differs from DPP:
  - Paid MONTHLY (not weekly).
  - Card portion is FIXED (hpp_card_base = čistá mzda per contract, e.g. 15000 Kč).
    Always paid by bank transfer from BU Demo Bistro — this is what the tax office sees.
  - Cash portion = whatever the bartender actually earned (hours × sazba + tips)
    minus card portion minus hpp_cash_deduction (Max's tax-split deduction).
  - Rounded to 100 with classical rounding; difference becomes new_dluh carried
    to next month (positive = under-paid Jan, will get next month; negative =
    over-paid, will deduct next month).
  - If raw_with_dluh < card + deduction → cash = 0, new_dluh is negative
    (Jan owes Max back next month).

P&L rows written per payout:
  Row 1 (always): BV hpp_card_base from "BU Demo Bistro" — official bank salary
  Row 2 (if cash > 0): BV cash from "Hotovost Demo Bistro" — informal cash supplement

Default period: previous calendar month (you typically run this on 1st–5th of
the next month when the month has fully closed). Custom period accepted.
"""
from __future__ import annotations

import calendar
from datetime import datetime

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from kasa import auth
from kasa.kalkulace import aggregate_week, round_to_100
from kasa.sheets import SheetsClient


# State IDs outside 0..32 (uzaverka) and 100..101 (vyplata weekly)
VH_PERIOD = 200
VH_CONFIRM = 201


def _previous_month_range(today: datetime | None = None) -> tuple[str, str]:
    """Return (first_day, last_day) of the previous calendar month as DD.MM.YYYY strings."""
    today = today or datetime.now()
    # Move to last day of previous month
    first_of_this = today.replace(day=1)
    last_prev = first_of_this.replace(day=1)  # placeholder
    # Compute previous month
    if today.month == 1:
        prev_year, prev_month = today.year - 1, 12
    else:
        prev_year, prev_month = today.year, today.month - 1
    last_day_prev = calendar.monthrange(prev_year, prev_month)[1]
    return (
        f"01.{prev_month:02d}.{prev_year}",
        f"{last_day_prev:02d}.{prev_month:02d}.{prev_year}",
    )


async def cmd_vyplata_hpp(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point. Role-gated to manager+."""
    sheets: SheetsClient = ctx.application.bot_data["sheets"]
    rows = sheets.get_zamestnanci()
    emp = auth.find_by_tg_user_id(rows, update.effective_user.id)
    if not emp or not auth.has_role(emp, "manager"):
        await update.message.reply_text(
            "Tento příkaz je pouze pro manažera nebo majitele."
        )
        return ConversationHandler.END

    period_from, period_to = _previous_month_range()
    ctx.user_data["_vh_period"] = (period_from, period_to)

    kb = ReplyKeyboardMarkup(
        [[f"OK ({period_from} – {period_to})", "Změnit"]],
        one_time_keyboard=True, resize_keyboard=True,
    )
    await update.message.reply_text(
        "Měsíční výplata HPP — období?\n"
        "💡 Default: minulý kalendářní měsíc.",
        reply_markup=kb,
    )
    return VH_PERIOD


async def on_vh_period(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text == "Změnit":
        await update.message.reply_text(
            "Napiš období ve formátu DD.MM.YYYY–DD.MM.YYYY:"
        )
        return VH_PERIOD
    # "OK (…)" button confirms default — skip the range parser (button label
    # contains "–" between dates which would otherwise be mis-parsed).
    if text.startswith("OK"):
        pass  # keep stored default
    elif "–" in text or "-" in text:
        sep = "–" if "–" in text else "-"
        try:
            f, t = (x.strip() for x in text.split(sep, 1))
            datetime.strptime(f, "%d.%m.%Y")
            datetime.strptime(t, "%d.%m.%Y")
            ctx.user_data["_vh_period"] = (f, t)
        except (ValueError, IndexError):
            await update.message.reply_text(
                "Neplatný formát. Napiš DD.MM.YYYY–DD.MM.YYYY:"
            )
            return VH_PERIOD

    from_, to_ = ctx.user_data["_vh_period"]
    sheets: SheetsClient = ctx.application.bot_data["sheets"]

    # Find all HPP employees in Zamestnanci
    hpp_emps = [
        r for r in sheets.get_zamestnanci()
        if str(r.get("typ_smlouvy", "")).strip().upper() == "HPP"
        and auth.is_active(r)
    ]
    if not hpp_emps:
        await update.message.reply_text(
            "Žádný aktivní HPP zaměstnanec v Zamestnanci. Zrušeno."
        )
        return ConversationHandler.END

    # Aggregate Smeny for the period — same generic aggregator as /vyplata
    smeny_rows = sheets.read_smeny_in_range(from_, to_)
    if not smeny_rows:
        await update.message.reply_text(
            f"Žádné směny v období {from_} – {to_}. Zrušeno."
        )
        return ConversationHandler.END

    shifts = [SheetsClient.smena_row_to_shift_dict(r) for r in smeny_rows]
    full_agg = aggregate_week(shifts)
    agg_by_jmeno = {r["jmeno"]: r for r in full_agg}

    # Build a per-HPP-person payout plan
    plan: list[dict] = []
    for emp in hpp_emps:
        jmeno = str(emp["jmeno"]).strip()
        try:
            card_base = int(emp.get("hpp_card_base") or 0)
            deduction = int(emp.get("hpp_cash_deduction") or 0)
        except (ValueError, TypeError):
            await update.message.reply_text(
                f"⚠️ {jmeno}: chybný hpp_card_base nebo hpp_cash_deduction "
                f"v Zamestnanci. Zrušeno."
            )
            return ConversationHandler.END

        r = agg_by_jmeno.get(jmeno)
        if r is None:
            # HPP person did not work any shift in the period — still pay card_base
            # (HPP salary is fixed by contract), and the entire card amount accrues
            # as positive dluh (employee owes future hours).
            raw = 0
            total_hodiny = 0.0
            total_plat = 0
            total_spropitne = 0
            total_personal_ucet = 0
            total_zalohy = 0
        else:
            raw = int(r["k_vyplate"])
            total_hodiny = float(r["total_hodiny"])
            total_plat = int(r["total_plat"])
            total_spropitne = int(r["total_spropitne"])
            total_personal_ucet = int(r["total_personal_ucet"])
            total_zalohy = int(r["total_zalohy"])

        prev_dluh = sheets.read_last_dluh(jmeno)
        raw_with_dluh = raw + prev_dluh
        cash_pre_round = raw_with_dluh - card_base - deduction

        if cash_pre_round > 0:
            cash = round_to_100(cash_pre_round)
            new_dluh = cash_pre_round - cash
        else:
            # Under-earned this month → no cash, debt is the shortfall.
            # new_dluh is negative (employee owes back) — next month's raw will
            # absorb it.
            cash = 0
            new_dluh = cash_pre_round  # already negative or zero

        plan.append({
            "jmeno": jmeno,
            "card_base": card_base,
            "deduction": deduction,
            "total_hodiny": total_hodiny,
            "total_plat": total_plat,
            "total_spropitne": total_spropitne,
            "total_personal_ucet": total_personal_ucet,
            "total_zalohy": total_zalohy,
            "k_vyplate_raw": raw,
            "prev_dluh": prev_dluh,
            "raw_with_dluh": raw_with_dluh,
            "cash_pre_round": cash_pre_round,
            "cash": cash,
            "card": card_base,
            "new_dluh": new_dluh,
            "k_vyplate": card_base + cash,  # for Vyplaty.k_vyplate column
        })

    ctx.user_data["_vh_plan"] = plan
    ctx.user_data["_vh_period"] = (from_, to_)

    # Build summary
    lines = [
        f"📊 HPP výplata {from_} – {to_}",
        f"   ({len(smeny_rows)} směn celkem)",
        "━" * 36,
    ]
    grand_card = 0
    grand_cash = 0
    for p in plan:
        dluh_str = f" + dluh {p['prev_dluh']:+d}" if p["prev_dluh"] != 0 else ""
        new_dluh_str = f"  Příště: {p['new_dluh']:+d}" if p["new_dluh"] != 0 else ""
        if p["cash"] > 0:
            line_pay = (
                f"│  → Karta {p['card']:,} Kč (BU)"
                f"\n│  → Hotově {p['cash']:,} Kč"
                f"\n│  − odpočet daně {p['deduction']:,} Kč"
            )
        else:
            line_pay = (
                f"│  → Karta {p['card']:,} Kč (BU)"
                f"\n│  Hotově 0 Kč (málo odpracováno)"
                f"\n│  − odpočet daně {p['deduction']:,} Kč"
            )
        lines.append(
            f"┌ {p['jmeno']} (HPP) — {p['total_hodiny']}h"
            f"\n│  raw {p['k_vyplate_raw']:,}{dluh_str} = {p['raw_with_dluh']:,}"
            f"\n{line_pay}"
            f"{new_dluh_str}"
        )
        grand_card += p["card"]
        grand_cash += p["cash"]
    lines.append("━" * 36)
    lines.append(f"Celkem karta (BU):     {grand_card:,} Kč")
    lines.append(f"Celkem hotově:         {grand_cash:,} Kč")
    lines.append(f"Celkem k vyplacení:    {grand_card + grand_cash:,} Kč")
    lines.append("")
    lines.append("Potvrdit?")

    kb = ReplyKeyboardMarkup(
        [["Uložit ✅"], ["Posunout"]],
        one_time_keyboard=True, resize_keyboard=True,
    )
    await update.message.reply_text("\n".join(lines), reply_markup=kb)
    return VH_CONFIRM


async def on_vh_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    if "Posunout" in text:
        await update.message.reply_text(
            "OK, nezaznamenáno. Zkus znovu /vyplata_hpp kdy budeš chtít."
        )
        return ConversationHandler.END

    sheets: SheetsClient = ctx.application.bot_data["sheets"]
    from_, to_ = ctx.user_data["_vh_period"]
    today = datetime.now()
    # Monthly id e.g. 2026-M05 to distinguish from /vyplata's 2026-W21 weekly ids
    vyplata_id = today.strftime("%Y-M%m")
    kym = update.effective_user.username or str(update.effective_user.id)

    n_written = 0
    for p in ctx.user_data["_vh_plan"]:
        # Build the agg dict shape expected by append_vyplata
        agg_for_row = {
            "total_hodiny": p["total_hodiny"],
            "total_plat": p["total_plat"],
            "total_spropitne": p["total_spropitne"],
            "total_personal_ucet": p["total_personal_ucet"],
            "total_zalohy": p["total_zalohy"],
            "k_vyplate": p["k_vyplate"],  # = card + cash
        }
        # zpusob — informational
        if p["cash"] > 0:
            zpusob = "hpp-mix"
        else:
            zpusob = "hpp-karta"

        sheets.append_vyplata(
            vyplata_id=vyplata_id,
            datum_vyplaty=today.strftime("%d.%m.%Y"),
            period_from=from_, period_to=to_,
            jmeno=p["jmeno"], agg=agg_for_row, zpusob=zpusob, kym=kym,
            dluh_vznikly=p["new_dluh"],
            castka_prevodem=p["card"],
            castka_hotove=p["cash"],
        )

        # P&L Row 1: card portion (always written, even if 0 would be unusual but allowed)
        if p["card"] > 0:
            sheets.append_naklad_to_pnl(
                datum=today.strftime("%d.%m.%Y"),
                castka=p["card"],
                popis=f"Výplata HPP {p['jmeno']} (karta) {from_}–{to_}",
                kategorie="BV",
                zaplaceno_zdroj="BU Demo Bistro",
                kdo_zapsal="bot:/vyplata_hpp",
            )
        # P&L Row 2: cash portion (only if > 0)
        if p["cash"] > 0:
            sheets.append_naklad_to_pnl(
                datum=today.strftime("%d.%m.%Y"),
                castka=p["cash"],
                popis=f"Výplata HPP {p['jmeno']} (hotově) {from_}–{to_}",
                kategorie="BV",
                zaplaceno_zdroj="Hotovost Demo Bistro",
                kdo_zapsal="bot:/vyplata_hpp",
            )
        n_written += 1

    await update.message.reply_text(
        f"✅ HPP výplata zapsána.\n"
        f"   {n_written} osob.\n"
        f"   Záznamy v Vyplaty + P&L (BV karta + BV hotově)."
    )
    return ConversationHandler.END
