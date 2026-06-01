"""Tests for kasa.notify — pure message formatting."""


def test_format_vyplata_owner_msg_has_transfer_block():
    from kasa.notify import format_vyplata_owner_msg
    rows = [
        {"jmeno": "Lena", "prevodem": 12000, "hotove": 16000, "is_hpp": False},
        {"jmeno": "Mia", "prevodem": 0, "hotove": 15500, "is_hpp": False},
        {"jmeno": "Hugo", "is_hpp": True},
    ]
    msg = format_vyplata_owner_msg(("16.05.2026", "22.05.2026"), rows)
    assert "Lena" in msg and "Mia" in msg
    assert "Hugo (HPP)" in msg
    assert "K ODESLÁNÍ PŘEVODEM" in msg
    assert "Lena — 12 000 Kč" in msg
    assert "CELKEM: 12 000 Kč" in msg
    assert "31 500" in msg  # cash total 16000 + 15500


def test_format_vyplata_owner_msg_no_transfers():
    from kasa.notify import format_vyplata_owner_msg
    rows = [{"jmeno": "Mia", "prevodem": 0, "hotove": 15500, "is_hpp": False}]
    msg = format_vyplata_owner_msg(("16.05.2026", "22.05.2026"), rows)
    assert "Převodem: nic" in msg
