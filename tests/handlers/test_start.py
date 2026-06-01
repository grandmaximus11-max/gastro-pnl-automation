"""Tests for /start handler logic — extracted as pure function for testability."""
from datetime import datetime
from unittest.mock import MagicMock


def test_handle_start_existing_active_user():
    from kasa.handlers.start import handle_start_message
    emp = {"jmeno": "Hugo", "role": "bartender", "aktivni": "TRUE", "tg_user_id": 100}
    sheets = MagicMock()
    sheets.get_zamestnanci.return_value = [emp]
    reply = handle_start_message(sheets, tg_user_id=100)
    assert "Ahoj, Hugo" in reply or "Vítej zpět, Hugo" in reply


def test_handle_start_unknown_user_requests_code():
    from kasa.handlers.start import handle_start_message
    sheets = MagicMock()
    sheets.get_zamestnanci.return_value = []
    reply = handle_start_message(sheets, tg_user_id=999)
    assert "kód" in reply.lower() or "aktivace" in reply.lower()


def test_handle_activation_code_correct():
    from kasa.handlers.start import handle_activation_code
    sheets = MagicMock()
    sheets.get_zamestnanci.return_value = [
        {"jmeno": "Mia", "aktivacni_kod": "DEMO-MIA-7384", "aktivni": "FALSE",
         "tg_user_id": "", "role": "bartender", "zablokovan_do": ""}
    ]
    result = handle_activation_code(sheets, tg_user_id=300, tg_username="mia_tg", code="DEMO-MIA-7384")
    assert result["status"] == "activated"
    assert result["jmeno"] == "Mia"
    sheets.activate_employee.assert_called_once()


def test_handle_activation_code_wrong():
    from kasa.handlers.start import handle_activation_code
    sheets = MagicMock()
    sheets.get_zamestnanci.return_value = []
    sheets.failed_attempts_get.return_value = 0
    result = handle_activation_code(sheets, tg_user_id=300, tg_username="", code="WRONG")
    assert result["status"] == "wrong"
    sheets.failed_attempts_incr.assert_called_once_with(300)


def test_handle_activation_code_third_strike_blocks():
    from kasa.handlers.start import handle_activation_code
    sheets = MagicMock()
    sheets.get_zamestnanci.return_value = []
    sheets.failed_attempts_get.return_value = 2  # already 2 fails, this is 3rd
    result = handle_activation_code(sheets, tg_user_id=300, tg_username="", code="WRONG")
    assert result["status"] == "blocked"
    sheets.block_user.assert_called_once()
