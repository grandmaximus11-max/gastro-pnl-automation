"""_finalize_vyplata writes correct P&L stav_platby and notifies the owner."""
import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_finalize_writes_split_and_notifies():
    from kasa.handlers import vyplata as V

    sheets = MagicMock()
    cfg = MagicMock()
    cfg.owner_tg_id = 555

    ctx = MagicMock()
    ctx.application.bot_data = {"sheets": sheets, "config": cfg}
    ctx.bot = AsyncMock()
    ctx.user_data = {
        "_v_period": ("16.05.2026", "22.05.2026"),
        "_v_agg": [
            {"jmeno": "Lena", "k_vyplate": 28000, "prevodem": 12000,
             "hotove": 16000, "new_dluh": 0, "is_hpp": False,
             "total_hodiny": 28, "total_plat": 0, "total_spropitne": 0,
             "total_personal_ucet": 0, "total_zalohy": 0},
            {"jmeno": "Hugo", "is_hpp": True},
        ],
    }

    update = MagicMock()
    update.effective_user.username = "adam"
    update.callback_query.edit_message_text = AsyncMock()

    result = await V._finalize_vyplata(update, ctx)

    from telegram.ext import ConversationHandler
    assert result == ConversationHandler.END

    # P&L: 2 строки для Lena (převod neuhrazeno + hotově default zaplaceno)
    pnl_calls = sheets.append_naklad_to_pnl.call_args_list
    assert len(pnl_calls) == 2
    by_zdroj = {c.kwargs["zaplaceno_zdroj"]: c.kwargs for c in pnl_calls}
    assert by_zdroj["BU Demo Bistro"]["stav_platby"] == "neuhrazeno"
    assert by_zdroj["BU Demo Bistro"]["castka"] == 12000
    assert "stav_platby" not in by_zdroj["Hotovost Demo Bistro"]
    assert by_zdroj["Hotovost Demo Bistro"]["castka"] == 16000

    # Vyplaty: одна строка (Lena), HPP пропущен
    assert sheets.append_vyplata.call_count == 1

    # Оповещение владельцу отправлено
    ctx.bot.send_message.assert_awaited_once()
    sent = ctx.bot.send_message.await_args.kwargs
    assert sent["chat_id"] == 555
    assert "K ODESLÁNÍ PŘEVODEM" in sent["text"]
