"""Tests for /uzaverka wizard transition validators (pure logic)."""
import pytest


def test_parse_hours_valid():
    from kasa.handlers.uzaverka import parse_hours
    assert parse_hours("9.5") == 9.5
    assert parse_hours("9,5") == 9.5  # Czech comma decimal
    assert parse_hours("10") == 10.0
    assert parse_hours("10.25") == 10.25


def test_parse_hours_invalid_step():
    from kasa.handlers.uzaverka import parse_hours
    with pytest.raises(ValueError):
        parse_hours("9.3")  # not a 0.25 multiple


def test_parse_hours_garbage():
    from kasa.handlers.uzaverka import parse_hours
    with pytest.raises(ValueError):
        parse_hours("abc")


def test_parse_kc_strips_kc_suffix():
    from kasa.handlers.uzaverka import parse_kc
    assert parse_kc("1500") == 1500
    assert parse_kc("1 500 Kč") == 1500
    assert parse_kc("23 714") == 23714
    assert parse_kc("23.714") == 23714  # Czech thousand sep


def test_decide_sazba():
    from kasa.handlers.uzaverka import decide_sazba
    assert decide_sazba("solo") == 160
    assert decide_sazba("vice") == 140


# ── /uzaverka access gating (activated employee, role bartender+) ──────

class _Msg:
    def __init__(self):
        self.sent = []

    async def reply_text(self, text, reply_markup=None):
        self.sent.append(text)


class _Upd:
    def __init__(self, uid):
        self.message = _Msg()
        self.effective_user = type("U", (), {"id": uid, "username": "u"})()


class _Sheets:
    def __init__(self, rows):
        self._rows = rows

    def get_zamestnanci(self):
        return self._rows

    def read_last_smena_carryover(self):
        return {"fond": 5000, "mince": 0}


class _Ctx:
    def __init__(self, rows):
        self.args = []
        self.user_data = {}
        self.application = type("A", (), {"bot_data": {"sheets": _Sheets(rows)}})()


async def test_uzaverka_rejects_non_employee():
    from kasa.handlers.uzaverka import cmd_uzaverka
    from telegram.ext import ConversationHandler
    upd, ctx = _Upd(uid=999), _Ctx(rows=[])  # nobody in Zamestnanci
    result = await cmd_uzaverka(upd, ctx)
    assert result == ConversationHandler.END
    assert any("aktiv" in s.lower() for s in upd.message.sent)


async def test_uzaverka_allows_bartender():
    from kasa.handlers.uzaverka import cmd_uzaverka, Z_TYP
    rows = [{"tg_user_id": "42", "jmeno": "Hugo", "role": "bartender", "aktivni": "TRUE"}]
    upd, ctx = _Upd(uid=42), _Ctx(rows=rows)
    result = await cmd_uzaverka(upd, ctx)
    assert result == Z_TYP
