"""kasa_bot — Telegram bot for shift close-out (uzávěrka) at Demo bar."""
from __future__ import annotations

import logging
import re
from telegram import Update, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, ConversationHandler, filters,
    PicklePersistence, PersistenceInput, CallbackQueryHandler,
)

from kasa import auth
from kasa.config import Config
from kasa.sheets import SheetsClient
from kasa.handlers.start import (
    handle_start_message, handle_activation_code, main_menu_keyboard,
    MENU_UZAVERKA, MENU_VYPLATA, MENU_VYPLATA_HPP, MENU_PREHLED,
    MENU_NAPOVEDA, NAPOVEDA_TEXT,
)
from kasa.handlers.prehled import cmd_prehled, cmd_den, on_prehled_cb, on_den_reply


def _exact(label: str):
    """filters.Regex matching a menu-button label exactly (emoji-safe)."""
    return filters.Regex(f"^{re.escape(label)}$")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kasa_bot")


async def post_init(app: Application) -> None:
    """Register the slash-command menu (visible via Telegram Menu button & / autocomplete)."""
    await app.bot.set_my_commands([
        BotCommand("uzaverka", "Zavřít směnu — uzávěrka pokladny"),
        BotCommand("vyplata", "Týdenní výplata DPP (manager+)"),
        BotCommand("vyplata_hpp", "Měsíční výplata HPP (manager+)"),
        BotCommand("prehled", "Přehled směn po dnech (manager+)"),
        BotCommand("den", "Detail dne: /den DD.MM.YYYY (manager+)"),
        BotCommand("start", "Aktivace / přihlášení / menu"),
        BotCommand("napoveda", "Nápověda"),
        BotCommand("zrusit", "Zrušit aktuální proces"),
    ])
    log.info("Bot commands menu registered.")


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Global error handler — logs full traceback for ANY uncaught exception."""
    import traceback
    log.error("UNCAUGHT EXCEPTION in handler:\n%s",
              "".join(traceback.format_exception(type(ctx.error), ctx.error, ctx.error.__traceback__)))
    # Try to notify the user (best-effort)
    if isinstance(update, Update) and update.effective_chat:
        try:
            await ctx.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"⚠️ Vnitřní chyba: {type(ctx.error).__name__}. Použij /zrusit pro restart.",
            )
        except Exception:
            pass


# ConversationHandler states
WAIT_CODE = 1


async def cmd_zrusit(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Generic /zrusit handler — clears wizard state and exits any conversation."""
    ctx.user_data.clear()
    await update.message.reply_text("Zrušeno. Pro novou uzávěrku napiš /uzaverka")
    return ConversationHandler.END


async def cmd_napoveda(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """ℹ️ Nápověda menu button / fallback help."""
    await update.message.reply_text(NAPOVEDA_TEXT)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    sheets: SheetsClient = ctx.application.bot_data["sheets"]
    uid = update.effective_user.id
    reply = handle_start_message(sheets, uid)
    if "aktivační kód" in reply:
        await update.message.reply_text(reply)
        return WAIT_CODE
    # Activated → show the role-aware main menu (persistent keyboard).
    emp = auth.find_by_tg_user_id(sheets.get_zamestnanci(), uid)
    await update.message.reply_text(reply, reply_markup=main_menu_keyboard(emp))
    return ConversationHandler.END


async def on_code(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    sheets: SheetsClient = ctx.application.bot_data["sheets"]
    code = update.message.text.strip()
    result = handle_activation_code(
        sheets, update.effective_user.id,
        update.effective_user.username or "",
        code,
    )
    if result["status"] == "activated":
        emp = auth.find_by_tg_user_id(
            sheets.get_zamestnanci(), update.effective_user.id
        )
        await update.message.reply_text(
            f"Aktivováno. Ahoj, {result['jmeno']}!",
            reply_markup=main_menu_keyboard(emp),
        )
        return ConversationHandler.END
    if result["status"] == "blocked":
        await update.message.reply_text(
            f"Příliš mnoho pokusů. Zablokován do {result['until']}."
        )
        return ConversationHandler.END
    await update.message.reply_text(
        f"Kód není platný. Pokus {result['attempts']}/3. Zkus znovu:"
    )
    return WAIT_CODE


def main() -> None:
    cfg = Config.from_env()
    sheets = SheetsClient(cfg.shared_auth_path, cfg.sheets_id)
    from kasa.drive import DriveClient
    drive = DriveClient(cfg.shared_auth_path)

    # Persistence saves conversation state + user_data + chat_data, but NOT bot_data —
    # bot_data holds non-pickleable runtime deps (SheetsClient, Config with OAuth creds).
    # Manual assignment to bot_data below would otherwise be overwritten by persistence load.
    persistence = PicklePersistence(
        filepath="kasa_state.pkl",
        store_data=PersistenceInput(bot_data=False, user_data=True, chat_data=True, callback_data=False),
    )
    app = (
        Application.builder()
        .token(cfg.bot_token)
        .persistence(persistence)
        .post_init(post_init)
        .build()
    )
    app.bot_data["sheets"] = sheets
    app.bot_data["config"] = cfg
    app.bot_data["drive"] = drive

    activation = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={WAIT_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_code)]},
        fallbacks=[CommandHandler("zrusit", cmd_zrusit)],
        name="activation",
        persistent=True,
        # If user re-types /start while waiting for code, restart cleanly
        # instead of leaving them stuck in WAIT_CODE forever.
        allow_reentry=True,
    )
    app.add_handler(activation)

    from kasa.handlers.uzaverka import (
        cmd_uzaverka, on_typ, on_pocet, on_jmeno, on_hodiny,
        on_karta, on_tip_mc, on_tip_visa, on_tip_confirm, on_hot_zac, on_trzba_pos,
        on_eshop_castka, on_eshop_popis, on_eshop_zpusob, on_eshop_more,
        on_naklad_castka, on_naklad_popis, on_naklad_doklad, on_naklad_foto, on_naklad_more,
        on_pers_ucet,
        on_xucet_jmeno, on_xucet_castka,
        on_zal_jmeno, on_zal_castka, on_zal_popis, on_zal_vraci, on_zal_more,
        on_bill_callback, on_hot_mince, on_hot_kon_confirm,
        on_pos_prebytek,
        on_recon_choice, on_chyba_popis,
        on_foto_terminal, on_foto_pos,
        on_confirm, _go_back,
        Z_TYP, Z_POCET, Z_JMENO, Z_HODINY,
        Z_KARTA, Z_TIP_KARTA, Z_TIP_VISA, Z_TIP_CONFIRM, Z_HOT_ZAC, Z_TRZBA_POS,
        Z_ESHOP_CASTKA, Z_ESHOP_POPIS, Z_ESHOP_ZPUSOB, Z_ESHOP_MORE,
        Z_NAKLAD_CASTKA, Z_NAKLAD_POPIS, Z_NAKLAD_DOKLAD, Z_NAKLAD_FOTO, Z_NAKLAD_MORE,
        Z_PERS_UCET,
        Z_XUCET_JMENO, Z_XUCET_CASTKA,
        Z_ZAL_JMENO, Z_ZAL_CASTKA, Z_ZAL_POPIS, Z_ZAL_VRACI, Z_ZAL_MORE,
        Z_HOT_5000, Z_HOT_2000, Z_HOT_1000, Z_HOT_500, Z_HOT_200, Z_HOT_100, Z_HOT_MINCE,
        Z_RECON_CHOICE, Z_CHYBA_POPIS,
        Z_CONFIRM,
    )
    uzaverka = ConversationHandler(
        entry_points=[
            CommandHandler("uzaverka", cmd_uzaverka),
            MessageHandler(_exact(MENU_UZAVERKA), cmd_uzaverka),
        ],
        states={
            Z_TYP: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_typ)],
            Z_POCET: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_pocet)],
            Z_JMENO: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_jmeno)],
            Z_HODINY: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_hodiny)],
            Z_KARTA: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_karta)],
            Z_TIP_KARTA: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_tip_mc)],
            Z_TIP_VISA: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_tip_visa)],
            Z_TIP_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_tip_confirm)],
            Z_HOT_ZAC: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_hot_zac)],
            Z_TRZBA_POS: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_trzba_pos)],
            Z_ESHOP_CASTKA: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_eshop_castka)],
            Z_ESHOP_POPIS: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_eshop_popis)],
            Z_ESHOP_ZPUSOB: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_eshop_zpusob)],
            Z_ESHOP_MORE: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_eshop_more)],
            Z_NAKLAD_CASTKA: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_naklad_castka)],
            Z_NAKLAD_POPIS: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_naklad_popis)],
            Z_NAKLAD_DOKLAD: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_naklad_doklad)],
            Z_NAKLAD_FOTO: [
                CallbackQueryHandler(_go_back, pattern=r"^nav:back$"),
                MessageHandler((filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, on_naklad_foto),
            ],
            Z_NAKLAD_MORE: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_naklad_more)],
            Z_PERS_UCET: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_pers_ucet)],
            Z_XUCET_JMENO: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_xucet_jmeno)],
            Z_XUCET_CASTKA: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_xucet_castka)],
            Z_ZAL_JMENO: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_zal_jmeno)],
            Z_ZAL_CASTKA: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_zal_castka)],
            Z_ZAL_POPIS: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_zal_popis)],
            Z_ZAL_VRACI: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_zal_vraci)],
            Z_ZAL_MORE: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_zal_more)],
            Z_HOT_5000: [CallbackQueryHandler(on_bill_callback, pattern=r"^bill:")],
            Z_HOT_2000: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_hot_kon_confirm)],
            # Repurposed states:
            #   Z_HOT_1000 = Z_FOTO_TERMINAL (mandatory terminal receipt photo)
            #   Z_HOT_500  = Z_FOTO_POS (mandatory POS Dotykačka uzávěrka photo)
            # Photo steps: photo/doc OR an inline "↩️ Zpět" (nav:back) callback.
            Z_HOT_1000: [
                CallbackQueryHandler(_go_back, pattern=r"^nav:back$"),
                MessageHandler((filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, on_foto_terminal),
            ],
            Z_HOT_500: [
                CallbackQueryHandler(_go_back, pattern=r"^nav:back$"),
                MessageHandler((filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, on_foto_pos),
            ],
            Z_HOT_200: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_pos_prebytek)],  # = Z_POS_PREBYTEK
            Z_HOT_MINCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_hot_mince)],
            Z_RECON_CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_recon_choice)],
            Z_CHYBA_POPIS: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_chyba_popis)],
            Z_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_confirm)],
        },
        fallbacks=[CommandHandler("zrusit", cmd_zrusit)],
        name="uzaverka",
        persistent=True,
        # Re-tapping /uzaverka starts a fresh wizard. Trade-off: if you're
        # mid-wizard and accidentally tap /uzaverka, you lose progress — but
        # that's MUCH better than getting stuck because the bot was restarted
        # mid-shift. The persisted state would otherwise corrupt tonight's data
        # with yesterday's half-filled inputs.
        allow_reentry=True,
    )
    app.add_handler(uzaverka)

    from kasa.handlers.vyplata import (
        cmd_vyplata, on_v_period, on_v_method_cb, on_v_method_text,
        V_PERIOD, V_METHODS,
    )
    vyplata = ConversationHandler(
        entry_points=[
            CommandHandler("vyplata", cmd_vyplata),
            MessageHandler(_exact(MENU_VYPLATA), cmd_vyplata),
        ],
        states={
            V_PERIOD: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_v_period)],
            V_METHODS: [
                CallbackQueryHandler(on_v_method_cb, pattern=r"^vm:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_v_method_text),
            ],
        },
        fallbacks=[CommandHandler("zrusit", cmd_zrusit)],
        name="vyplata",
        persistent=True,
        allow_reentry=True,
    )
    app.add_handler(vyplata)

    from kasa.handlers.vyplata_hpp import (
        cmd_vyplata_hpp, on_vh_period, on_vh_confirm,
        VH_PERIOD, VH_CONFIRM,
    )
    vyplata_hpp = ConversationHandler(
        entry_points=[
            CommandHandler("vyplata_hpp", cmd_vyplata_hpp),
            MessageHandler(_exact(MENU_VYPLATA_HPP), cmd_vyplata_hpp),
        ],
        states={
            VH_PERIOD: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_vh_period)],
            VH_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_vh_confirm)],
        },
        fallbacks=[CommandHandler("zrusit", cmd_zrusit)],
        name="vyplata_hpp",
        persistent=True,
        allow_reentry=True,
    )
    app.add_handler(vyplata_hpp)
    # ℹ️ Nápověda — menu button + /napoveda (standalone, no conversation).
    app.add_handler(CommandHandler("napoveda", cmd_napoveda))
    app.add_handler(MessageHandler(_exact(MENU_NAPOVEDA), cmd_napoveda))
    # 📊 Přehled — per-day overview + /den detail (manager+, read-only).
    app.add_handler(CommandHandler("prehled", cmd_prehled))
    app.add_handler(CommandHandler("den", cmd_den))
    app.add_handler(MessageHandler(_exact(MENU_PREHLED), cmd_prehled))
    app.add_handler(CallbackQueryHandler(on_prehled_cb, pattern=r"^ph[dbw]:"))
    # /den prompt answer — replies to the "Který den" ForceReply. Group 1 so it
    # only runs when no active wizard (group 0) owns the message; on_den_reply
    # itself ignores replies that aren't to our prompt.
    app.add_handler(
        MessageHandler(filters.REPLY & filters.TEXT & ~filters.COMMAND, on_den_reply),
        group=1,
    )
    app.add_error_handler(on_error)

    log.info("Bot starting (polling)...")
    app.run_polling()


if __name__ == "__main__":
    main()
