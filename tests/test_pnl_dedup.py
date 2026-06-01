import pnl_dedup


def _row(num, *, a="", c="", d="", h="", k="", n="", o="", p="", q="", s="", t=""):
    cells = [""] * 22
    cells[0], cells[2], cells[3], cells[7] = a, c, d, h
    cells[10] = k
    cells[13], cells[14], cells[15] = n, o, p
    cells[16], cells[18], cells[19] = q, s, t
    return (num, cells)


def test_strong_dup_by_vs():
    rows = [_row(2, d="5724", h="Zanzibar", s="111")]
    r = pnl_dedup.classify_entry({"amount": "5724", "var_symbol": "111"}, rows)
    assert r == {"kind": "strong_dup", "row_number": 2, "matched_on": "vs"}


def test_strong_dup_by_doklad():
    rows = [_row(2, d="999", t="FV-7")]
    r = pnl_dedup.classify_entry({"amount": "999", "cislo_dokladu": "FV-7"}, rows)
    assert r == {"kind": "strong_dup", "row_number": 2, "matched_on": "doklad"}


def test_clean_when_nothing_matches():
    rows = [_row(2, d="100", h="Pivo", s="111")]
    r = pnl_dedup.classify_entry({"amount": "5724", "var_symbol": "999"}, rows)
    assert r == {"kind": "clean"}


def test_weak_enrich_new_brings_vs():
    rows = [_row(2, d="888", c="10.05.2026", q="Zanzibar")]
    new = {"amount": "888", "date": "11.05.2026", "dodavatel": "Zanzibar",
           "var_symbol": "VS-1"}
    r = pnl_dedup.classify_entry(new, rows)
    assert r["kind"] == "weak_enrich"
    assert r["row_number"] == 2
    assert r["missing"][pnl_dedup.COL_S] == "VS-1"


def test_weak_enrich_new_brings_drive_and_dph():
    rows = [_row(2, d="500", c="10.05.2026", q="Makro")]
    new = {"amount": "500", "date": "10.05.2026", "dodavatel": "Makro",
           "drive_url": "http://d/1", "sazba_dph": "21", "dph": "86", "zaklad_dph": "414"}
    r = pnl_dedup.classify_entry(new, rows)
    assert r["kind"] == "weak_enrich"
    assert r["missing"][pnl_dedup.COL_K] == "http://d/1"
    assert r["missing"][pnl_dedup.COL_N] == "21"


def test_weak_no_enrich_pure_repeat():
    rows2 = [_row(3, d="300", c="10.05.2026", q="Pivo")]
    new2 = {"amount": "300", "date": "10.05.2026", "dodavatel": "Pivo"}
    r = pnl_dedup.classify_entry(new2, rows2)
    assert r == {"kind": "weak_no_enrich", "row_number": 3}


def test_weak_match_needs_amount_equal():
    rows = [_row(2, d="888", c="10.05.2026", q="Zanzibar")]
    new = {"amount": "999", "date": "10.05.2026", "dodavatel": "Zanzibar", "var_symbol": "X"}
    assert pnl_dedup.classify_entry(new, rows) == {"kind": "clean"}


def test_weak_match_outside_window_and_diff_supplier_is_clean():
    rows = [_row(2, d="888", c="01.01.2026", q="Zanzibar")]
    new = {"amount": "888", "date": "10.05.2026", "dodavatel": "Other", "var_symbol": "X"}
    assert pnl_dedup.classify_entry(new, rows) == {"kind": "clean"}
