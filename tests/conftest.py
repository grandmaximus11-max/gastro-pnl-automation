"""Shared pytest fixtures for kasa_bot tests."""
import pytest


@pytest.fixture
def sample_smena_data() -> dict:
    """Minimal smena data used across calc tests."""
    return {
        "datum": "16.05.2026",
        "typ": "vice",
        "pocet_lidi": 3,
        "sazba_h": 140,
        "karta": 23714,
        "spropitne_karta": 1375,
        "hot_zac_celkem": 7608,
        "hot_kon_celkem": 12487,
        "trzba_pos_hot": 6885,
        "eshop_hotove": 960,
        "eshop_kartou": 0,
        "naklady_celkem": 0,
        "zalohy_neuhrazene": 0,
        "lidi": [
            {"jmeno": "Hugo", "hodiny": 10.5, "pers_ucet": 432, "zaloha": 0},
            {"jmeno": "Lena", "hodiny": 7.5, "pers_ucet": 232, "zaloha": 0},
            {"jmeno": "Mia", "hodiny": 7.5, "pers_ucet": 140, "zaloha": 0},
        ],
    }
