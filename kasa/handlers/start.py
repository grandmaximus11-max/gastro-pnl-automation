"""/start handler and activation logic. Pure functions plus Telegram glue."""
from __future__ import annotations

from datetime import datetime, timedelta

from telegram import ReplyKeyboardMarkup

from kasa import auth

# ── Main menu (FakturaBot-style persistent reply keyboard) ──────
# Button labels double as ConversationHandler entry triggers (wired in
# kasa_bot.py via filters.Regex), so the bot reacts to a tap exactly like the
# matching slash command.
MENU_UZAVERKA = "📋 Uzávěrka"
MENU_VYPLATA = "💰 Výplata"
MENU_VYPLATA_HPP = "💵 Výplata HPP"
MENU_PREHLED = "📊 Přehled"
MENU_NAPOVEDA = "ℹ️ Nápověda"

NAPOVEDA_TEXT = (
    "ℹ️ Demo Pokladna\n\n"
    "📋 Uzávěrka — zavřít směnu (tržby, obálka, spropitné, účty).\n"
    "💰 Výplata — týdenní výplata DPP (jen manažer/majitel).\n"
    "💵 Výplata HPP — měsíční výplata HPP (jen manažer/majitel).\n"
    "📊 Přehled — směny po dnech + problémové dny (manažer). "
    "Detail dne: /den DD.MM.YYYY.\n\n"
    "Tlačítka v menu spouští tyto akce. Příkazy fungují taky: "
    "/uzaverka /vyplata /vyplata_hpp.\n"
    "Zrušit rozdělaný proces: /zrusit."
)


def main_menu_keyboard(emp: dict | None) -> ReplyKeyboardMarkup:
    """Role-aware main menu shown on /start. Bartender sees only Uzávěrka;
    manager and majitel also see the payout buttons. Always shows Nápověda.

    NOT is_persistent: a persistent keyboard fights with the wizards'
    one_time_keyboard prompts (Telegram client fails to render the step buttons
    and shows a "long load"). A normal reply keyboard is cleanly replaced by the
    wizard keyboards; user re-summons the menu with /start."""
    rows: list[list[str]] = [[MENU_UZAVERKA]]
    if emp and auth.has_role(emp, "manager"):
        rows.append([MENU_VYPLATA, MENU_VYPLATA_HPP])
        rows.append([MENU_PREHLED])
    rows.append([MENU_NAPOVEDA])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def handle_start_message(sheets, tg_user_id: int) -> str:
    """Reply text when user runs /start."""
    rows = sheets.get_zamestnanci()
    emp = auth.find_by_tg_user_id(rows, tg_user_id)
    if emp and auth.is_active(emp):
        return f"Ahoj, {emp['jmeno']}! Pro uzávěrku napiš /uzaverka."
    return ("Vítej v Demo Pokladně.\n"
            "Pro aktivaci zadej svůj aktivační kód (vypadá jako DEMO-JMENO-1234):")


def handle_activation_code(sheets, tg_user_id: int, tg_username: str, code: str) -> dict:
    """Process activation code submission. Returns dict with status."""
    rows = sheets.get_zamestnanci()
    emp = auth.validate_activation_code(rows, code)

    if emp is None:
        attempts = sheets.failed_attempts_get(tg_user_id) + 1
        sheets.failed_attempts_incr(tg_user_id)
        if attempts >= 3:
            until = (datetime.now() + timedelta(hours=1)).strftime("%d.%m.%Y %H:%M")
            sheets.block_user(tg_user_id, until)
            return {"status": "blocked", "until": until}
        return {"status": "wrong", "attempts": attempts}

    sheets.activate_employee(tg_user_id, tg_username, code)
    return {"status": "activated", "jmeno": emp["jmeno"]}
