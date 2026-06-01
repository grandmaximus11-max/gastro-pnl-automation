"""Tests for kasa.claude — classify náklad description to P&L category."""
from unittest.mock import MagicMock, patch


def test_classify_naklad_returns_valid_category():
    from kasa.claude import classify_naklad
    with patch("kasa.claude.anthropic.Anthropic") as mock_cls:
        client = MagicMock()
        mock_cls.return_value = client
        client.messages.create.return_value = MagicMock(
            content=[MagicMock(text="BL")]
        )
        cat = classify_naklad("led od zanzibaru", api_key="sk-x")
        assert cat == "BL"


def test_classify_naklad_falls_back_to_BO_on_invalid():
    from kasa.claude import classify_naklad
    with patch("kasa.claude.anthropic.Anthropic") as mock_cls:
        client = MagicMock()
        mock_cls.return_value = client
        client.messages.create.return_value = MagicMock(
            content=[MagicMock(text="XYZ_INVALID")]
        )
        # Invalid response → fall back to BO (běžné ostatní)
        cat = classify_naklad("něco nepochopitelného", api_key="sk-x")
        assert cat == "BO"
