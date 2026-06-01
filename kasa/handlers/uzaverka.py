"""/uzaverka — shift close-out wizard. Pure logic + ConversationHandler glue."""
from __future__ import annotations

import copy
import os
import re
import tempfile
from datetime import datetime, timedelta

from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from kasa import auth
from kasa.claude import classify_naklad
from kasa.config import (
    SAZBA_SOLO, SAZBA_VICE, KROK_HODIN, DRIVE_ROOT_UZAVERKY,
    BUSINESS_DAY_CUTOFF_HOUR, DEFAULT_HOT_ZAC,
)
from kasa.kalkulace import expected_konec, rozdil_a_tip_hotove, tip_per_hour, k_vyplate_per_person
from kasa.notify import push_chyba_alert, push_overnight_alert, push_pokladna_diff_alert

# ── Pure parser helpers ─────────────────────────────────────────

def parse_hours(raw: str) -> float:
    raw = raw.strip().replace(",", ".")
    try:
        h = float(raw)
    except ValueError as e:
        raise ValueError(f"Nelze přečíst hodiny: {raw}") from e
    # Validate 0.25 step
    if abs((h / KROK_HODIN) - round(h / KROK_HODIN)) > 1e-6:
        raise ValueError(f"Hodiny musí být v krocích po {KROK_HODIN} (např. 9.0, 9.25, 9.5).")
    return round(h / KROK_HODIN) * KROK_HODIN


def parse_kc(raw: str) -> int:
    """Strip ' Kč', spaces, thousand separators. Return int Kč."""
    s = re.sub(r"[Kk]č", "", raw).strip()
    s = s.replace(" ", "").replace("\xa0", "")
    # Any non-digit (except minus) is treated as junk (e.g., '.' or ',' as thousand separator)
    s = re.sub(r"[^\d-]", "", s)
    return int(s) if s else 0


def decide_sazba(typ: str) -> int:
    return SAZBA_SOLO if typ == "solo" else SAZBA_VICE


# ── Back-navigation (universal undo) ────────────────────────────
# Mechanism: a per-step snapshot stack. Before a step's handler mutates the
# wizard state we deep-copy the whole `user_data` (minus the stack itself). The
# "↩️ Zpět" affordance pops the last snapshot, restores it wholesale, and replays
# the previous step's prompt via the _ASK registry. Wholesale restore means
# going back across the e-shop / náklady cycles un-adds the just-appended item,
# and going back across the per-person loop restores the previous worker — no
# per-field reasoning needed.
#
# Two surfaces, because a Telegram message can carry EITHER a reply keyboard OR
# an inline keyboard, never both:
#   • text / reply-keyboard steps → "↩️ Zpět" as a reply-keyboard row (text)
#   • photo steps                 → "↩️ Zpět" as an inline button (callback)

_BACK = "↩️ Zpět"
_BACK_CB = "nav:back"


def _is_back_text(update: Update) -> bool:
    msg = getattr(update, "message", None)
    return bool(msg and msg.text and msg.text.strip() == _BACK)


def _kb_back(rows: list[list[str]] | None = None) -> ReplyKeyboardMarkup:
    """Reply keyboard = given rows + a trailing standalone '↩️ Zpět' row."""
    rows = list(rows or [])
    rows.append([_BACK])
    return ReplyKeyboardMarkup(rows, one_time_keyboard=True, resize_keyboard=True)


def _inline_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(_BACK, callback_data=_BACK_CB)]])


def _snapshot(ctx: ContextTypes.DEFAULT_TYPE) -> dict:
    """Deep copy of wizard state, excluding the history stack itself."""
    return copy.deepcopy({k: v for k, v in ctx.user_data.items() if k != "_hist"})


def _push(ctx: ContextTypes.DEFAULT_TYPE, ask_key: str, snap: dict) -> None:
    """Record a resume point: (ask_key, state-as-it-was-when-that-step-was-shown).

    `snap` must be captured at the TOP of the current handler (before it mutates
    anything), so replaying `ask_key` restores exactly the state the user saw."""
    ctx.user_data.setdefault("_hist", []).append((ask_key, snap))


async def _go_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Pop the last resume point, restore its snapshot, replay its prompt."""
    if update.callback_query:
        await update.callback_query.answer()
    hist = ctx.user_data.get("_hist") or []
    if not hist:
        # At the very first reversible step — nothing to undo. Replay typ.
        return await _ask_typ(update, ctx)
    ask_key, snap = hist.pop()
    ctx.user_data.clear()
    ctx.user_data.update(snap)
    ctx.user_data["_hist"] = hist
    return await _ASK[ask_key](update, ctx)


# ── States ──────────────────────────────────────────────────────
# 34 states. New states are APPENDED (never inserted in the middle) so the
# integer value of existing states never shifts — persisted in-flight
# conversations in kasa_state.pkl stay valid across restarts.
# Z_TIP_VISA was added last (=33) for the MasterCard/Visa tip split.

(
    Z_TYP, Z_POCET, Z_JMENO, Z_HODINY,
    Z_KARTA, Z_TIP_KARTA, Z_HOT_ZAC, Z_TRZBA_POS,
    Z_ESHOP_CASTKA, Z_ESHOP_POPIS, Z_ESHOP_ZPUSOB, Z_ESHOP_MORE,
    Z_NAKLAD_CASTKA, Z_NAKLAD_POPIS, Z_NAKLAD_DOKLAD, Z_NAKLAD_FOTO, Z_NAKLAD_MORE,
    Z_PERS_UCET,
    Z_ZAL_JMENO, Z_ZAL_CASTKA, Z_ZAL_POPIS, Z_ZAL_VRACI, Z_ZAL_MORE,
    Z_HOT_5000, Z_HOT_2000, Z_HOT_1000, Z_HOT_500, Z_HOT_200, Z_HOT_100, Z_HOT_MINCE,
    Z_RECON_CHOICE, Z_CHYBA_POPIS,
    Z_CONFIRM,
    Z_TIP_VISA,
    Z_TIP_CONFIRM,
    Z_XUCET_JMENO,
    Z_XUCET_CASTKA,
) = range(37)


# ── Handlers — Phase 1 (typ směny, počet, lidé) ─────────────────

async def cmd_uzaverka(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for /uzaverka. Resets wizard state.

    Date resolution (priority order):
      1. Explicit arg `/uzaverka 20.05.2026` → that date (backfill / override).
      2. Business-day cutoff: closing before 06:00 → previous calendar day
         (bar works past midnight; 01:30 close-out is still yesterday's shift).
      3. Otherwise → today.
    Invalid arg → warn and fall back to the business-day default.

    Access: activated employee with role bartender+ only. find_by_tg_user_id
    matches on tg_user_id, which is set only at activation — so an unmatched id
    means "not activated". This closes the gap where anyone could close a shift.
    """
    sheets = ctx.application.bot_data["sheets"]
    emp = auth.find_by_tg_user_id(sheets.get_zamestnanci(), update.effective_user.id)
    if not emp or not auth.has_role(emp, "bartender"):
        await update.message.reply_text(
            "Tento příkaz je jen pro zaměstnance. Nejdřív se aktivuj přes /start."
        )
        return ConversationHandler.END

    now = datetime.now()
    # Business-day cutoff (handles after-midnight close-out)
    if now.hour < BUSINESS_DAY_CUTOFF_HOUR:
        biz_dt = now - timedelta(days=1)
        auto_shifted = True
    else:
        biz_dt = now
        auto_shifted = False
    datum = biz_dt.strftime("%d.%m.%Y")

    explicit = False
    if ctx.args:
        candidate = ctx.args[0].strip()
        try:
            datetime.strptime(candidate, "%d.%m.%Y")
            datum = candidate
            explicit = True
            auto_shifted = False  # explicit arg wins — no auto-shift note
        except ValueError:
            await update.message.reply_text(
                f"⚠️ Neplatné datum '{candidate}'. Formát: DD.MM.YYYY "
                f"(např. /uzaverka 20.05.2026).\n"
                f"Pokračuji s datem {datum}."
            )

    # Fresh wizard: wipe any leftover state (incl. a stale back-history stack)
    # from a previous run, then seed the new shift + empty history.
    ctx.user_data.clear()
    ctx.user_data["_hist"] = []
    ctx.user_data["smena"] = {
        "datum": datum,
        "smena_id": datum,
        "lidi": [],
        "naklady": [],
        "eshop_items": [],
        "zalohy": [],
        "created_by_tg": update.effective_user.username or str(update.effective_user.id),
    }
    kb = ReplyKeyboardMarkup(
        [["Solo 160 Kč/h", "Více 140 Kč/h"]],
        one_time_keyboard=True, resize_keyboard=True,
    )
    if explicit:
        header = f"Začínáme uzávěrku za {datum} 📅 (zpětný zápis)."
    elif auto_shifted:
        header = (
            f"Začínáme uzávěrku za {datum} 🌙\n"
            f"(uzávěrka po půlnoci — směna z včerejška. "
            f"Jiné datum: /uzaverka DD.MM.YYYY)"
        )
    else:
        header = "Začínáme uzávěrku."
    await update.message.reply_text(f"{header} Jaký typ směny?", reply_markup=kb)
    return Z_TYP


async def _ask_typ(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    kb = ReplyKeyboardMarkup(
        [["Solo 160 Kč/h", "Více 140 Kč/h"]],
        one_time_keyboard=True, resize_keyboard=True,
    )
    await update.effective_message.reply_text("Jaký typ směny?", reply_markup=kb)
    return Z_TYP


async def on_typ(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if _is_back_text(update):
        return await _go_back(update, ctx)
    snap = _snapshot(ctx)
    text = update.message.text.lower()
    if "solo" in text:
        ctx.user_data["smena"]["typ"] = "solo"
        ctx.user_data["smena"]["sazba_h"] = SAZBA_SOLO
        ctx.user_data["smena"]["pocet_lidi"] = 1
        ctx.user_data["_lidi_idx"] = 0
        _push(ctx, "typ", snap)
        return await _ask_jmeno(update, ctx)
    ctx.user_data["smena"]["typ"] = "vice"
    ctx.user_data["smena"]["sazba_h"] = SAZBA_VICE
    _push(ctx, "typ", snap)
    return await _ask_pocet(update, ctx)


async def _ask_pocet(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text(
        "Kolik vás dnes pracovalo?", reply_markup=_kb_back([["2", "3", "4"]]),
    )
    return Z_POCET


async def on_pocet(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if _is_back_text(update):
        return await _go_back(update, ctx)
    try:
        n = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Napiš číslo. Kolik lidí?")
        return Z_POCET
    snap = _snapshot(ctx)
    ctx.user_data["smena"]["pocet_lidi"] = n
    ctx.user_data["_lidi_idx"] = 0
    _push(ctx, "pocet", snap)
    return await _ask_jmeno(update, ctx)


async def _ask_jmeno(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Re-renders the name prompt for the CURRENT worker (index = len(lidi))."""
    smena = ctx.user_data["smena"]
    idx = len(smena["lidi"])
    if smena.get("typ") == "solo":
        # No "Vlastní jméno" on solo: solo shifts are worked only by regular
        # staff. A custom name can still be typed if ever needed.
        rows = [["Hugo", "Lena"], ["Mia", "Adam"]]
        prompt = "Solo směna 👍 Jak se jmenuješ? (klikni nebo napiš)"
    else:
        rows = [["Hugo", "Lena"], ["Mia", "Adam"], ["Vlastní jméno"]]
        prompt = f"Jméno {idx + 1}. zaměstnance? (klikni nebo napiš)"
    await update.effective_message.reply_text(prompt, reply_markup=_kb_back(rows))
    return Z_JMENO


async def on_jmeno(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if _is_back_text(update):
        return await _go_back(update, ctx)
    jmeno = update.message.text.strip()
    if jmeno == "Vlastní jméno":
        await update.message.reply_text("Napiš jméno:")
        return Z_JMENO  # stay (same step, no history push)
    snap = _snapshot(ctx)
    ctx.user_data["_curr_jmeno"] = jmeno
    _push(ctx, "jmeno", snap)
    return await _ask_hodiny(update, ctx)


async def _ask_hodiny(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    jmeno = ctx.user_data.get("_curr_jmeno", "")
    await update.effective_message.reply_text(
        f"{jmeno} — kolik hodin? (krok 0.25)", reply_markup=_kb_back(),
    )
    return Z_HODINY


async def on_hodiny(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if _is_back_text(update):
        return await _go_back(update, ctx)
    try:
        h = parse_hours(update.message.text)
    except ValueError as e:
        await update.message.reply_text(f"❌ {e}")
        return Z_HODINY
    snap = _snapshot(ctx)
    smena = ctx.user_data["smena"]
    smena["lidi"].append({
        "jmeno": ctx.user_data.get("_curr_jmeno"),
        "hodiny": h,
        "pers_ucet": 0,
        "zaloha": 0,
    })
    ctx.user_data.pop("_curr_jmeno", None)
    _push(ctx, "hodiny", snap)
    if len(smena["lidi"]) < smena["pocet_lidi"]:
        return await _ask_jmeno(update, ctx)
    return await _ask_karta(update, ctx)


async def _ask_karta(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text(
        "Tržba na kartě (celkem z terminálu, včetně tipu)?", reply_markup=_kb_back(),
    )
    return Z_KARTA


async def on_karta(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if _is_back_text(update):
        return await _go_back(update, ctx)
    try:
        v = parse_kc(update.message.text)
    except ValueError:
        await update.message.reply_text("Napiš prosím číslo v Kč.")
        return Z_KARTA
    snap = _snapshot(ctx)
    ctx.user_data["smena"]["karta"] = v
    _push(ctx, "karta", snap)
    return await _ask_tip_mc(update, ctx)


async def _ask_tip_mc(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text(
        "A z toho spropitné kartou — MasterCard?", reply_markup=_kb_back(),
    )
    return Z_TIP_KARTA


async def on_tip_mc(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if _is_back_text(update):
        return await _go_back(update, ctx)
    try:
        v = parse_kc(update.message.text)
    except ValueError:
        await update.message.reply_text("Napiš prosím číslo v Kč.")
        return Z_TIP_KARTA
    snap = _snapshot(ctx)
    ctx.user_data["smena"]["spropitne_mc"] = v
    _push(ctx, "tip_mc", snap)
    return await _ask_tip_visa(update, ctx)


async def _ask_tip_visa(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text(
        "A spropitné kartou — Visa?", reply_markup=_kb_back(),
    )
    return Z_TIP_VISA


async def on_tip_visa(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if _is_back_text(update):
        return await _go_back(update, ctx)
    try:
        v = parse_kc(update.message.text)
    except ValueError:
        await update.message.reply_text("Napiš prosím číslo v Kč.")
        return Z_TIP_VISA
    snap = _snapshot(ctx)
    smena = ctx.user_data["smena"]
    smena["spropitne_visa"] = v
    # Total card tips = MC + Visa. spropitne_karta stays the single persisted
    # field (Smeny schema unchanged); the MC/Visa split is shown everywhere.
    smena["spropitne_karta"] = int(smena.get("spropitne_mc", 0)) + v
    _push(ctx, "tip_visa", snap)
    return await _ask_tip_confirm(update, ctx)


async def _ask_tip_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Show combined card tips (MC + Visa) and confirm — money-step pattern like
    the fond confirmation."""
    smena = ctx.user_data["smena"]
    mc = int(smena.get("spropitne_mc", 0))
    visa = int(smena.get("spropitne_visa", 0))
    msg = (
        f"Spropitné kartou celkem: {mc + visa:,} Kč\n"
        f"  MasterCard: {mc:,} Kč\n"
        f"  Visa:       {visa:,} Kč\n\n"
        f"Sedí?"
    )
    await update.effective_message.reply_text(
        msg, reply_markup=_kb_back([["OK", "Změnit"]]),
    )
    return Z_TIP_CONFIRM


async def on_tip_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if _is_back_text(update):
        return await _go_back(update, ctx)
    text = update.message.text.strip()
    if text == "Změnit":
        # Re-enter from MasterCard. Old mc/visa get overwritten.
        return await _ask_tip_mc(update, ctx)
    # OK (or anything else) → proceed. Compute the fond carry-over here (last
    # step before the fond confirm). See SheetsClient.read_last_smena_carryover —
    # the REAL ending fond from the previous shift, NOT a hardcoded 5000.
    snap = _snapshot(ctx)
    sheets = ctx.application.bot_data["sheets"]
    prev_fond, prev_mince = DEFAULT_HOT_ZAC, 0
    try:
        carry = sheets.read_last_smena_carryover()
        prev_fond = carry["fond"]
        prev_mince = carry["mince"]
    except Exception:
        prev_fond, prev_mince = DEFAULT_HOT_ZAC, 0
    ctx.user_data["_default_hot_zac_fond"] = prev_fond
    ctx.user_data["_default_hot_zac_mince"] = prev_mince
    _push(ctx, "tip_confirm", snap)
    return await _ask_hot_zac(update, ctx)


async def _ask_hot_zac(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    # Reset any half-finished manual sub-entry on (re)entry, so "back" into this
    # step always lands on the clean confirmation prompt.
    ctx.user_data.pop("_hot_zac_sub", None)
    prev_fond = ctx.user_data.get("_default_hot_zac_fond", DEFAULT_HOT_ZAC)
    prev_mince = ctx.user_data.get("_default_hot_zac_mince", 0)
    default_total = prev_fond + prev_mince
    msg = (
        f"Začáteční hotovost — přenos z minulé směny:\n"
        f"  Bankovky (fond): {prev_fond:,} Kč\n"
        f"  Mince: {prev_mince:,} Kč\n"
        f"  ───────────\n"
        f"  Celkem: {default_total:,} Kč\n\n"
        f"💡 Hledej v Dotykačce «Otevření pokladny: Hotovost — XXX»\n"
        f"Sedí?"
    )
    await update.effective_message.reply_text(
        msg, reply_markup=_kb_back([[f"OK {default_total:,} Kč", "Změnit"]]),
    )
    return Z_HOT_ZAC


async def on_hot_zac(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if _is_back_text(update):
        return await _go_back(update, ctx)
    sub = ctx.user_data.get("_hot_zac_sub")
    text = update.message.text.strip()
    smena = ctx.user_data["smena"]

    # First entry to this state — handle OK auto-default / Změnit / direct number
    if sub is None:
        snap = _snapshot(ctx)  # state of the clean confirm prompt
        if text.startswith("OK"):
            # Accept the proposed carry-over as-is (defaults NOT popped here —
            # _finalize_hot_zac reads them for the overnight comparison).
            fond_default = ctx.user_data.get("_default_hot_zac_fond", DEFAULT_HOT_ZAC)
            mince_default = ctx.user_data.get("_default_hot_zac_mince", 0)
            smena["hot_zac_celkem"] = fond_default + mince_default
            smena["_fond"] = fond_default
            smena["_mince_stays"] = mince_default
            _push(ctx, "hot_zac", snap)
            return await _finalize_hot_zac(update, ctx)
        if text == "Změnit":
            # Manual 2-step entry (bills then mince). Push so "back" from the
            # sub-entry returns to this clean confirm.
            ctx.user_data["_hot_zac_sub"] = "bills"
            _push(ctx, "hot_zac", snap)
            await update.message.reply_text(
                "Napiš začáteční bankovky v Kč:", reply_markup=_kb_back(),
            )
            return Z_HOT_ZAC
        # Direct number — treat as bankovky, proceed to mince
        try:
            smena["_hot_zac_bankovky"] = parse_kc(text)
        except ValueError:
            await update.message.reply_text("Napiš prosím číslo v Kč nebo klikni OK / Změnit.")
            return Z_HOT_ZAC
        ctx.user_data["_hot_zac_sub"] = "mince"
        _push(ctx, "hot_zac", snap)
        await update.message.reply_text(
            "A kolik je mince na začátku? (Kč)", reply_markup=_kb_back(),
        )
        return Z_HOT_ZAC

    if sub == "bills":
        try:
            smena["_hot_zac_bankovky"] = parse_kc(text)
        except ValueError:
            await update.message.reply_text("Napiš prosím číslo v Kč.")
            return Z_HOT_ZAC
        ctx.user_data["_hot_zac_sub"] = "mince"
        await update.message.reply_text(
            "A kolik je mince na začátku? (Kč)", reply_markup=_kb_back(),
        )
        return Z_HOT_ZAC

    if sub == "mince":
        try:
            mince = parse_kc(text)
        except ValueError:
            await update.message.reply_text("Napiš prosím číslo v Kč.")
            return Z_HOT_ZAC
        bankovky = smena.get("_hot_zac_bankovky", DEFAULT_HOT_ZAC)
        smena["hot_zac_celkem"] = bankovky + mince
        smena["_fond"] = bankovky
        smena["_mince_stays"] = mince
        # Manual entry → confirm the total before proceeding (the money-step
        # "Sedí?" pattern), instead of jumping straight to tržba.
        ctx.user_data["_hot_zac_sub"] = "confirm"
        return await _ask_hot_zac_confirm(update, ctx)

    # sub == "confirm" — manually-entered total awaiting Sedí?
    if text == "Změnit":
        ctx.user_data["_hot_zac_sub"] = "bills"
        await update.message.reply_text(
            "Napiš začáteční bankovky v Kč:", reply_markup=_kb_back(),
        )
        return Z_HOT_ZAC
    return await _finalize_hot_zac(update, ctx)


async def _ask_hot_zac_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Confirm the MANUALLY entered starting cash (Sedí?) before proceeding."""
    smena = ctx.user_data["smena"]
    bankovky = int(smena.get("_fond", 0))
    mince = int(smena.get("_mince_stays", 0))
    await update.effective_message.reply_text(
        f"Začáteční hotovost:\n"
        f"  Bankovky: {bankovky:,} Kč\n"
        f"  Mince: {mince:,} Kč\n"
        f"  ───────────\n"
        f"  Celkem: {bankovky + mince:,} Kč\n\n"
        f"Sedí?",
        reply_markup=_kb_back([["OK", "Změnit"]]),
    )
    return Z_HOT_ZAC


async def _finalize_hot_zac(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Lock in the starting cash. If it differs from the previous shift's end
    (the proposed carry-over), record an overnight discrepancy — the manager is
    notified at save time (on_confirm). OK-on-proposal → diff 0 → no flag."""
    smena = ctx.user_data["smena"]
    proposed = (int(ctx.user_data.get("_default_hot_zac_fond", DEFAULT_HOT_ZAC))
                + int(ctx.user_data.get("_default_hot_zac_mince", 0)))
    entered = int(smena.get("hot_zac_celkem", proposed))
    diff = entered - proposed
    if diff != 0:
        smena["overnight_rozdil"] = diff
        smena["overnight_proposed"] = proposed
        smena["overnight_entered"] = entered
    ctx.user_data.pop("_hot_zac_sub", None)
    ctx.user_data.pop("_default_hot_zac_fond", None)
    ctx.user_data.pop("_default_hot_zac_mince", None)
    smena.pop("_hot_zac_bankovky", None)
    return await _ask_trzba_pos(update, ctx)


async def _ask_trzba_pos(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text(
        "Skvělé 👍\n\n"
        "Teď tržba hotově z Dotykačky.\n"
        "💡 Na POS uzávěrce hledej řádek:\n"
        "    «Tržba: Hotovost (počet) — XXX,XX»\n"
        "(NE «Uzavření pokladny» — to je celkový obsah kasy)",
        reply_markup=_kb_back(),
    )
    return Z_TRZBA_POS


async def on_trzba_pos(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if _is_back_text(update):
        return await _go_back(update, ctx)
    try:
        v = parse_kc(update.message.text)
    except ValueError:
        await update.message.reply_text("Napiš číslo v Kč.")
        return Z_TRZBA_POS
    snap = _snapshot(ctx)
    ctx.user_data["smena"]["trzba_pos_hot"] = v
    _push(ctx, "trzba_pos", snap)
    return await _ask_eshop(update, ctx)


# ── Phase 3 — Eshop cycle ──────────────────────────────────────

async def _ask_eshop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """E-shop decision prompt (Ano / Žádný eshop)."""
    await update.effective_message.reply_text(
        "Byly nějaké prodeje eshop (tabák, uhlí)?",
        reply_markup=_kb_back([["Ano", "Žádný eshop"]]),
    )
    return Z_ESHOP_CASTKA


async def _ask_eshop_castka(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    n = len(ctx.user_data["smena"]["eshop_items"])
    word = "první" if n == 0 else "další"
    await update.effective_message.reply_text(
        f"Kolik Kč za {word} prodej?", reply_markup=_kb_back(),
    )
    return Z_ESHOP_CASTKA


async def on_eshop_castka(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if _is_back_text(update):
        return await _go_back(update, ctx)
    text = update.message.text.strip()
    if text == "Žádný eshop":
        snap = _snapshot(ctx)
        _push(ctx, "eshop_decision", snap)
        return await _ask_naklady(update, ctx)
    if text == "Ano":
        snap = _snapshot(ctx)
        _push(ctx, "eshop_decision", snap)
        return await _ask_eshop_castka(update, ctx)
    castka = parse_kc(text)
    if castka <= 0:
        await update.message.reply_text("Napiš prosím číslo v Kč (větší než 0).")
        return Z_ESHOP_CASTKA
    snap = _snapshot(ctx)
    ctx.user_data["_eshop_curr"] = {"castka": castka}
    _push(ctx, "eshop_castka", snap)
    return await _ask_eshop_popis(update, ctx)


async def _ask_eshop_popis(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text(
        "Popis prodeje (co se prodalo):", reply_markup=_kb_back(),
    )
    return Z_ESHOP_POPIS


async def on_eshop_popis(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if _is_back_text(update):
        return await _go_back(update, ctx)
    snap = _snapshot(ctx)
    ctx.user_data["_eshop_curr"]["popis"] = update.message.text.strip()
    _push(ctx, "eshop_popis", snap)
    return await _ask_eshop_zpusob(update, ctx)


async def _ask_eshop_zpusob(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text(
        "Hotově nebo kartou?", reply_markup=_kb_back([["Hotově", "Kartou"]]),
    )
    return Z_ESHOP_ZPUSOB


async def on_eshop_zpusob(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if _is_back_text(update):
        return await _go_back(update, ctx)
    snap = _snapshot(ctx)
    text = update.message.text.strip().lower()
    zpusob = "hotove" if "hotov" in text else "karta"
    ctx.user_data["_eshop_curr"]["zpusob"] = zpusob
    ctx.user_data["smena"]["eshop_items"].append(ctx.user_data.pop("_eshop_curr"))
    _push(ctx, "eshop_zpusob", snap)
    return await _ask_eshop_more(update, ctx)


async def _ask_eshop_more(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text(
        "Eshop zaznamenán ✓", reply_markup=_kb_back([["Přidat další", "Skončit eshop"]]),
    )
    return Z_ESHOP_MORE


async def on_eshop_more(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if _is_back_text(update):
        return await _go_back(update, ctx)
    snap = _snapshot(ctx)
    _push(ctx, "eshop_more", snap)
    if "Přidat" in update.message.text:
        return await _ask_eshop_castka(update, ctx)
    return await _ask_naklady(update, ctx)


async def _ask_naklady(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text(
        "Byly nějaké náklady z pokladny?",
        reply_markup=_kb_back([["Ano", "Žádné náklady"]]),
    )
    return Z_NAKLAD_CASTKA


# ── Phase 3 — Náklady cycle (with optional doklad foto) ────────

async def _ask_naklad_castka(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    n = len(ctx.user_data["smena"]["naklady"])
    word = "první" if n == 0 else "další"
    await update.effective_message.reply_text(
        f"Kolik Kč za {word} náklad?", reply_markup=_kb_back(),
    )
    return Z_NAKLAD_CASTKA


async def on_naklad_castka(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if _is_back_text(update):
        return await _go_back(update, ctx)
    text = update.message.text.strip()
    if text == "Žádné náklady":
        snap = _snapshot(ctx)
        _push(ctx, "naklad_decision", snap)
        return await _ask_personal_ucty(update, ctx)
    if text == "Ano":
        snap = _snapshot(ctx)
        _push(ctx, "naklad_decision", snap)
        return await _ask_naklad_castka(update, ctx)
    castka = parse_kc(text)
    if castka <= 0:
        await update.message.reply_text("Napiš prosím číslo v Kč (větší než 0).")
        return Z_NAKLAD_CASTKA
    snap = _snapshot(ctx)
    ctx.user_data["_naklad_curr"] = {"castka": castka}
    _push(ctx, "naklad_castka", snap)
    return await _ask_naklad_popis(update, ctx)


async def _ask_naklad_popis(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text(
        "Popis nákladu (co a u koho):", reply_markup=_kb_back(),
    )
    return Z_NAKLAD_POPIS


async def on_naklad_popis(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if _is_back_text(update):
        return await _go_back(update, ctx)
    snap = _snapshot(ctx)
    ctx.user_data["_naklad_curr"]["popis"] = update.message.text.strip()
    _push(ctx, "naklad_popis", snap)
    return await _ask_naklad_doklad(update, ctx)


async def _ask_naklad_doklad(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text(
        "Máš doklad?", reply_markup=_kb_back([["Mám doklad", "Bez dokladu"]]),
    )
    return Z_NAKLAD_DOKLAD


async def on_naklad_doklad(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if _is_back_text(update):
        return await _go_back(update, ctx)
    snap = _snapshot(ctx)
    if "Mám" in update.message.text:
        _push(ctx, "naklad_doklad", snap)
        return await _ask_naklad_foto(update, ctx)
    ctx.user_data["_naklad_curr"]["ma_doklad"] = False
    ctx.user_data["smena"]["naklady"].append(ctx.user_data.pop("_naklad_curr"))
    _push(ctx, "naklad_doklad", snap)
    return await _ask_naklad_more(update, ctx)


async def _ask_naklad_foto(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    # Photo step → inline "↩️ Zpět" (a photo prompt can't host a reply keyboard).
    await update.effective_message.reply_text(
        "Super, pošli prosím foto dokladu:", reply_markup=_inline_back(),
    )
    return Z_NAKLAD_FOTO


async def on_naklad_foto(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    # Save photo file_id; upload to Drive happens at finalization (Task 18)
    photo = update.message.photo[-1] if update.message.photo else None
    document = update.message.document
    if not (photo or document):
        await update.message.reply_text(
            "Pošli foto nebo PDF jako přílohu:", reply_markup=_inline_back(),
        )
        return Z_NAKLAD_FOTO
    snap = _snapshot(ctx)
    curr = ctx.user_data["_naklad_curr"]
    if photo:
        curr["doklad_file_id"] = photo.file_id
        curr["doklad_kind"] = "photo"
    else:
        curr["doklad_file_id"] = document.file_id
        curr["doklad_kind"] = "doc"
        curr["doklad_name"] = document.file_name or "doklad.pdf"
    curr["ma_doklad"] = True
    ctx.user_data["smena"]["naklady"].append(ctx.user_data.pop("_naklad_curr"))
    _push(ctx, "naklad_foto", snap)
    return await _ask_naklad_more(update, ctx)


async def _ask_naklad_more(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text(
        "Náklad uložený ✓", reply_markup=_kb_back([["Přidat další", "Skončit náklady"]]),
    )
    return Z_NAKLAD_MORE


async def on_naklad_more(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if _is_back_text(update):
        return await _go_back(update, ctx)
    snap = _snapshot(ctx)
    _push(ctx, "naklad_more", snap)
    if "Přidat" in update.message.text:
        return await _ask_naklad_castka(update, ctx)
    return await _ask_personal_ucty(update, ctx)


# ── Phase 3 — Personal účty (one per worker) ───────────────────

async def _ask_personal_ucty(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data["_pers_idx"] = 0
    return await _ask_pers_ucet(update, ctx)


async def _ask_pers_ucet(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Re-renders the personal-účet prompt for the CURRENT worker (_pers_idx)."""
    smena = ctx.user_data["smena"]
    idx = ctx.user_data.get("_pers_idx", 0)
    person = smena["lidi"][idx]
    if idx == 0:
        prompt = (
            f"Personální účet — {person['jmeno']}? "
            f"(souhrn Kč, sleva 50% už zohledněna; napiš 0 pokud nic)"
        )
    else:
        prompt = f"Personální účet — {person['jmeno']}?"
    await update.effective_message.reply_text(prompt, reply_markup=_kb_back())
    return Z_PERS_UCET


async def on_pers_ucet(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if _is_back_text(update):
        return await _go_back(update, ctx)
    try:
        v = parse_kc(update.message.text)
    except ValueError:
        await update.message.reply_text("Napiš číslo (lze 0).")
        return Z_PERS_UCET
    snap = _snapshot(ctx)
    idx = ctx.user_data["_pers_idx"]
    smena = ctx.user_data["smena"]
    smena["lidi"][idx]["pers_ucet"] = v
    next_idx = idx + 1
    _push(ctx, "pers_ucet", snap)
    if next_idx < len(smena["lidi"]):
        ctx.user_data["_pers_idx"] = next_idx
        return await _ask_pers_ucet(update, ctx)
    # done → konec směny.
    # Zálohy step removed per owner request ("potom, pokud bude potřeba, vrátíme").
    # The on_zal_* handlers + Z_ZAL_* states stay wired but unreachable (dormant
    # infra). `smena["zalohy"]` stays an empty list, so the BV-writing loop at
    # save time is a no-op and nothing downstream changes.
    # Variant A (personal účet of a non-worker) is NOT asked here anymore —
    # it's an opt-in "➕ Účet mimo směnu" button on the final summary instead.
    return await _ask_hot_kon(update, ctx)


# ── Phase 3.5 — Personal účet of a non-worker (variant A) ───────
# A staff member who didn't work the shift but had an open personal účet settled
# now. Added to `lidi` with hodiny=0 → plat=0 (any sazba), tip=0 (0 hours),
# k_vyplate = −účet. Flows into weekly /vyplata as a deduction. Smeny stores max
# 3 people (jmeno_1/2/3), so the option is hidden when the shift is already full.
# Opt-in via the "➕ Účet mimo směnu" button on the summary (NOT a forced step).

_MAX_LIDI = 3
ADD_XUCET_BTN = "➕ Účet mimo směnu"


async def _ask_xucet_jmeno_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Name picker for a non-worker whose personal účet is being settled.
    Reached from the "➕ Účet mimo směnu" button on the summary."""
    await update.effective_message.reply_text(
        "Čí účet? (klikni nebo napiš)",
        reply_markup=_kb_back([["Hugo", "Lena"], ["Mia", "Adam"], ["Vlastní jméno"]]),
    )
    return Z_XUCET_JMENO


async def on_xucet_jmeno(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if _is_back_text(update):
        return await _go_back(update, ctx)
    text = update.message.text.strip()
    if text == "Vlastní jméno":
        await update.message.reply_text("Napiš jméno:")
        return Z_XUCET_JMENO
    snap = _snapshot(ctx)
    ctx.user_data["_xucet_curr"] = {"jmeno": text}
    _push(ctx, "xucet_jmeno", snap)
    return await _ask_xucet_castka(update, ctx)


async def _ask_xucet_castka(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    jmeno = ctx.user_data.get("_xucet_curr", {}).get("jmeno", "")
    await update.effective_message.reply_text(
        f"Personální účet — {jmeno}? (souhrn Kč, sleva 50% už zohledněna)",
        reply_markup=_kb_back(),
    )
    return Z_XUCET_CASTKA


async def on_xucet_castka(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if _is_back_text(update):
        return await _go_back(update, ctx)
    try:
        v = parse_kc(update.message.text)
    except ValueError:
        await update.message.reply_text("Napiš číslo v Kč.")
        return Z_XUCET_CASTKA
    if v <= 0:
        await update.message.reply_text("Napiš prosím číslo v Kč (větší než 0).")
        return Z_XUCET_CASTKA
    curr = ctx.user_data.pop("_xucet_curr", {"jmeno": "?"})
    # 0-hour person: plat=0 at any sazba, tip=0 (no hours) → k_vyplate = −účet.
    ctx.user_data["smena"]["lidi"].append({
        "jmeno": curr["jmeno"], "hodiny": 0, "pers_ucet": v, "zaloha": 0,
    })
    # Re-show the (recomputed) summary — it lists the new person and offers the
    # button again if there's still room (Smeny holds max 3).
    return await _show_summary(update, ctx)


# ── Phase 3 — Zálohy cycle ─────────────────────────────────────

async def on_zal_jmeno(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text == "Žádné zálohy":
        return await _ask_hot_kon(update, ctx)
    if text == "Ano":
        await update.message.reply_text("Jméno kdo si bere zálohu:")
        return Z_ZAL_JMENO
    ctx.user_data["_zal_curr"] = {"jmeno": text}
    await update.message.reply_text("Kolik Kč:")
    return Z_ZAL_CASTKA


async def on_zal_castka(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    castka = parse_kc(update.message.text)
    if castka <= 0:
        await update.message.reply_text("Napiš prosím číslo v Kč (větší než 0).")
        return Z_ZAL_CASTKA
    ctx.user_data["_zal_curr"]["castka"] = castka
    await update.message.reply_text("Popis zálohy (na co je):")
    return Z_ZAL_POPIS


async def on_zal_popis(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data["_zal_curr"]["popis"] = update.message.text.strip()
    kb = ReplyKeyboardMarkup([["Vrátí dnes", "Vyúčtovat ve výplatě"]], one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text("Co dál?", reply_markup=kb)
    return Z_ZAL_VRACI


async def on_zal_vraci(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    ctx.user_data["_zal_curr"]["vraci_dnes"] = "Vrátí" in text
    ctx.user_data["smena"]["zalohy"].append(ctx.user_data.pop("_zal_curr"))
    kb = ReplyKeyboardMarkup([["Přidat další", "Skončit zálohy"]], one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text("Záloha uložená ✓", reply_markup=kb)
    return Z_ZAL_MORE


async def on_zal_more(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if "Přidat" in update.message.text:
        await update.message.reply_text("Jméno kdo si bere zálohu:")
        return Z_ZAL_JMENO
    return await _ask_hot_kon(update, ctx)


async def _ask_hot_kon(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Confirmation step: fond + mince that stay in till."""
    smena = ctx.user_data["smena"]
    fond = smena.get("_fond", 5000)
    mince_stays = smena.get("_mince_stays", 0)
    msg = (
        f"Konec směny — co zůstává v kase do zítra:\n"
        f"  • Fond: {fond:,} Kč (bankovky)\n"
        f"  • Mince: {mince_stays:,} Kč\n"
        f"  ───────────\n"
        f"  Celkem zůstává: {fond + mince_stays:,} Kč\n\n"
        f"Sedí? (pak spočítáme obálku)"
    )
    # effective_message (not .message) so this works when reached from an inline
    # "↩️ Zpět" tap — a callback Update has no .message, only .callback_query.
    await update.effective_message.reply_text(
        msg, reply_markup=_kb_back([["OK pokračovat"], ["Změnit fond", "Změnit mince"]]),
    )
    return Z_HOT_2000  # reused as Z_HOT_KON_CONFIRM


async def on_hot_kon_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle fond/mince confirmation or override before counting obálka."""
    if _is_back_text(update):
        return await _go_back(update, ctx)
    text = update.message.text.strip()
    smena = ctx.user_data["smena"]
    changing = ctx.user_data.get("_kon_changing")

    if changing in ("fond", "mince"):
        # Numeric input for override at END-of-shift only.
        # _mince_stays here = mince that's IN TILL at the end (= "stays for tomorrow"),
        # NOT the start mince. hot_zac_celkem (set at start) stays UNTOUCHED.
        val = parse_kc(text)
        if val < 0:
            await update.message.reply_text("Napiš prosím nezáporné číslo v Kč.")
            return Z_HOT_2000
        if changing == "fond":
            smena["_fond"] = val
        else:
            smena["_mince_stays"] = val
        # DO NOT update hot_zac_celkem — start is set at start, end is independent.
        ctx.user_data.pop("_kon_changing", None)
        return await _ask_hot_kon(update, ctx)  # re-show confirmation

    if "pokračovat" in text.lower() or text.startswith("OK"):
        ctx.user_data["_bill_idx"] = 0
        return await _show_current_bill(update, ctx, edit=False)

    if "fond" in text.lower():
        ctx.user_data["_kon_changing"] = "fond"
        await update.message.reply_text(f"Nová hodnota fondu (Kč)? (aktuálně {smena.get('_fond', 5000)})")
        return Z_HOT_2000

    if "mince" in text.lower():
        ctx.user_data["_kon_changing"] = "mince"
        await update.message.reply_text(f"Nová hodnota mince (Kč)? (aktuálně {smena.get('_mince_stays', 0)})")
        return Z_HOT_2000

    # Unknown — re-show
    return await _ask_hot_kon(update, ctx)


# ── Phase 4 — Cash count by denomination (inline keyboard) ─────

NOMINALY = [5000, 2000, 1000, 500, 200, 100]


def _bills_keyboard(denom: int) -> InlineKeyboardMarkup:
    """Inline keyboard for adjusting count of one banknote denomination.

    The current count is shown in the message body, not as a button — non-interactive
    rows in inline keyboards confuse users (they tap and nothing happens).
    """
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("−3", callback_data=f"bill:{denom}:-3"),
            InlineKeyboardButton("−1", callback_data=f"bill:{denom}:-1"),
            InlineKeyboardButton("+1", callback_data=f"bill:{denom}:+1"),
            InlineKeyboardButton("+2", callback_data=f"bill:{denom}:+2"),
            InlineKeyboardButton("+3", callback_data=f"bill:{denom}:+3"),
        ],
        [
            InlineKeyboardButton("↩️ Zpět", callback_data=f"bill:{denom}:back"),
            InlineKeyboardButton("✅ Další", callback_data=f"bill:{denom}:next"),
        ],
    ])


async def _ask_bill_last(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Back-target from POS přebytek: re-open the obálka counter at the last
    denomination (counts are preserved in `smena`)."""
    ctx.user_data["_bill_idx"] = len(NOMINALY) - 1
    return await _show_current_bill(update, ctx, edit=False)


async def _ask_pos_prebytek(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    # Source of truth for the cash tip; bot's own calc is shown later for sanity.
    await update.effective_message.reply_text(
        "Poslední krok 🎯\n\n"
        "Jaký je POS přebytek?\n"
        "💡 Na POS uzávěrce řádek «Přebytek: Hotovost +XXX Kč»\n"
        "(Pokud nedoplatek — napiš se znaménkem −, např. -100)",
        reply_markup=_kb_back(),
    )
    return Z_HOT_200  # repurposed as Z_POS_PREBYTEK


def _obalka_total(smena: dict) -> int:
    """Running grand total of all obálka banknotes counted so far."""
    return sum(smena.get(f"hot_kon_{n}", 0) * n for n in NOMINALY)


async def _show_obalka_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE, edit: bool = False) -> int:
    """Final obálka confirmation — total + Sedí?, the money-step pattern."""
    smena = ctx.user_data["smena"]
    smena["hot_kon_mince"] = smena.get("_mince_stays", 0)  # mince auto-carries
    total = _obalka_total(smena)
    breakdown = " · ".join(
        f"{n}×{smena.get(f'hot_kon_{n}', 0)}"
        for n in NOMINALY if smena.get(f"hot_kon_{n}", 0)
    ) or "—"
    text = (
        f"📥 Obálka spočítána\n"
        f"Celkem: {total:,} Kč\n"
        f"({breakdown})\n\n"
        f"Sedí?"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Ano", callback_data="bill:0:obalka_ok"),
        InlineKeyboardButton("↩️ Změnit", callback_data="bill:0:obalka_back"),
    ]])
    if edit and update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, reply_markup=kb)
        except Exception as e:
            if "not modified" not in str(e).lower():
                raise
    else:
        await update.effective_message.reply_text(text, reply_markup=kb)
    return Z_HOT_5000


async def _show_current_bill(update: Update, ctx: ContextTypes.DEFAULT_TYPE, edit: bool = False) -> int:
    """Display the OBÁLKA bill counter for the current denomination index."""
    idx = ctx.user_data.get("_bill_idx", 0)
    smena = ctx.user_data["smena"]
    if idx >= len(NOMINALY):
        # All denominations entered → show the total + confirm (not straight to POS).
        return await _show_obalka_confirm(update, ctx, edit=edit)

    denom = NOMINALY[idx]
    count = smena.get(f"hot_kon_{denom}", 0)
    kb = _bills_keyboard(denom)
    subtotal = denom * count
    text = (
        f"📥 Obálka — bankovky {denom} Kč\n"
        f"Počet: {count}  →  {subtotal:,} Kč\n"
        f"Obálka zatím: {_obalka_total(smena):,} Kč\n\n"
        f"Klikej +/−  ·  ↩️ Zpět = krok zpět  ·  ✅ Další"
    )
    if edit and update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, reply_markup=kb)
        except Exception as e:
            # Telegram returns BadRequest "Message is not modified" when content
            # equals current (e.g., user taps −1 while count is already 0).
            # Safe to ignore — UI didn't need updating anyway.
            if "not modified" not in str(e).lower():
                raise
    else:
        await update.message.reply_text(text, reply_markup=kb)
    return Z_HOT_5000  # reuse Z_HOT_5000 as the "obálka bills" single state


async def on_bill_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle +/−/Další taps from the bill-counter inline keyboard."""
    q = update.callback_query
    await q.answer()
    if not q.data or not q.data.startswith("bill:"):
        return Z_HOT_5000
    parts = q.data.split(":")
    if len(parts) != 3:
        return Z_HOT_5000
    denom = int(parts[1])
    action = parts[2]

    if action == "obalka_ok":
        # Obálka total confirmed → hand off to POS přebytek. Push "bill_last" so
        # POS's "↩️ Zpět" returns into the counter.
        smena = ctx.user_data["smena"]
        smena["hot_kon_mince"] = smena.get("_mince_stays", 0)
        try:
            await q.edit_message_text("✓ Obálka potvrzena.")
        except Exception as e:
            if "not modified" not in str(e).lower():
                raise
        _push(ctx, "bill_last", _snapshot(ctx))
        return await _ask_pos_prebytek(update, ctx)

    if action == "obalka_back":
        # Re-open the last denomination to adjust counts.
        ctx.user_data["_bill_idx"] = len(NOMINALY) - 1
        return await _show_current_bill(update, ctx, edit=True)

    if action == "next":
        ctx.user_data["_bill_idx"] = ctx.user_data.get("_bill_idx", 0) + 1
        return await _show_current_bill(update, ctx, edit=True)

    if action == "back":
        idx = ctx.user_data.get("_bill_idx", 0)
        if idx <= 0:
            # Already at the first denomination — step back to the fond/mince
            # confirmation. Counts stay in `smena` (not reset), so re-entering
            # the counter via "OK pokračovat" preserves what's already typed.
            try:
                await q.edit_message_text("↩️ Zpět na potvrzení fondu.")
            except Exception as e:
                if "not modified" not in str(e).lower():
                    raise
            return await _ask_hot_kon(update, ctx)
        ctx.user_data["_bill_idx"] = idx - 1
        return await _show_current_bill(update, ctx, edit=True)

    # Adjustment (-5/-1/+1/+5/+10)
    try:
        delta = int(action)
    except ValueError:
        return Z_HOT_5000
    key = f"hot_kon_{denom}"
    smena = ctx.user_data["smena"]
    smena[key] = max(0, smena.get(key, 0) + delta)
    return await _show_current_bill(update, ctx, edit=True)


async def on_hot_mince(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    smena = ctx.user_data["smena"]
    if text.startswith("OK"):
        smena["hot_kon_mince"] = ctx.user_data.pop("_default_mince_end", 0)
    elif text == "Změnit":
        await update.message.reply_text("Napiš novou hodnotu mince v kase (Kč):")
        return Z_HOT_MINCE
    else:
        val = parse_kc(text)
        if val < 0:
            await update.message.reply_text("Napiš prosím nezáporné číslo v Kč.")
            return Z_HOT_MINCE
        smena["hot_kon_mince"] = val
    ctx.user_data.pop("_default_mince_end", None)
    return await _do_reconciliation(update, ctx)


async def on_pos_prebytek(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive POS přebytek directly. Use it as source of truth for cash tip,
    compare with bot's internal calculation, and show diff for sanity check."""
    if _is_back_text(update):
        return await _go_back(update, ctx)
    text = update.message.text.strip()
    smena = ctx.user_data["smena"]
    # Parse signed number (negative for nedoplatek)
    sign = -1 if text.startswith(("-", "−")) else 1
    raw = text.lstrip("-−+").strip()
    try:
        val = parse_kc(raw)
    except Exception:
        await update.message.reply_text("Napiš prosím číslo v Kč (může být i záporné, např. -100).")
        return Z_HOT_200
    # Push before reconciliation (which mutates+displays) so the photo step's
    # "↩️ Zpět" returns here to re-enter the POS přebytek.
    _push(ctx, "pos_prebytek", _snapshot(ctx))
    smena["_pos_prebytek"] = sign * val
    return await _do_reconciliation(update, ctx)


# ── Phase 4 — Reconciliation ────────────────────────────────────


async def _do_reconciliation(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    smena = ctx.user_data["smena"]
    # Aggregate sub-cycle results
    eshop_hotove = sum(e["castka"] for e in smena["eshop_items"] if e["zpusob"] == "hotove")
    eshop_kartou = sum(e["castka"] for e in smena["eshop_items"] if e["zpusob"] == "karta")
    naklady_celkem = sum(n["castka"] for n in smena["naklady"])
    zalohy_neuhrazene = sum(z["castka"] for z in smena["zalohy"] if not z["vraci_dnes"])
    # Obálka bills counted via inline keyboard
    obalka_bills = sum(smena.get(f"hot_kon_{n}", 0) * n for n in NOMINALY)
    # End-of-shift state: fond + actual end mince + obálka bills (above fond)
    fond = smena.get("_fond", 5000)
    mince_end = smena.get("hot_kon_mince", 0)
    hot_kon = fond + obalka_bills + mince_end
    bills_total = obalka_bills  # for log compat

    smena["eshop_hotove"] = eshop_hotove
    smena["eshop_kartou"] = eshop_kartou
    smena["eshop_celkem"] = eshop_hotove + eshop_kartou
    smena["naklady_celkem"] = naklady_celkem
    smena["zalohy_neuhrazene"] = zalohy_neuhrazene
    smena["hot_kon_celkem"] = hot_kon

    exp = expected_konec(smena)
    rozdil = hot_kon - exp
    tip_hotove, chyba = rozdil_a_tip_hotove(rozdil)

    # Diagnostic logging — shows full reconciliation breakdown in /tmp/kasa_bot.log
    import logging
    bills_breakdown = ", ".join(f"{n}×{smena.get(f'hot_kon_{n}',0)}" for n in NOMINALY)
    eshop_items_str = ", ".join(f"{e['castka']}/{e['zpusob']}" for e in smena["eshop_items"]) or "none"
    naklady_str = ", ".join(str(n["castka"]) for n in smena["naklady"]) or "none"
    logging.getLogger("kasa_bot").info(
        "RECON: hot_zac=%s trzba_pos=%s naklady=%s eshop_hotove=%s eshop_kartou=%s "
        "zalohy_neuhrazene=%s | bills=[%s]=%s mince=%s | hot_kon=%s | expected=%s rozdil=%s | "
        "eshop_items=[%s] naklady=[%s]",
        smena.get("hot_zac_celkem"), smena.get("trzba_pos_hot"), naklady_celkem,
        eshop_hotove, eshop_kartou, zalohy_neuhrazene,
        bills_breakdown, bills_total, smena.get("hot_kon_mince", 0),
        hot_kon, exp, rozdil,
        eshop_items_str, naklady_str,
    )

    smena["expected_konec"] = exp
    smena["rozdil"] = rozdil
    # POS přebytek is the source of truth for cash tip (if provided)
    pos_prebytek = smena.get("_pos_prebytek")
    if pos_prebytek is not None:
        # Use POS as truth
        smena["spropitne_hotov"] = max(0, pos_prebytek)
        pos_chyba = abs(pos_prebytek) if pos_prebytek < 0 else 0
        bot_vs_pos_diff = rozdil - pos_prebytek  # how much bot over-counts
    else:
        # Fallback to bot's internal calc
        smena["spropitne_hotov"] = tip_hotove
        pos_chyba = chyba
        bot_vs_pos_diff = 0

    # Cash discrepancy bot-vs-POS with a POSITIVE POS (tip) — the till didn't add
    # up by `bot_vs_pos_diff` even though POS shows a surplus (e.g. a banknote
    # missing from the obálka → −100). Tracked like overnight: shown in the
    # summary, manager-notified + logged to Chyby at save. ≥10 Kč (below =
    # rounding). The negative-POS case is already a nedoplatek chyba, skip it.
    if pos_prebytek is not None and pos_prebytek >= 0 and abs(bot_vs_pos_diff) >= 10:
        smena["pokladna_diff"] = bot_vs_pos_diff

    reply_to = update.effective_message

    # If POS says nedoplatek (negative), treat as chyba regardless of bot's calc
    if pos_prebytek is not None and pos_prebytek < 0:
        await reply_to.reply_text(
            f"─── Pokladna ────────────\n"
            f"POS přebytek:    −{abs(pos_prebytek):,} Kč  ← Nedoplatek\n\n"
            f"Bot spočítal:     +{tip_hotove:,} Kč\n"
            f"Diff:             {bot_vs_pos_diff:+,} Kč\n\n"
            f"❌ POS hlásí nedoplatek.",
            reply_markup=ReplyKeyboardMarkup(
                [["Záznam chyby", "Spočítat znovu"]], one_time_keyboard=True, resize_keyboard=True,
            ),
        )
        ctx.user_data["_chyba_castka"] = abs(pos_prebytek)
        return Z_RECON_CHOICE

    if pos_prebytek is None and chyba > 0:
        # Legacy path: no POS provided, bot's internal chyba
        await reply_to.reply_text(
            f"─── Pokladna ────────────\n"
            f"Skutečně:    {hot_kon:,} Kč\n"
            f"Očekáváno:   {exp:,} Kč\n"
            f"Rozdíl:        −{chyba} Kč  ← Nedoplatek\n\n"
            f"❌ Pokladna nesedí.",
            reply_markup=ReplyKeyboardMarkup(
                [["Záznam chyby", "Spočítat znovu"]], one_time_keyboard=True, resize_keyboard=True,
            ),
        )
        ctx.user_data["_chyba_castka"] = chyba
        return Z_RECON_CHOICE

    # OK path — POS přebytek is positive (or no POS, but bot's calc is positive)
    if pos_prebytek is not None:
        # POS-driven message with comparison
        diff_hint = ""
        if abs(bot_vs_pos_diff) >= 10:
            if bot_vs_pos_diff > 0:
                diff_hint = (
                    f"\n⚠️ Bot napočítal o {bot_vs_pos_diff:,} Kč VÍC než POS.\n"
                    f"Pravděpodobně fond v kase je menší než 5000 Kč\n"
                    f"(někdo vzal {bot_vs_pos_diff} Kč na rozměnu během směny).\n"
                    f"Pro audit: spočítej co skutečně leží v kase teď."
                )
            else:
                diff_hint = (
                    f"\n⚠️ Bot napočítal o {abs(bot_vs_pos_diff):,} Kč MÉNĚ než POS.\n"
                    f"Možná chybí bankovka v obálce, nebo se přidalo mince."
                )
        karta_tip = int(smena.get("spropitne_karta", 0))
        karta_rev = int(smena.get("karta", 0)) - karta_tip  # net card revenue
        tip_celkem = pos_prebytek + karta_tip
        await reply_to.reply_text(
            f"─── Pokladna ────────────\n"
            f"Tržba POS:       {smena.get('trzba_pos_hot', 0):,} Kč\n"
            f"Tržba karta:     {karta_rev:,} Kč\n"
            f"Obálka spočítaná: {bills_total:,} Kč\n"
            f"Bot diff:        {bot_vs_pos_diff:+,} Kč"
            f"{diff_hint}\n\n"
            f"🎯 Spropitné:\n"
            f"  💵 hotově: {pos_prebytek:,} Kč\n"
            f"  💳 karta:  {karta_tip:,} Kč\n"
            f"  ─────\n"
            f"  celkem:   {tip_celkem:,} Kč\n\n"
            f"✅ Použijeme POS hodnotu jako spropitné."
        )
    else:
        # Legacy path — no POS
        await reply_to.reply_text(
            f"─── Pokladna ────────────\n"
            f"Skutečně:    {hot_kon:,} Kč\n"
            f"Očekáváno:   {exp:,} Kč\n"
            f"Rozdíl:        +{tip_hotove} Kč  ← Spropitné hotově\n"
            f"✅ Sedí."
        )
    return await _ask_terminal_photo(update, ctx)


# ── Phase 4.5 — Mandatory receipt photos (audit) ───────────────
# Bartender must send 2 photos before save: terminal acquiring + Dotykačka POS.
# Both stored in Drive shift folder. Future: Claude vision will auto-validate
# entered numbers against these images.

async def _ask_terminal_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text(
        "📸 Pošli prosím foto uzávěrky terminálu (karta).",
        reply_markup=_inline_back(),
    )
    return Z_HOT_1000  # repurposed as Z_FOTO_TERMINAL


async def on_foto_terminal(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    photo = update.message.photo[-1] if update.message.photo else None
    document = update.message.document
    if not (photo or document):
        await update.message.reply_text(
            "Pošli prosím foto nebo PDF přílohy.", reply_markup=_inline_back(),
        )
        return Z_HOT_1000
    snap = _snapshot(ctx)
    smena = ctx.user_data["smena"]
    if photo:
        smena["_foto_terminal_id"] = photo.file_id
        smena["_foto_terminal_kind"] = "photo"
    else:
        smena["_foto_terminal_id"] = document.file_id
        smena["_foto_terminal_kind"] = "doc"
        smena["_foto_terminal_name"] = document.file_name or "terminal.pdf"
    await update.message.reply_text("✓ Terminál uložen.")
    _push(ctx, "foto_terminal", snap)
    return await _ask_foto_pos(update, ctx)


async def _ask_foto_pos(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text(
        "📸 A teď foto uzávěrky z Dotykačky (POS).",
        reply_markup=_inline_back(),
    )
    return Z_HOT_500  # repurposed as Z_FOTO_POS


async def on_foto_pos(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    photo = update.message.photo[-1] if update.message.photo else None
    document = update.message.document
    if not (photo or document):
        await update.message.reply_text(
            "Pošli prosím foto nebo PDF přílohy.", reply_markup=_inline_back(),
        )
        return Z_HOT_500
    smena = ctx.user_data["smena"]
    if photo:
        smena["_foto_pos_id"] = photo.file_id
        smena["_foto_pos_kind"] = "photo"
    else:
        smena["_foto_pos_id"] = document.file_id
        smena["_foto_pos_kind"] = "doc"
        smena["_foto_pos_name"] = document.file_name or "pos.pdf"
    await update.message.reply_text("✓ POS uložen.")
    return await _show_summary(update, ctx)


async def on_recon_choice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.lower()
    if "znovu" in text or "pokra" in text:
        # User confirmed re-count → reset and restart inline keyboard
        smena = ctx.user_data["smena"]
        for n in NOMINALY:
            smena[f"hot_kon_{n}"] = 0
        smena["hot_kon_mince"] = 0
        await update.message.reply_text("Dobře, počítáme znovu od nuly.")
        return await _ask_hot_kon(update, ctx)
    if "zruš" in text or "ne" == text:
        # User cancelled re-count — back to the chyba decision
        kb = ReplyKeyboardMarkup(
            [["Záznam chyby", "Spočítat znovu"]], one_time_keyboard=True, resize_keyboard=True,
        )
        await update.message.reply_text("OK, vrácení k volbě:", reply_markup=kb)
        return Z_RECON_CHOICE
    # Záznam chyby path
    await update.message.reply_text("Popiš stručně, co se stalo:")
    return Z_CHYBA_POPIS


async def on_chyba_popis(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data["smena"]["chyba_popis"] = update.message.text.strip()
    ctx.user_data["smena"]["status"] = "chyba"
    return await _ask_terminal_photo(update, ctx)


async def _show_summary(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    smena = ctx.user_data["smena"]
    smena["tip_per_hour"] = tip_per_hour(smena)
    rows = k_vyplate_per_person(smena)
    smena["lidi"] = rows  # overwrite with computed plat/tip/k_vyplate
    smena["total_hodiny"] = sum(r["hodiny"] for r in rows)
    smena["spropitne_celkem"] = smena["spropitne_karta"] + smena["spropitne_hotov"]
    smena["karta_pos"] = smena["karta"] - smena["spropitne_karta"]
    smena["trzba_bar"] = smena["karta_pos"] + smena["trzba_pos_hot"] - smena["eshop_celkem"]
    if "status" not in smena:
        smena["status"] = "ok"

    # Cash that STAYS in the till for tomorrow (= next shift's proposed opening):
    # fond (banknote float) + mince. The obálka (day's takings) is taken out.
    _fond_stays = int(smena.get("_fond", DEFAULT_HOT_ZAC))
    _mince_stays = int(smena.get("_mince_stays", smena.get("hot_kon_mince", 0)))

    # MC/Visa split is an INPUT convenience only (two questions so the bartender
    # doesn't sum on a calculator) — NOT shown here. Display tips as
    # hotově / karta / celkem.

    # Display order matches owner's mental model: tržby first (3 channels →
    # bar total), then náklady/e-shop/spropitné, then payroll. Hot_kon_celkem
    # is INTERNAL (fond + sales), the bartender already sees it during the
    # cash count — no need to repeat here. Show trzba_pos_hot (net cash sales)
    # which is what's analogous to "karta" (net card sales).
    lines = [
        f"📋 Souhrn směny {smena['datum']}:",
        f"  💳 Tržba karta:  {smena['karta_pos']:,} Kč  (+ tip {smena['spropitne_karta']})",
        f"  💵 Tržba hotově: {smena['trzba_pos_hot']:,} Kč",
        f"  🛒 E-shop:       {smena['eshop_celkem']:,} Kč",
        f"  ─────",
        f"  📊 Tržba bar:    {smena['trzba_bar']:,} Kč",
        "",
        f"  💸 Náklady:    {smena['naklady_celkem']:,} Kč",
        f"  🎯 Spropitné:  {smena['spropitne_celkem']:,} Kč ({smena['tip_per_hour']}/h)",
        f"       💵 hotově {smena['spropitne_hotov']:,} · 💳 karta {smena['spropitne_karta']:,}",
        "",
        f"  🏦 V kase zůstává (do zítra): {_fond_stays + _mince_stays:,} Kč",
        f"       bankovky {_fond_stays:,} · mince {_mince_stays:,}",
        "",
    ]
    # Cash discrepancy (bot count vs POS) — the "missing" money. Manager-notified.
    _pdiff = int(smena.get("pokladna_diff", 0))
    if _pdiff:
        _kind = "chybí v obálce" if _pdiff < 0 else "přebývá v kase"
        lines.append(f"  ⚠️ Rozdíl pokladny: {_pdiff:+,} Kč ({_kind}) → manažer")
        lines.append("")
    lines.append("Lidé:")
    for r in rows:
        _u = f" · účet {r['pers_ucet']:,}" if r.get("pers_ucet") else ""
        lines.append(f"  {r['jmeno']:8} {r['hodiny']:g}h → {r['k_vyplate']:,} Kč{_u}")
    # Day totals (Σ za den) — sums of the per-person payout columns; self-checks
    # because k_vyplate = plat + tip − účet − záloha.
    lines += [
        "  ─────",
        "  Σ za den:",
        f"     ⏱ Hodiny:    {sum(r['hodiny'] for r in rows):g} h",
        f"     💼 Mzda:      {sum(r['plat'] for r in rows):,} Kč",
        f"     🎯 Spropitné: {sum(r['spropitne'] for r in rows):,} Kč",
        f"     🧾 Účty:      {sum(r['pers_ucet'] for r in rows):,} Kč",
        f"     💰 K výplatě: {sum(r['k_vyplate'] for r in rows):,} Kč",
    ]

    # Opt-in: add a personal účet of someone who did NOT work the shift (variant A).
    # Only offered while the Smeny row still has a free slot (max 3 people).
    kb_rows = [["Uložit ✅", "Zrušit"]]
    if len(rows) < _MAX_LIDI:
        kb_rows = [[ADD_XUCET_BTN]] + kb_rows
    kb = ReplyKeyboardMarkup(kb_rows, one_time_keyboard=True, resize_keyboard=True)
    await update.effective_message.reply_text("\n".join(lines), reply_markup=kb)
    return Z_CONFIRM


# ── Phase 5 — Atomic save (Drive + P&L + Smeny + Chyby + notify) ──

async def on_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    if _is_back_text(update):
        return await _go_back(update, ctx)
    if ADD_XUCET_BTN in text:
        # Opt-in non-worker účet → name picker, then back to the summary.
        _push(ctx, "summary", _snapshot(ctx))
        return await _ask_xucet_jmeno_pick(update, ctx)
    if "Zrušit" in text:
        await update.message.reply_text("Zrušeno. Nic neuloženo.")
        return ConversationHandler.END

    smena = ctx.user_data["smena"]
    sheets = ctx.application.bot_data["sheets"]
    drive = ctx.application.bot_data["drive"]
    cfg = ctx.application.bot_data["config"]

    smena["zodpovedny"] = ctx.user_data.get("_curr_jmeno") or smena["lidi"][0]["jmeno"]
    smena["created_at_tg"] = datetime.now().strftime("%d.%m.%Y %H:%M")

    # 1. Create Drive folder for this shift.
    # All bot artifacts live under DRIVE_UMBRELLA_FOLDER_ID (umbrella in user's Drive).
    # Falls back to root if env not set (backwards compat).
    umbrella_id = cfg.drive_umbrella_id  # may be None
    root_id = drive.ensure_folder(DRIVE_ROOT_UZAVERKY, parent_id=umbrella_id)
    dt = datetime.strptime(smena["datum"], "%d.%m.%Y")
    month_folder = drive.ensure_folder(f"{dt.month}.{dt.year}", parent_id=root_id)
    day_folder = drive.ensure_folder(f"{dt.day}.{dt.month}", parent_id=month_folder)
    smena["drive_folder_url"] = drive.get_folder_webview(day_folder)

    # 2. Upload doklad photos (those with file_id from náklady)
    for nak in smena["naklady"]:
        if not nak.get("ma_doklad") or not nak.get("doklad_file_id"):
            continue
        tg_file = await ctx.bot.get_file(nak["doklad_file_id"])
        if nak.get("doklad_kind") == "photo":
            suffix = ".jpg"
        else:
            suffix = os.path.splitext(nak.get("doklad_name", ".pdf"))[1] or ".pdf"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp_path = tmp.name
        try:
            await tg_file.download_to_drive(tmp_path)
            short_popis = "_".join(nak["popis"].split()[:2])[:30]
            target_name = f"doklad_{nak['castka']}_{short_popis}{suffix}"
            url = drive.upload_doklad(tmp_path, day_folder, target_name=target_name)
            nak["doklad_url"] = url
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # 2b. Upload terminal + POS uzávěrka photos (mandatory audit captures)
    for tag in ("terminal", "pos"):
        fid = smena.get(f"_foto_{tag}_id")
        if not fid:
            continue
        tg_file = await ctx.bot.get_file(fid)
        kind = smena.get(f"_foto_{tag}_kind", "photo")
        if kind == "photo":
            suffix = ".jpg"
        else:
            suffix = os.path.splitext(smena.get(f"_foto_{tag}_name", ".pdf"))[1] or ".pdf"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp_path = tmp.name
        try:
            await tg_file.download_to_drive(tmp_path)
            target_name = f"uzaverka_{tag}_{smena['datum']}{suffix}"
            url = drive.upload_doklad(tmp_path, day_folder, target_name=target_name)
            smena[f"_foto_{tag}_url"] = url
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # 3. Classify each náklad via Claude, write to P&L
    for nak in smena["naklady"]:
        kat = classify_naklad(nak["popis"], api_key=cfg.anthropic_api_key)
        sheets.append_naklad_to_pnl(
            datum=smena["datum"], castka=nak["castka"], popis=nak["popis"],
            kategorie=kat, zaplaceno_zdroj="Hotovost Demo",
            kdo_zapsal=smena["zodpovedny"], doklad_url=nak.get("doklad_url", ""),
            smena_id=smena["smena_id"],
        )

    # 3a. E-shop sales — write to P&L as income (PESH category).
    # E-shop is separate income stream (tabák, uhlí prodej). Source depends on payment:
    #   hotove → "Hotovost Demo" (cash came into till)
    #   karta  → "BU Demo Bistro" (came to bank via POS terminal)
    for es in smena.get("eshop_items", []):
        if es.get("castka", 0) <= 0:
            continue
        zdroj_es = "Hotovost Demo" if es.get("zpusob") == "hotove" else "BU Demo Bistro"
        sheets.append_naklad_to_pnl(
            datum=smena["datum"],
            castka=es["castka"],
            popis=f"E-shop: {es.get('popis', '')}".strip(': ') or "E-shop",
            kategorie="PESH",
            zaplaceno_zdroj=zdroj_es,
            kdo_zapsal=smena["zodpovedny"],
            smena_id=smena["smena_id"],
            is_income=True,
        )

    # 3b. Zálohy — write to P&L as BV (partial salary already paid).
    # Will be deducted from weekly /vyplata via aggregate_week.total_zalohy.
    # Only "vyúčtovat ve výplatě" záloh affect cash (returned-today záloh have vraci_dnes=True and no cash impact).
    for zal in smena.get("zalohy", []):
        if zal.get("vraci_dnes"):
            continue  # returned same day — no P&L entry needed
        popis_parts = [f"Záloha {zal['jmeno']}"]
        if zal.get("popis"):
            popis_parts.append(zal["popis"])
        sheets.append_naklad_to_pnl(
            datum=smena["datum"],
            castka=zal["castka"],
            popis=": ".join(popis_parts),
            kategorie="BV",
            zaplaceno_zdroj="Hotovost Demo",
            kdo_zapsal=smena["zodpovedny"],
            smena_id=smena["smena_id"],
        )

    # 4. Append Smeny row
    sheets.append_smena(smena)

    # 5. Notify on chyba
    if smena.get("status") == "chyba":
        chyba_castka = abs(smena["rozdil"])
        await push_chyba_alert(ctx.bot, cfg.manager_tg_id, smena, chyba_castka)
        sheets.append_chyba(
            smena_id=smena["smena_id"], datum=smena["datum"],
            typ="nedoplatek", castka=chyba_castka,
            popis=smena.get("chyba_popis", ""),
        )

    # 5b. Notify on overnight discrepancy (start cash ≠ previous shift's end).
    overnight = smena.get("overnight_rozdil")
    if overnight:
        await push_overnight_alert(ctx.bot, cfg.manager_tg_id, smena, overnight)
        sheets.append_chyba(
            smena_id=smena["smena_id"], datum=smena["datum"],
            typ="overnight", castka=abs(overnight),
            popis=(f"Start {smena.get('overnight_entered')} vs minulý konec "
                   f"{smena.get('overnight_proposed')} Kč"),
        )

    # 5c. Notify on pokladna discrepancy (bot count vs POS, positive-POS case).
    pdiff = smena.get("pokladna_diff")
    if pdiff:
        await push_pokladna_diff_alert(ctx.bot, cfg.manager_tg_id, smena, pdiff)
        sheets.append_chyba(
            smena_id=smena["smena_id"], datum=smena["datum"],
            typ="rozdil_pokladny", castka=abs(pdiff),
            popis=f"Bot vs POS {pdiff:+} Kč (obálka {smena.get('hot_kon_celkem','?')} Kč)",
        )

    await update.message.reply_text("✅ Uloženo.")
    return ConversationHandler.END


# ── Back-navigation registry ────────────────────────────────────
# Maps a resume-point key (pushed by handlers) → the ask function that
# re-renders that step. _go_back() pops a key and dispatches here. Defined at
# module bottom so every _ask_* it references is already bound.
_ASK = {
    "typ": _ask_typ,
    "pocet": _ask_pocet,
    "jmeno": _ask_jmeno,
    "hodiny": _ask_hodiny,
    "karta": _ask_karta,
    "tip_mc": _ask_tip_mc,
    "tip_visa": _ask_tip_visa,
    "tip_confirm": _ask_tip_confirm,
    "hot_zac": _ask_hot_zac,
    "trzba_pos": _ask_trzba_pos,
    "eshop_decision": _ask_eshop,
    "eshop_castka": _ask_eshop_castka,
    "eshop_popis": _ask_eshop_popis,
    "eshop_zpusob": _ask_eshop_zpusob,
    "eshop_more": _ask_eshop_more,
    "naklad_decision": _ask_naklady,
    "naklad_castka": _ask_naklad_castka,
    "naklad_popis": _ask_naklad_popis,
    "naklad_doklad": _ask_naklad_doklad,
    "naklad_foto": _ask_naklad_foto,
    "naklad_more": _ask_naklad_more,
    "pers_ucet": _ask_pers_ucet,
    "summary": _show_summary,
    "xucet_jmeno": _ask_xucet_jmeno_pick,
    "hot_kon": _ask_hot_kon,
    "bill_last": _ask_bill_last,
    "pos_prebytek": _ask_pos_prebytek,
    "foto_terminal": _ask_terminal_photo,
    "foto_pos": _ask_foto_pos,
}
