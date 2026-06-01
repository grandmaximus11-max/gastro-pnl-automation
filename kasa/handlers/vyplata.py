"""/vyplata — weekly payout aggregation. Manager+ only.

Saturday flow:
  1. Bot proposes last Saturday → today as period
  2. Pulls all Smeny rows in that range from Sheets
  3. Aggregates per person via kalkulace.aggregate_week
  4. Shows tabular summary with totals
  5. On Vyplaceno hotově/převodem: writes Vyplaty rows + appends BV rows to P&L
"""
from __future__ import annotations

from datetime import datetime, timedelta

from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from kasa import auth
from kasa.kalkulace import (
    aggregate_week, round_to_100, per_person_daily,
)
from kasa.notify import push_vyplata_owner
from kasa.sheets import SheetsClient


# Use state IDs outside the 0..33 range used by /uzaverka ConversationHandler
V_PERIOD = 100
V_METHODS = 102


def _fmt(amount: int) -> str:
    """Czech-style number formatting: thin no-break space as thousands separator.
    Examples: 1400 → '1 400', 22493900 → '22 493 900'. The U+202F char prevents
    line-breaks inside numbers on mobile, fixing the wrap-mid-amount issue."""
    return f"{amount:,}".replace(",", " ")


def _daily_lines(daily: list) -> list[str]:
    """Render per-day breakdown rows: 'Po 16.05 · 7.5h · 1 435 Kč'."""
    out = []
    for d in daily:
        out.append(
            f"   {d['dow']} {str(d['datum'])[:5]} · "
            f"{d['hodiny']:g}h · {_fmt(int(d['k_vyplate']))} Kč"
        )
    return out


def _last_saturday(today: datetime | None = None) -> datetime:
    """Return the date of the most recent Saturday (today if today is Saturday)."""
    today = today or datetime.now()
    days_since_sat = (today.weekday() - 5) % 7  # Mon=0, Sat=5
    return today - timedelta(days=days_since_sat)


def _pay_weeks(today: datetime | None = None) -> list[tuple[str, datetime, datetime]]:
    """Recent Sat–Fri pay-weeks, newest first → [(label, from, to), ...].

    Mzda is paid on Saturday for the PREVIOUS completed week, so the DEFAULT is
    the last completed Sat–Fri week (not the current, still-running one). Also
    offers the week before it, and the current incomplete week as a fallback.
    """
    today = today or datetime.now()
    cur_sat = _last_saturday(today)  # Saturday that started the CURRENT week
    return [
        ("Minulý týden", cur_sat - timedelta(days=7), cur_sat - timedelta(days=1)),
        ("Předminulý", cur_sat - timedelta(days=14), cur_sat - timedelta(days=8)),
        ("Tento (neúplný)", cur_sat, today),
    ]


async def cmd_vyplata(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point. Role-gated to manager+."""
    sheets: SheetsClient = ctx.application.bot_data["sheets"]
    rows = sheets.get_zamestnanci()
    emp = auth.find_by_tg_user_id(rows, update.effective_user.id)
    if not emp or not auth.has_role(emp, "manager"):
        await update.message.reply_text(
            "Tento příkaz je pouze pro manažera nebo majitele."
        )
        return ConversationHandler.END

    today = datetime.now()
    # Offer recent Sat–Fri pay-weeks as buttons; default = last completed week.
    _v_weeks: dict[str, tuple[str, str]] = {}
    rows: list[list[str]] = []
    for label, f, t in _pay_weeks(today):
        btn = f"{label} {f.strftime('%d.%m')}–{t.strftime('%d.%m')}"
        _v_weeks[btn] = (f.strftime("%d.%m.%Y"), t.strftime("%d.%m.%Y"))
        rows.append([btn])
    rows.append(["Změnit"])
    ctx.user_data["_v_weeks"] = _v_weeks
    ctx.user_data["_v_period"] = next(iter(_v_weeks.values()))  # last completed week

    kb = ReplyKeyboardMarkup(rows, one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(
        "Za jaké období výplata?\n"
        "💡 Mzda Sobota–Pátek, vyplácí se v sobotu za minulý týden.\n"
        "(Default: minulý dokončený týden.)",
        reply_markup=kb,
    )
    return V_PERIOD


async def on_v_period(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    weeks = ctx.user_data.get("_v_weeks", {})
    # Week button tapped — matched FIRST (the label contains "–", which would
    # otherwise trigger the manual-range parser below).
    if text in weeks:
        ctx.user_data["_v_period"] = weeks[text]
    elif text == "Změnit":
        await update.message.reply_text(
            "Napiš období ve formátu DD.MM.YYYY–DD.MM.YYYY:"
        )
        return V_PERIOD
    elif text.startswith("OK"):
        pass  # legacy — keep stored default
    elif "–" in text or "-" in text:
        # User typed range explicitly
        sep = "–" if "–" in text else "-"
        try:
            f, t = (x.strip() for x in text.split(sep, 1))
            datetime.strptime(f, "%d.%m.%Y")
            datetime.strptime(t, "%d.%m.%Y")
            ctx.user_data["_v_period"] = (f, t)
        except (ValueError, IndexError):
            await update.message.reply_text(
                "Neplatný formát. Napiš DD.MM.YYYY–DD.MM.YYYY:"
            )
            return V_PERIOD
    # else: keep the default period stored in cmd_vyplata
    from_, to_ = ctx.user_data["_v_period"]

    sheets: SheetsClient = ctx.application.bot_data["sheets"]
    smeny_rows = sheets.read_smeny_in_range(from_, to_)
    if not smeny_rows:
        await update.message.reply_text(
            f"Žádné směny v období {from_} – {to_}. Zrušeno."
        )
        return ConversationHandler.END

    shifts = [SheetsClient.smena_row_to_shift_dict(r) for r in smeny_rows]
    agg = aggregate_week(shifts)

    if not agg:
        await update.message.reply_text(
            f"V {len(smeny_rows)} směnách nikdo nepracoval. Zrušeno."
        )
        return ConversationHandler.END

    # Per-day breakdown (den + výdělek za den) for the summary table.
    _dated = [(str(r.get("datum", "")), SheetsClient.smena_row_to_shift_dict(r))
              for r in smeny_rows]
    _daily = per_person_daily(_dated)
    for r in agg:
        r["daily"] = _daily.get(r["jmeno"], [])

    # Tag each person as DPP or HPP. HPP entries stay in the summary for team
    # transparency (bartenders verify hours + tip pool against the table Max
    # screenshots them) but get NO payout math and are skipped at save-time.
    for r in agg:
        emp = sheets.get_employee_by_jmeno(r["jmeno"])
        typ = str(emp.get("typ_smlouvy", "")).strip().upper() if emp else ""
        r["is_hpp"] = typ == "HPP"
        # OSVČ → match with/without diacritic. Shift pay = cash, weekly, no DPP.
        r["is_osvc"] = typ.replace("Č", "C") == "OSVC"

    # Month-to-date accumulation for HPP people: they're paid monthly, so the
    # weekly summary shows a RUNNING total (накопительная сумма) from the 1st of
    # the period's month through period_to. Computed once here (one extra Smeny
    # read) and looked up per HPP person below — cheaper than per-person reads.
    _has_hpp = any(r["is_hpp"] for r in agg)
    mtd_by_jmeno: dict[str, dict] = {}
    if _has_hpp:
        to_dt = datetime.strptime(to_, "%d.%m.%Y")
        month_start = to_dt.replace(day=1).strftime("%d.%m.%Y")
        mtd_rows = sheets.read_smeny_in_range(month_start, to_)
        mtd_shifts = [SheetsClient.smena_row_to_shift_dict(rr) for rr in mtd_rows]
        mtd_by_jmeno = {x["jmeno"]: x for x in aggregate_week(mtd_shifts)}

    # Phase A: apply prev-dluh carryover + round to 100 + compute new dluh per person
    # Phase B: split paid into prevodem (up to remaining DPP limit) + hotove
    # HPP people are skipped — their math runs monthly in /vyplata_hpp.
    DPP_MONTHLY_LIMIT = 12_000
    for r in agg:
        if r["is_hpp"]:
            # Keep raw values for display; zero out payout fields so save-loop sees them.
            r["k_vyplate_raw"] = r["k_vyplate"]  # preserve raw (this period only)
            r["prev_dluh"] = 0
            r["raw_with_dluh"] = r["k_vyplate"]
            r["new_dluh"] = 0
            r["prevodem"] = 0
            r["hotove"] = 0
            r["transferred_before"] = 0
            r["zustatek_banka"] = 0
            # Running month-to-date total + carryover from previous month's payout.
            mtd = mtd_by_jmeno.get(r["jmeno"])
            r["mtd_total"] = int(mtd["k_vyplate"]) if mtd else int(r["k_vyplate"])
            r["mtd_hodiny"] = float(mtd["total_hodiny"]) if mtd else float(r["total_hodiny"])
            r["prev_month_dluh"] = sheets.read_last_dluh(r["jmeno"])
            continue

        raw = r["k_vyplate"]
        prev_dluh = sheets.read_last_dluh(r["jmeno"])
        raw_with_dluh = raw + prev_dluh
        paid = round_to_100(raw_with_dluh)
        new_dluh = raw_with_dluh - paid  # + = under-paid, − = over-paid

        # DPP split: max převodem until monthly bank limit, rest hotově.
        # OSVČ: shift pay is CASH ONLY — no bank transfer, no 12k limit (jejich
        # hlavní mzda jde fakturou). remaining_bank=0 makes the SAME split logic
        # put 100 % into hotově, so no special-casing downstream.
        if r.get("is_osvc"):
            transferred_this_month = 0
            remaining_bank = 0
        else:
            transferred_this_month = sheets.read_prevod_this_month(r["jmeno"])
            remaining_bank = max(0, DPP_MONTHLY_LIMIT - transferred_this_month)
        prevodem = min(paid, remaining_bank)
        hotove = paid - prevodem

        r["k_vyplate_raw"] = raw
        r["prev_dluh"] = prev_dluh
        r["raw_with_dluh"] = raw_with_dluh
        r["k_vyplate"] = paid
        r["new_dluh"] = new_dluh
        r["prevodem"] = prevodem
        r["hotove"] = hotove
        r["transferred_before"] = transferred_this_month
        r["zustatek_banka"] = remaining_bank - prevodem  # what's left AFTER this payout
        r["method"] = "mix"
        r["remaining_bank"] = remaining_bank

    ctx.user_data["_v_agg"] = agg
    ctx.user_data["_v_period"] = (from_, to_)

    ctx.user_data["_v_smeny_count"] = len(smeny_rows)
    # Anti-double-pay: who was already paid for THIS exact period? (shown in the
    # table + Potvrdit asks again). Reset any prior "pay again" override.
    ctx.user_data["_v_already_paid"] = sheets.read_vyplaty_for_period(from_, to_)
    ctx.user_data.pop("_v_repay_ok", None)
    # One screen: full table (per-person real split) + Potvrdit/Upravit/Zrušit.
    return await _show_summary_screen(update, ctx)


def _build_summary_text(ctx: ContextTypes.DEFAULT_TYPE) -> str:
    """Full payout table with each person's REAL split (not a bare 'mix' label).
    Rebuilt from _v_agg on every render, so it reflects edits made via Upravit.

    Design decisions (mobile-first):
      • 👤 / 💼 prefix distinguishes DPP vs HPP at a glance
      • show 'raw'/'příště' lines only when they carry info (less clutter)
      • DPP-cap warnings collected into a footer, not per-person noise
    """
    agg = ctx.user_data["_v_agg"]
    from_, to_ = ctx.user_data["_v_period"]
    already = ctx.user_data.get("_v_already_paid", {})
    smeny_count = ctx.user_data.get("_v_smeny_count", 0)
    sep_line = "━" * 14  # short separator — phones wrap longer ones
    lines = [f"📊 {from_} – {to_} ({smeny_count} směn)", sep_line]
    total_prevodem = 0
    total_hotove = 0
    bank_warnings: list[str] = []

    for r in agg:
        if r["is_hpp"]:
            # HPP info block — visible for team transparency, no payout written.
            block = [f"💼 {r['jmeno']} · {r['total_hodiny']}h (HPP)"]
            block += _daily_lines(r.get("daily", []))
            block += [
                f"   ── tento týden: {_fmt(r['k_vyplate_raw'])} Kč",
                f"   📈 Za měsíc dosud: {_fmt(r['mtd_total'])} Kč ({r['mtd_hodiny']:g}h)",
            ]
            if r["prev_month_dluh"] != 0:
                sign = "+" if r["prev_month_dluh"] > 0 else "−"
                block.append(
                    f"   Dluh z min. měsíce: {sign}{_fmt(abs(r['prev_month_dluh']))} Kč"
                )
            block.append("   → výplata měsíčně přes /vyplata_hpp")
            lines.append("\n".join(block))
            lines.append("")
            continue

        # ── DPP / OSVČ person: header, per-day breakdown, payout + real split ──
        osvc_tag = " · OSVČ (hotově)" if r.get("is_osvc") else ""
        block = [f"👤 {r['jmeno']} · {r['total_hodiny']}h{osvc_tag}"]
        block += _daily_lines(r.get("daily", []))
        if r.get("daily"):
            block.append("   ──────")
        if r["prevodem"] > 0 and r["hotove"] > 0:
            block.append(f"   {_fmt(r['k_vyplate'])} Kč 🔄 mix")
            block.append(f"      💳 {_fmt(r['prevodem'])} převodem")
            block.append(f"      💵 {_fmt(r['hotove'])} hotově")
        elif r["prevodem"] > 0:
            block.append(f"   {_fmt(r['k_vyplate'])} Kč 💳 převodem")
        else:
            block.append(f"   {_fmt(r['k_vyplate'])} Kč 💵 hotově")
        if r["jmeno"] in already:
            block.append(f"   ✅ Už vyplaceno {already[r['jmeno']]}")

        details: list[str] = []
        if r["prev_dluh"] != 0:
            details.append(
                f"raw {_fmt(r['k_vyplate_raw'])} "
                f"{'+' if r['prev_dluh'] > 0 else '−'} dluh "
                f"{_fmt(abs(r['prev_dluh']))} = {_fmt(r['raw_with_dluh'])}"
            )
        elif r["k_vyplate_raw"] != r["k_vyplate"]:
            details.append(f"raw {_fmt(r['k_vyplate_raw'])}")
        if r["new_dluh"] != 0:
            sign = "+" if r["new_dluh"] > 0 else "−"
            details.append(f"příště {sign}{_fmt(abs(r['new_dluh']))}")
        if details:
            block.append(f"   {' · '.join(details)}")

        lines.append("\n".join(block))
        lines.append("")
        if not r.get("is_osvc") and r["zustatek_banka"] < 3000:
            bank_warnings.append(
                f"{r['jmeno']}: zbývá {_fmt(r['zustatek_banka'])} Kč na DPP převod tento měsíc"
            )
        total_prevodem += r["prevodem"]
        total_hotove += r["hotove"]

    lines.append(sep_line)
    if total_prevodem > 0:
        lines.append(f"💳 převodem: {_fmt(total_prevodem)} Kč")
    if total_hotove > 0:
        lines.append(f"💵 hotově:   {_fmt(total_hotove)} Kč")
    lines.append(sep_line)
    lines.append(f"✅ celkem: {_fmt(total_prevodem + total_hotove)} Kč")
    if bank_warnings:
        lines.append("")
        lines.append("⚠️ DPP-limit upozornění:")
        for w in bank_warnings:
            lines.append(f"   • {w}")
    dup = [r["jmeno"] for r in agg if not r["is_hpp"] and r["jmeno"] in already]
    if dup:
        lines.append("")
        lines.append(f"⚠️ Toto období už bylo vyplaceno ({len(dup)} os.). "
                     f"Potvrdit se zeptá znovu.")
    return "\n".join(lines)


async def _show_summary_screen(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                               edit: bool = False) -> int:
    """Default payout screen: the full table + three actions. Editing the split
    is opt-in (✏️ Upravit), so the common path is a single Potvrdit tap — no
    per-person toggling, no waiting on a round-trip for every name."""
    text = _build_summary_text(ctx)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Potvrdit", callback_data="vm:confirm")],
        [InlineKeyboardButton("✏️ Upravit", callback_data="vm:edit"),
         InlineKeyboardButton("❌ Zrušit", callback_data="vm:skip")],
    ])
    if edit and update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, reply_markup=kb)
        except Exception as e:
            if "not modified" not in str(e).lower():
                raise
    else:
        await update.effective_message.reply_text(text, reply_markup=kb)
    return V_METHODS


_METHOD_LABEL = {"mix": "🔄 mix", "hotove": "💵 hotově", "prevodem": "💳 převodem"}


async def _show_methods(update: Update, ctx: ContextTypes.DEFAULT_TYPE, edit: bool = False) -> int:
    """Opt-in EDIT screen (reached via ✏️ Upravit). Per-DPP-person method toggle;
    each button shows the person's REAL split (💳/💵 amounts), never a bare 'mix'.
    ✅ Hotovo returns to the summary; ❌ Zrušit aborts without recording."""
    agg = ctx.user_data["_v_agg"]
    kb_rows = []
    for i, r in enumerate(agg):
        if r.get("is_hpp"):
            continue
        pv, ho = r.get("prevodem", 0), r.get("hotove", 0)
        edited = "✏️ " if r.get("method") == "rucne" else ""
        if pv > 0 and ho > 0:
            label = f"{edited}🔄 💳{pv}+💵{ho}"
        elif pv > 0:
            label = f"{edited}💳 {pv}"
        else:
            label = f"{edited}💵 {ho}"
        kb_rows.append([InlineKeyboardButton(f"{r['jmeno']}: {label}", callback_data=f"vm:{i}")])
    kb_rows.append([
        InlineKeyboardButton("✅ Hotovo", callback_data="vm:done"),
        InlineKeyboardButton("❌ Zrušit", callback_data="vm:skip"),
    ])
    text = ("Uprav výplatu — klikni na osobu a napiš, kolik jí poslat převodem\n"
            "(zbytek dostane hotově). Klikni znovu pro jinou částku.\n"
            "Až bude hotovo → ✅ Hotovo.")
    kb = InlineKeyboardMarkup(kb_rows)
    if edit and update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, reply_markup=kb)
        except Exception as e:
            if "not modified" not in str(e).lower():
                raise
    else:
        await update.effective_message.reply_text(text, reply_markup=kb)
    return V_METHODS


def _apply_manual_split(target: dict, amount: int) -> None:
    """Set a person's převodem = amount (clamped to pay AND remaining DPP bank
    headroom), the rest goes hotově. Marks the row as hand-edited ('rucne')."""
    paid = int(target["k_vyplate"])
    remaining = int(target.get("remaining_bank", 0))
    prevodem = max(0, min(amount, paid, remaining))
    target["prevodem"] = prevodem
    target["hotove"] = paid - prevodem
    target["method"] = "rucne"


def _split_confirm_text(target: dict, amount: int) -> str:
    paid = int(target["k_vyplate"])
    remaining = int(target.get("remaining_bank", 0))
    prevodem = int(target["prevodem"])
    warn = ""
    if amount > prevodem:  # the entry was clamped — name the BINDING constraint
        if remaining < paid and prevodem == remaining:
            warn = f"\n⚠️ DPP limit měsíce: převod omezen na {_fmt(remaining)} Kč."
        else:
            warn = f"\n⚠️ Víc než výplata — převod = {_fmt(paid)} Kč."
    return (f"{target['jmeno']}: 💳 {_fmt(prevodem)} převodem "
            f"+ 💵 {_fmt(target['hotove'])} hotově{warn}")


async def on_v_method_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    # Any button press cancels a pending "type the amount" for a tapped person.
    ctx.user_data.pop("_v_edit_target", None)
    if data == "vm:skip":
        await q.edit_message_text("OK, nezaznamenáno. Spusť /vyplata znovu kdykoliv.")
        return ConversationHandler.END
    if data == "vm:confirm":
        # Anti-double-pay guard: if anyone in this period was already paid and the
        # manager hasn't explicitly OK'd a re-pay, ask before writing duplicates.
        already = ctx.user_data.get("_v_already_paid") or {}
        agg = ctx.user_data.get("_v_agg", [])
        dup = [r["jmeno"] for r in agg if not r.get("is_hpp") and r["jmeno"] in already]
        if dup and not ctx.user_data.get("_v_repay_ok"):
            from_, to_ = ctx.user_data.get("_v_period", ("", ""))
            names = "\n".join(f"   • {n} ({already[n]})" for n in dup)
            await q.edit_message_text(
                f"⚠️ Období {from_} – {to_} už bylo vyplaceno:\n{names}\n\n"
                f"Opravdu vyplatit ZNOVU? Vznikne druhý záznam v Vyplaty + P&L.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Ano, vyplatit znovu", callback_data="vm:confirm2")],
                    [InlineKeyboardButton("❌ Zrušit", callback_data="vm:skip")],
                ]),
            )
            return V_METHODS
        return await _finalize_vyplata(update, ctx)
    if data == "vm:confirm2":  # explicit re-pay of an already-paid period
        ctx.user_data["_v_repay_ok"] = True
        return await _finalize_vyplata(update, ctx)
    if data == "vm:edit":
        return await _show_methods(update, ctx, edit=True)
    if data == "vm:done":
        return await _show_summary_screen(update, ctx, edit=True)
    if data.startswith("vm:"):
        try:
            idx = int(data.split(":")[1])
        except (ValueError, IndexError):
            return V_METHODS
        agg = ctx.user_data["_v_agg"]
        if 0 <= idx < len(agg) and not agg[idx].get("is_hpp"):
            # Tap a person → ask how much of their pay goes převodem (rest hotově).
            # One input covers every case: 0 = vše hotově, max = vše na účet,
            # anything between = a custom mix. Replaces the old 3-state toggle,
            # which collapsed to "card vs cash" whenever pay fit under the cap.
            r = agg[idx]
            ctx.user_data["_v_edit_target"] = idx
            paid = int(r["k_vyplate"])
            cap = max(0, min(paid, int(r.get("remaining_bank", 0))))
            await q.edit_message_text(
                f"{r['jmeno']}: kolik z {_fmt(paid)} Kč poslat převodem?\n"
                f"Napiš číslo 0–{_fmt(cap)}  "
                f"(0 = vše hotově · {_fmt(cap)} = max na účet · zbytek hotově)."
            )
            return V_METHODS
        return await _show_methods(update, ctx, edit=True)
    return V_METHODS


async def on_v_method_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Custom-split entry. Two ways in:
      • Tap a person on the Upravit screen, then type just the amount (preferred).
      • Or type «Jméno částka» directly.
    Sets that person's převodem (rest hotově), clamped to the DPP monthly cap."""
    text = (update.message.text or "").strip()
    agg = ctx.user_data.get("_v_agg", [])

    # Tapped-person flow: a target index is pending → this message is the amount.
    target_idx = ctx.user_data.pop("_v_edit_target", None)
    if target_idx is not None:
        try:
            amount = int(text.replace(" ", "").replace("\xa0", "").replace("Kč", ""))
        except ValueError:
            ctx.user_data["_v_edit_target"] = target_idx  # stay, ask again
            await update.message.reply_text("Napiš jen číslo, např. 1000 (0 = vše hotově).")
            return V_METHODS
        if not (0 <= target_idx < len(agg)) or agg[target_idx].get("is_hpp"):
            return await _show_methods(update, ctx, edit=False)
        target = agg[target_idx]
        _apply_manual_split(target, amount)
        await update.message.reply_text(_split_confirm_text(target, amount))
        # Stay in the edit screen so the manager can adjust another person.
        return await _show_methods(update, ctx, edit=False)

    # Legacy direct form: «Jméno částka»
    parts = text.rsplit(None, 1)
    if len(parts) != 2:
        await update.message.reply_text("Klikni na osobu nahoře, nebo napiš «Jméno částka».")
        return V_METHODS
    name = parts[0].strip()
    try:
        amount = int(parts[1].replace(" ", "").replace("\xa0", "").replace("Kč", ""))
    except ValueError:
        await update.message.reply_text("Částka musí být číslo, např. «Lena 3000».")
        return V_METHODS
    target = next(
        (r for r in agg
         if not r.get("is_hpp") and str(r["jmeno"]).strip().lower() == name.lower()),
        None,
    )
    if target is None:
        await update.message.reply_text(f"«{name}» není mezi DPP osobami. Zkus znovu.")
        return V_METHODS
    _apply_manual_split(target, amount)
    await update.message.reply_text(_split_confirm_text(target, amount))
    # Back to the full table (now reflecting this edit) + Potvrdit.
    return await _show_summary_screen(update, ctx, edit=False)


async def _finalize_vyplata(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    sheets: SheetsClient = ctx.application.bot_data["sheets"]
    cfg = ctx.application.bot_data["config"]
    from_, to_ = ctx.user_data["_v_period"]
    today = datetime.now()
    vyplata_id = today.strftime("%Y-W%U")
    kym = update.effective_user.username or str(update.effective_user.id)

    agg = ctx.user_data["_v_agg"]
    n_written = 0
    n_skipped_hpp = 0
    for r in agg:
        if r.get("is_hpp"):
            n_skipped_hpp += 1
            continue
        if r["prevodem"] > 0 and r["hotove"] > 0:
            zpusob = "mix"
        elif r["prevodem"] > 0:
            zpusob = "prevodem"
        else:
            zpusob = "hotove"
        sheets.append_vyplata(
            vyplata_id=vyplata_id, datum_vyplaty=today.strftime("%d.%m.%Y"),
            period_from=from_, period_to=to_, jmeno=r["jmeno"], agg=r,
            zpusob=zpusob, kym=kym, dluh_vznikly=r.get("new_dluh", 0),
            castka_prevodem=r["prevodem"], castka_hotove=r["hotove"],
        )
        # Transfer → owner pays by bank → висит neuhrazeno until FIO matches.
        if r["prevodem"] > 0:
            sheets.append_naklad_to_pnl(
                datum=today.strftime("%d.%m.%Y"), castka=r["prevodem"],
                popis=f"Výplata {r['jmeno']} (převodem) {from_}–{to_}",
                kategorie="BV", zaplaceno_zdroj="BU Demo Bistro",
                kdo_zapsal="bot:/vyplata",
                stav_platby="neuhrazeno", datum_uhrady="",
            )
        # Cash → manager pays from envelope → zaplaceno hned.
        if r["hotove"] > 0:
            sheets.append_naklad_to_pnl(
                datum=today.strftime("%d.%m.%Y"), castka=r["hotove"],
                popis=f"Výplata {r['jmeno']} (hotově) {from_}–{to_}",
                kategorie="BV", zaplaceno_zdroj="Hotovost Demo Bistro",
                kdo_zapsal="bot:/vyplata",
            )
        n_written += 1

    await push_vyplata_owner(ctx.bot, cfg.owner_tg_id, (from_, to_), agg)

    msg = (
        f"✅ Výplata potvrzena.\n"
        f"   {n_written} osob (DPP) zapsáno do Vyplaty + P&L.\n"
        f"   Převody visí jako neuhrazené (uzavře FIO).\n"
        f"   Majiteli odeslán přehled."
    )
    if n_skipped_hpp:
        msg += f"\n   💼 HPP přeskočeno: {n_skipped_hpp} (měsíčně /vyplata_hpp)."
    await update.callback_query.edit_message_text(msg)
    return ConversationHandler.END
