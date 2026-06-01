"""Tests for kasa.kalkulace — pure calculations from a shift dict."""
import pytest


def test_expected_konec_basic(sample_smena_data):
    from kasa.kalkulace import expected_konec
    # e-shop hotově (960) NENÍ odečteno — prochází Dotykačkou a zůstává v kase.
    # 7608 + 6885 − 0 (náklady) − 0 (zálohy) = 14 493
    assert expected_konec(sample_smena_data) == 14_493


def test_pokladna_diff_from_row_real_shortfall_28_05():
    """Real Demo row 28.05.2026 — saved status=ok with no Chyby row, but the
    till reconciliation is 100 Kč short of the POS-reported cash tip. The
    recompute must surface it from raw columns."""
    from kasa.kalkulace import pokladna_diff_from_row
    row = {
        "hot_zac_celkem": "5124", "trzba_pos_hot": "2730",
        "naklady_celkem": "0", "zalohy_neuhrazene": "0",
        "hot_kon_celkem": "8373", "spropitne_hotov": "619",
    }
    # bot_surplus = 8373 − (5124+2730) = 519 ; recorded tip = 619 ; diff = −100
    assert pokladna_diff_from_row(row) == -100


def test_pokladna_diff_from_row_clears_stale_eshop_artifact_23_05():
    """Real Demo row 23.05.2026 — stored `rozdil` (2285) is inflated by the old
    pre-fix formula that subtracted eshop_hotove (920). Recompute with the
    current formula reconciles cleanly to 0 (cash tip 1365 == till surplus)."""
    from kasa.kalkulace import pokladna_diff_from_row
    row = {
        "hot_zac_celkem": "4997", "trzba_pos_hot": "5335",
        "naklady_celkem": "0", "zalohy_neuhrazene": "0",
        "hot_kon_celkem": "11697", "spropitne_hotov": "1365",
        "eshop_hotove": "920", "rozdil": "2285",  # stale stored fields, ignored
    }
    # bot_surplus = 11697 − (4997+5335) = 1365 ; recorded tip = 1365 ; diff = 0
    assert pokladna_diff_from_row(row) == 0


def test_pokladna_diff_from_row_handles_ru_formatted_cells():
    """Cells may arrive thin-space/comma formatted (ru_RU locale)."""
    from kasa.kalkulace import pokladna_diff_from_row
    row = {
        "hot_zac_celkem": "5 124", "trzba_pos_hot": "2 730",
        "hot_kon_celkem": "8 373", "spropitne_hotov": "619",
    }
    assert pokladna_diff_from_row(row) == -100


def test_rozdil_positive_is_cash_tip(sample_smena_data):
    from kasa.kalkulace import expected_konec, rozdil_a_tip_hotove
    # Přebytek nad očekáváním → spropitné hotově. Kotvíme na expected, aby test
    # zůstal správný nezávisle na přesné hodnotě expected_konec.
    exp = expected_konec(sample_smena_data)
    sample_smena_data["hot_kon_celkem"] = exp + 723
    diff = sample_smena_data["hot_kon_celkem"] - exp
    tip_hotove, chyba = rozdil_a_tip_hotove(diff)
    assert tip_hotove == 723
    assert chyba == 0


def test_rozdil_negative_is_chyba():
    from kasa.kalkulace import rozdil_a_tip_hotove
    tip_hotove, chyba = rozdil_a_tip_hotove(-564)
    assert tip_hotove == 0
    assert chyba == 564


def test_rozdil_zero():
    from kasa.kalkulace import rozdil_a_tip_hotove
    tip_hotove, chyba = rozdil_a_tip_hotove(0)
    assert tip_hotove == 0
    assert chyba == 0


def test_tip_per_hour(sample_smena_data):
    from kasa.kalkulace import tip_per_hour
    sample_smena_data["spropitne_karta"] = 1375
    sample_smena_data["spropitne_hotov"] = 723
    # total tips 2098, total hours 25.5 → 82.27 → round to 82
    assert tip_per_hour(sample_smena_data) == 82


def test_tip_per_hour_zero_hours():
    from kasa.kalkulace import tip_per_hour
    data = {"spropitne_karta": 100, "spropitne_hotov": 0, "lidi": []}
    assert tip_per_hour(data) == 0  # avoid division by zero


def test_k_vyplate_per_person(sample_smena_data):
    from kasa.kalkulace import k_vyplate_per_person
    sample_smena_data["spropitne_karta"] = 1375
    sample_smena_data["spropitne_hotov"] = 723
    rows = k_vyplate_per_person(sample_smena_data)
    assert len(rows) == 3
    hugo = rows[0]
    assert hugo["jmeno"] == "Hugo"
    # 10.5 × 140 = 1470 plat
    # tipy: largest-remainder z poolu 2098 Kč / 25.5 h → Hugo 864
    #   (2098 × 10.5 / 25.5 = 863.96 → +1 reziduum; součet všech = 2098)
    # k_vyplate = 1470 + 864 − 432 − 0 = 1902
    assert hugo["plat"] == 1470
    assert hugo["spropitne"] == 864
    assert hugo["k_vyplate"] == 1902


def test_round_to_100_classical():
    from kasa.kalkulace import round_to_100
    # Below midpoint → down
    assert round_to_100(7249) == 7200
    assert round_to_100(7220) == 7200
    # At/above midpoint → up
    assert round_to_100(7250) == 7300  # half rounds up (key case)
    assert round_to_100(7280) == 7300
    assert round_to_100(11275) == 11300
    # Match user's real spreadsheet examples
    assert round_to_100(7250) == 7300  # Lily case: 7223 + 27 carry = 7250 → 7300
    assert round_to_100(3280) == 3300  # Mia case: 3235 + 45 carry = 3280 → 3300
    # Exact multiples — unchanged
    assert round_to_100(7200) == 7200
    assert round_to_100(0) == 0
    # Small numbers
    assert round_to_100(49) == 0
    assert round_to_100(50) == 100
    # Negative
    assert round_to_100(-50) == -100
    assert round_to_100(-49) == 0


def test_aggregate_week():
    from kasa.kalkulace import aggregate_week
    shifts = [
        {"sazba_h": 140, "spropitne_karta": 1000, "spropitne_hotov": 500,
         "lidi": [
             {"jmeno": "Hugo", "hodiny": 10, "pers_ucet": 100, "zaloha": 0},
             {"jmeno": "Lena", "hodiny": 8, "pers_ucet": 50, "zaloha": 0},
         ]},
        {"sazba_h": 160, "spropitne_karta": 400, "spropitne_hotov": 100,
         "lidi": [
             {"jmeno": "Hugo", "hodiny": 9, "pers_ucet": 200, "zaloha": 500},
         ]},
    ]
    result = aggregate_week(shifts)
    hugo = next(r for r in result if r["jmeno"] == "Hugo")
    # Shift 1 (pool 1500 / 18 h, largest-remainder): Hugo 833, Lena 667
    #   (Hugo 1500×10/18 = 833.33; reziduum +1 jde Lena s vyšším zbytkem)
    # Shift 2 (pool 500 / 9 h, jediná osoba): Hugo 500
    # plat total = 1400 + 1440 = 2840
    # tip total = 833 + 500 = 1333
    # pers_ucet = 100 + 200 = 300 ; zaloha = 500
    # k_vyplate = 2840 + 1333 − 300 − 500 = 3373
    assert hugo["total_hodiny"] == 19
    assert hugo["total_plat"] == 2840
    assert hugo["total_personal_ucet"] == 300
    assert hugo["total_zalohy"] == 500
    assert hugo["k_vyplate"] == 3373


def test_split_for_method_hotove():
    from kasa.kalkulace import split_for_method
    assert split_for_method(1500, "hotove", 9999) == (0, 1500)


def test_split_for_method_prevodem():
    from kasa.kalkulace import split_for_method
    assert split_for_method(1500, "prevodem", 9999) == (1500, 0)


def test_split_for_method_mix_caps_at_headroom():
    from kasa.kalkulace import split_for_method
    assert split_for_method(1500, "mix", 1000) == (1000, 500)
    assert split_for_method(1500, "mix", 0) == (0, 1500)


def test_cycle_payout_method_basic():
    from kasa.kalkulace import cycle_payout_method
    assert cycle_payout_method("mix", 1500, 2000) == "hotove"
    assert cycle_payout_method("hotove", 1500, 2000) == "prevodem"
    assert cycle_payout_method("prevodem", 1500, 2000) == "mix"


def test_cycle_payout_method_skips_prevodem_over_limit():
    from kasa.kalkulace import cycle_payout_method
    assert cycle_payout_method("hotove", 1500, 1000) == "mix"
