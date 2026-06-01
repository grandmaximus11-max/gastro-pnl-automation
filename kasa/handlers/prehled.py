"""/prehled — per-day shift overview + /den detail. Manager+ only (subsystem H).

Reads `Smeny` (per-shift) + `Chyby` (discrepancies) for a Sat–Fri pay-week and
shows each day with ✅ / 🔴, plus full per-day detail. Read-only.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply
from telegram.ext import ContextTypes

from kasa import auth
from kasa.kalkulace import czech_dow, pokladna_diff_from_row
from kasa.sheets import SheetsClient
from kasa.handlers.vyplata import _last_saturday


def _i(v) -> int:
    """ru_RU-safe int parse (Smeny cells come as formatted strings)."""
    try:
        s = (str(v).replace("\xa0", "").replace(" ", "")
             .replace("Kč", "").replace("kč", "").replace(",", "."))
        return int(round(float(s))) if s else 0
    except (ValueError, TypeError):
        return 0


def _fmt(n) -> str:
    return f"{int(n):,}".replace(",", " ")


def _parse_day(raw: str) -> datetime | None:
    """Flexible day input → datetime, or None if invalid.

    Accepts the relaxed forms a human actually types: 20.5.26, 20.05.2026, 20.5
    (current year), even space/slash/dash separated ('20 5 26', '20/5/26').
    This loosens ONLY the input boundary — everything downstream (display, Sheets,
    callbacks) is normalized back to strict DD.MM.YYYY via strftime."""
    parts = [p for p in re.split(r"[.\s/\-]+", (raw or "").strip()) if p]
    if len(parts) not in (2, 3):
        return None
    try:
        day, month = int(parts[0]), int(parts[1])
        if len(parts) == 3:
            y = int(parts[2])
            year = y if y >= 1000 else 2000 + y  # 26 → 2026
        else:
            year = datetime.now().year
        return datetime(year, month, day)
    except (ValueError, TypeError):
        return None


async def _is_manager(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    sheets: SheetsClient = ctx.application.bot_data["sheets"]
    emp = auth.find_by_tg_user_id(sheets.get_zamestnanci(), update.effective_user.id)
    if not emp or not auth.has_role(emp, "manager"):
        await update.effective_message.reply_text(
            "Tento přehled je jen pro manažera/majitele."
        )
        return False
    return True


def _default_week(anchor: datetime) -> tuple[datetime, datetime]:
    """Last completed Sat–Fri pay-week relative to `anchor`."""
    cur_sat = _last_saturday(anchor)
    return cur_sat - timedelta(days=7), cur_sat - timedelta(days=1)


# ── /prehled — week overview ────────────────────────────────────

async def cmd_prehled(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_manager(update, ctx):
        return
    if ctx.args:  # /prehled <datum> → the Sat–Fri week containing that date
        d = _parse_day(" ".join(ctx.args))
        if d is None:
            await update.effective_message.reply_text(
                "Datum: /přehled DD.MM.RRRR (stačí i 20.5.26)."
            )
            return
        sat = _last_saturday(d)
        await _show_prehled(update, ctx, sat, sat + timedelta(days=6))
        return
    f, t = _default_week(datetime.now())
    await _show_prehled(update, ctx, f, t)


async def _show_prehled(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                        f: datetime, t: datetime, edit: bool = False) -> None:
    sheets: SheetsClient = ctx.application.bot_data["sheets"]
    f_s, t_s = f.strftime("%d.%m.%Y"), t.strftime("%d.%m.%Y")
    smeny = sheets.read_smeny_in_range(f_s, t_s)
    chyby = sheets.read_chyby_in_range(f_s, t_s)
    smeny_by_date: dict[str, list] = {}
    for r in smeny:
        smeny_by_date.setdefault(str(r.get("datum", "")), []).append(r)
    chyby_by_date: dict[str, list] = {}
    for c in chyby:
        chyby_by_date.setdefault(str(c.get("datum", "")), []).append(c)

    lines = [f"📊 Přehled směn · {f.strftime('%d.%m')}–{t.strftime('%d.%m')}",
             "💰 tržba · 🎯 spropitné", ""]
    buttons: list[InlineKeyboardButton] = []
    n_prob = 0
    for i in range(7):
        day = f + timedelta(days=i)
        ds = day.strftime("%d.%m.%Y")
        dm = day.strftime("%d.%m")
        dow = czech_dow(ds)
        rows = smeny_by_date.get(ds, [])
        probs = chyby_by_date.get(ds, [])
        if not rows:
            lines.append(f"{dow} {dm}  —")
            continue
        # 🔴 if ANY of: a Chyby row exists, the směna was saved status≠ok, OR a
        # fresh recompute of the till-vs-POS reconciliation (from raw columns,
        # current formula) is off by ≥10 Kč. The recompute catches discrepancies
        # that were never logged to Chyby (saved before that feature) and clears
        # stale rows whose stored `rozdil` used the old e-shop formula.
        # (positive `rozdil` alone is the cash tip / přebytek — NOT a problem.)
        status_bad = str(rows[0].get("status", "ok")).strip().lower() not in ("", "ok")
        pdiff = pokladna_diff_from_row(rows[0])
        pdiff_bad = abs(pdiff) >= 10
        buttons.append(InlineKeyboardButton(f"{dow} {dm}", callback_data=f"phd:{ds}"))
        if probs or status_bad or pdiff_bad:
            n_prob += 1
            if probs:
                ptxt = ", ".join(f"{c.get('typ')} {c.get('castka')}" for c in probs)
            elif pdiff_bad:
                ptxt = f"kasa vs POS {pdiff:+} Kč"
            else:
                ptxt = f"status {rows[0].get('status')}"
            lines.append(f"{dow} {dm}  🔴 {ptxt}")
        else:
            trzba = _i(rows[0].get("trzba_bar"))
            tip = _i(rows[0].get("spropitne_celkem"))
            lines.append(f"{dow} {dm}  ✅  💰{_fmt(trzba)} · 🎯{_fmt(tip)}")
    lines += ["", f"🔴 Problémové dny: {n_prob}" if n_prob else "Vše sedí ✅"]
    if buttons:
        lines.append("Klikni na den pro detail ↓")
    kb_rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    # Week navigation — browse to ANY week, then tap a day. Forward is capped at
    # the current week so we don't page into empty future weeks.
    prev_sat = f - timedelta(days=7)
    nav = [InlineKeyboardButton("◀ Předchozí týden",
                                callback_data=f"phw:{prev_sat.strftime('%d.%m.%Y')}")]
    if f.date() < _last_saturday(datetime.now()).date():
        next_sat = f + timedelta(days=7)
        nav.append(InlineKeyboardButton("Další týden ▶",
                                        callback_data=f"phw:{next_sat.strftime('%d.%m.%Y')}"))
    kb_rows.append(nav)
    kb = InlineKeyboardMarkup(kb_rows)
    txt = "\n".join(lines)
    if edit and update.callback_query:
        try:
            await update.callback_query.edit_message_text(txt, reply_markup=kb)
        except Exception as e:
            if "not modified" not in str(e).lower():
                raise
    else:
        await update.effective_message.reply_text(txt, reply_markup=kb)


# ── /den — single-day detail ────────────────────────────────────

async def cmd_den(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_manager(update, ctx):
        return
    if not ctx.args:
        # Ask for the day and let the manager just type it (ForceReply opens the
        # input box). The reply is caught by on_den_reply. /den DD.MM.YYYY still
        # works as a direct shortcut below.
        await update.effective_message.reply_text(
            "Který den chceš vidět? Napiš datum (např. 20.5.26):",
            reply_markup=ForceReply(input_field_placeholder="20.5.26"),
        )
        return
    d = _parse_day(" ".join(ctx.args))
    if d is None:
        await update.effective_message.reply_text("Datum: /den DD.MM.RRRR (stačí i 20.5.26).")
        return
    await _show_den(update, ctx, d.strftime("%d.%m.%Y"))


async def on_den_reply(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Catch the date the manager types in reply to the /den prompt → show that
    day. Fires only for replies to OUR prompt (guarded here), so tapping a menu
    button instead never gets mis-eaten."""
    msg = update.message
    rtm = msg.reply_to_message if msg else None
    if not rtm or not str(rtm.text or "").startswith("Který den"):
        return  # not a reply to the /den prompt — leave it for other handlers
    if not await _is_manager(update, ctx):
        return
    d = _parse_day(msg.text or "")
    if d is None:
        await msg.reply_text(
            "To nevypadá jako datum. Zkus třeba 20.5.26:",
            reply_markup=ForceReply(input_field_placeholder="20.5.26"),
        )
        return
    await _show_den(update, ctx, d.strftime("%d.%m.%Y"))


async def on_prehled_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if data.startswith("phd:"):
        await _show_den(update, ctx, data.split(":", 1)[1], edit=True)
    elif data.startswith("phb:"):  # back to the week overview containing that day
        d = datetime.strptime(data.split(":", 1)[1], "%d.%m.%Y")
        sat = _last_saturday(d)
        await _show_prehled(update, ctx, sat, sat + timedelta(days=6), edit=True)
    elif data.startswith("phw:"):  # week navigation → jump to that Sat–Fri week
        sat = datetime.strptime(data.split(":", 1)[1], "%d.%m.%Y")
        await _show_prehled(update, ctx, sat, sat + timedelta(days=6), edit=True)


async def _show_den(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                    ds: str, edit: bool = False) -> None:
    sheets: SheetsClient = ctx.application.bot_data["sheets"]
    # Day-step navigation (◀ prev / next ▶, crossing week boundaries) + back to
    # the week grid. Lets the manager walk to ANY day from ANY week. Next is
    # capped at today (no future days).
    d = datetime.strptime(ds, "%d.%m.%Y")
    prev_d, next_d = d - timedelta(days=1), d + timedelta(days=1)
    nav_row = [InlineKeyboardButton(f"◀ {prev_d.strftime('%d.%m')}",
                                    callback_data=f"phd:{prev_d.strftime('%d.%m.%Y')}")]
    if d.date() < datetime.now().date():
        nav_row.append(InlineKeyboardButton(f"{next_d.strftime('%d.%m')} ▶",
                                            callback_data=f"phd:{next_d.strftime('%d.%m.%Y')}"))
    kb = InlineKeyboardMarkup(
        [nav_row, [InlineKeyboardButton("← Zpět na přehled", callback_data=f"phb:{ds}")]]
    )
    rows = sheets.read_smeny_in_range(ds, ds)
    if not rows:
        msg = f"📋 {ds} ({czech_dow(ds)}) — žádná směna."
        if edit and update.callback_query:
            await update.callback_query.edit_message_text(msg, reply_markup=kb)
        else:
            await update.effective_message.reply_text(msg, reply_markup=kb)
        return
    row = rows[0]
    chyby = sheets.read_chyby_in_range(ds, ds)
    lidi = []
    for i in (1, 2, 3):
        jm = str(row.get(f"jmeno_{i}", "")).strip()
        if not jm:
            continue
        h = row.get(f"hodiny_{i}")
        uc = _i(row.get(f"pers_ucet_{i}"))
        if _i(h) == 0 and uc:
            extra = f" (jen účet −{uc})"
        elif uc:
            extra = f" · účet {uc}"
        else:
            extra = ""
        lidi.append(f"{jm} {h}h{extra}")
    # Day totals (Σ za den) — sums of per-person columns. Money via _i; hours
    # need a float sum (7.5h must not round to 8).
    _hsum = 0.0
    for i in (1, 2, 3):
        _hs = str(row.get(f"hodiny_{i}", "")).replace(",", ".").strip()
        try:
            _hsum += float(_hs) if _hs else 0.0
        except ValueError:
            pass
    _psum = sum(_i(row.get(f"plat_{i}")) for i in (1, 2, 3))
    _tsum = sum(_i(row.get(f"spropitne_{i}")) for i in (1, 2, 3))
    _usum = sum(_i(row.get(f"pers_ucet_{i}")) for i in (1, 2, 3))
    _kvsum = sum(_i(row.get(f"k_vyplate_{i}")) for i in (1, 2, 3))
    lines = [
        f"📋 Směna {ds} ({czech_dow(ds)})",
        f"Zodpovědný: {row.get('zodpovedny', '?')} · typ: {row.get('typ', '?')}",
        "",
        "Lidé: " + (" · ".join(lidi) or "—"),
        "",
        f"💳 Tržba karta: {_fmt(_i(row.get('karta_pos')))} "
        f"(+tip {_fmt(_i(row.get('spropitne_karta')))})",
        f"💵 Tržba hotově: {_fmt(_i(row.get('trzba_pos_hot')))}",
        f"🛒 E-shop: {_fmt(_i(row.get('eshop_celkem')))}",
        f"🎯 Spropitné: {_fmt(_i(row.get('spropitne_celkem')))} "
        f"(💵{_i(row.get('spropitne_hotov'))} · 💳{_i(row.get('spropitne_karta'))})",
        f"🏦 Start {_fmt(_i(row.get('hot_zac_celkem')))} → "
        f"konec {_fmt(_i(row.get('hot_kon_celkem')))}",
        "",
        "Σ za den:",
        f"  ⏱ Hodiny:    {_hsum:g} h",
        f"  💼 Mzda:      {_fmt(_psum)} Kč",
        f"  🎯 Spropitné: {_fmt(_tsum)} Kč",
        f"  🧾 Účty:      {_fmt(_usum)} Kč",
        f"  💰 K výplatě: {_fmt(_kvsum)} Kč",
    ]
    for c in chyby:
        lines.append(f"🔴 {c.get('typ')}: {c.get('castka')} Kč — {c.get('popis', '')}")
    # Fresh recompute — surfaces a till-vs-POS gap even when nothing was logged
    # to Chyby (e.g. saved before discrepancy-logging existed).
    pdiff = pokladna_diff_from_row(row)
    if not chyby and abs(pdiff) >= 10:
        kde = "chybí v obálce" if pdiff < 0 else "přebytek navíc"
        lines.append(f"🔴 Pokladna vs POS: {pdiff:+} Kč ({kde})")
    url = str(row.get("drive_folder_url", "")).strip()
    if url:
        lines.append(f"📁 {url}")
    txt = "\n".join(lines)
    if edit and update.callback_query:
        try:
            await update.callback_query.edit_message_text(txt, reply_markup=kb)
        except Exception as e:
            if "not modified" not in str(e).lower():
                raise
    else:
        await update.effective_message.reply_text(txt, reply_markup=kb)
