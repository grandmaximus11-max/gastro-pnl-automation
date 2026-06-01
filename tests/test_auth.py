"""Tests for kasa.auth — activation flow + role checks."""
from datetime import datetime, timedelta
import pytest


def _emp(**overrides):
    base = {
        "tg_user_id": "", "tg_username": "", "jmeno": "Mia",
        "role": "bartender", "aktivni": "FALSE", "notify_chyby": "FALSE",
        "aktivacni_kod": "DEMO-MIA-7384",
        "aktivovano_at": "", "zablokovan_do": "",
    }
    base.update(overrides)
    return base


def test_find_employee_by_tg_user_id():
    from kasa.auth import find_by_tg_user_id
    rows = [_emp(tg_user_id=100, jmeno="Hugo", aktivni="TRUE"),
            _emp(tg_user_id=200, jmeno="Lena", aktivni="TRUE")]
    assert find_by_tg_user_id(rows, 200)["jmeno"] == "Lena"
    assert find_by_tg_user_id(rows, 999) is None


def test_validate_activation_code_match():
    from kasa.auth import validate_activation_code
    rows = [_emp(jmeno="Mia", aktivacni_kod="DEMO-MIA-7384")]
    emp = validate_activation_code(rows, "DEMO-MIA-7384")
    assert emp is not None
    assert emp["jmeno"] == "Mia"


def test_validate_activation_code_no_match():
    from kasa.auth import validate_activation_code
    rows = [_emp(jmeno="Mia", aktivacni_kod="DEMO-MIA-7384")]
    assert validate_activation_code(rows, "WRONG-CODE") is None


def test_validate_activation_code_already_used():
    from kasa.auth import validate_activation_code
    rows = [_emp(jmeno="Mia", aktivacni_kod="", aktivovano_at="01.05.2026 12:00")]
    # Empty code = already used → reject even if input is empty
    assert validate_activation_code(rows, "") is None


def test_is_blocked_now():
    from kasa.auth import is_blocked
    future = (datetime.now() + timedelta(hours=1)).strftime("%d.%m.%Y %H:%M")
    past = (datetime.now() - timedelta(hours=1)).strftime("%d.%m.%Y %H:%M")
    assert is_blocked(_emp(zablokovan_do=future)) is True
    assert is_blocked(_emp(zablokovan_do=past)) is False
    assert is_blocked(_emp(zablokovan_do="")) is False


def test_role_check():
    from kasa.auth import has_role
    assert has_role(_emp(role="majitel"), "majitel") is True
    assert has_role(_emp(role="majitel"), "manager") is True  # majitel >= manager
    assert has_role(_emp(role="manager"), "majitel") is False
    assert has_role(_emp(role="bartender"), "manager") is False


def test_generate_activation_code():
    from kasa.auth import generate_activation_code
    code = generate_activation_code("Mia")
    assert code.startswith("DEMO-MIA-")
    assert len(code) == len("DEMO-MIA-1234")  # 4 digits
    # Different calls → different codes (random)
    assert generate_activation_code("Mia") != code or generate_activation_code("Mia") != code
