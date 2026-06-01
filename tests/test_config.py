"""Tests for kasa.config — env loading and constants."""
import os
import pytest


def test_config_loads_bot_token(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN_KASA", "12345:test")
    monkeypatch.setenv("SHEETS_ID", "abc123")
    monkeypatch.setenv("SHARED_AUTH_SHEETS", "/tmp/auth.json")
    monkeypatch.setenv("SHARED_CLIENT_SECRET", "/tmp/client.json")
    monkeypatch.setenv("NOTIFY_OWNER_TG_ID", "123456789")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")

    from kasa.config import Config
    cfg = Config.from_env()

    assert cfg.bot_token == "12345:test"
    assert cfg.sheets_id == "abc123"
    assert cfg.shared_auth_path == "/tmp/auth.json"
    assert cfg.owner_tg_id == 123456789
    assert cfg.anthropic_api_key == "sk-x"


def test_config_constants():
    from kasa.config import SAZBA_SOLO, SAZBA_VICE, KROK_HODIN, DEFAULT_HOT_ZAC
    assert SAZBA_SOLO == 160
    assert SAZBA_VICE == 140
    assert KROK_HODIN == 0.25
    assert DEFAULT_HOT_ZAC == 5000


def test_config_missing_required_raises(monkeypatch):
    monkeypatch.delenv("BOT_TOKEN_KASA", raising=False)
    from kasa.config import Config
    with pytest.raises(RuntimeError, match="BOT_TOKEN_KASA"):
        Config.from_env()
