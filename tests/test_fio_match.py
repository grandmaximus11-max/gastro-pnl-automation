from datetime import date
import fio_match


def _row(num, *, b="", c="", d="", vs="", t="", u="nezaplaceno", q="", h="x"):
    cells = [""] * 22
    cells[1], cells[2], cells[3] = b, c, d
    cells[7] = h
    cells[16], cells[18], cells[19], cells[20] = q, vs, t, u
    return (num, cells)


def _pmt(amount, *, dd, vs="", dodavatel="", signed=None):
    return {
        "id": f"id-{amount}-{vs}", "amount": str(amount),
        "signed": signed if signed is not None else -float(amount),
        "date": dd, "var_symbol": vs, "dodavatel": dodavatel, "info": "",
    }


def test_group_unpaid_by_doklad_sums_amounts():
    rows = [
        _row(2, d="100", t="FV-1", u="nezaplaceno"),
        _row(3, d="200", t="FV-1", u="nezaplaceno"),
        _row(4, d="999", t="FV-2", u="nezaplaceno"),
    ]
    groups = fio_match.group_unpaid_by_doklad(rows)
    assert groups["FV-1"]["total"] == 300
    assert groups["FV-1"]["row_numbers"] == [2, 3]
    assert groups["FV-2"]["total"] == 999


def test_phase0_vs_exact_match():
    rows = [_row(2, d="5724", vs="111", t="FV-9")]
    r = fio_match.match_payment(_pmt(5724, dd=date(2026,5,20), vs="111"), rows)
    assert r == {"kind": "paid", "row_numbers": [2]}


def test_phase1_doklad_match_when_no_vs():
    rows = [_row(2, d="300", t="FV-7")]
    r = fio_match.match_payment(_pmt(300, dd=date(2026,5,20)), rows)
    assert r["kind"] == "paid" and r["row_numbers"] == [2]


def test_phase1_multirate_group_by_t():
    rows = [_row(2, d="100", t="FV-1"), _row(3, d="200", t="FV-1")]
    r = fio_match.match_payment(_pmt(300, dd=date(2026,5,20)), rows)
    assert r["kind"] == "paid" and sorted(r["row_numbers"]) == [2, 3]


def test_phase2_dodavatel_amount_within_90d():
    rows = [_row(2, d="888", q="Zanzibar", c="01.03.2026")]
    r = fio_match.match_payment(_pmt(888, dd=date(2026,5,20), dodavatel="Zanzibar"), rows)
    assert r["kind"] == "paid" and r["row_numbers"] == [2]


def test_phase2_outside_90d_is_orphan():
    rows = [_row(2, d="888", q="Zanzibar", c="01.01.2026")]
    r = fio_match.match_payment(_pmt(888, dd=date(2026,5,20), dodavatel="Zanzibar"), rows)
    assert r["kind"] == "orphan"


def test_phase3_amount_only_within_90d():
    rows = [_row(2, d="450", c="01.05.2026")]
    r = fio_match.match_payment(_pmt(450, dd=date(2026,5,20)), rows)
    assert r["kind"] == "paid" and r["row_numbers"] == [2]


def test_amount_ambiguous_two_candidates():
    rows = [_row(2, d="450", c="01.05.2026"), _row(3, d="450", c="02.05.2026")]
    r = fio_match.match_payment(_pmt(450, dd=date(2026,5,20)), rows)
    assert r["kind"] == "ambiguous" and sorted(r["row_numbers"]) == [2, 3]


def test_vs_guard_different_vs_not_matched():
    rows = [_row(2, d="5724", vs="111")]
    r = fio_match.match_payment(_pmt(5724, dd=date(2026,5,20), vs="222"), rows)
    assert r["kind"] == "orphan"


def test_incoming_payment_skipped_as_orphan():
    rows = [_row(2, d="980", c="01.05.2026")]
    r = fio_match.match_payment(_pmt(980, dd=date(2026,5,20), signed=+980.0), rows)
    assert r["kind"] == "orphan"


def test_already_paid_rows_ignored():
    rows = [_row(2, d="450", c="01.05.2026", u="zaplaceno")]
    r = fio_match.match_payment(_pmt(450, dd=date(2026,5,20)), rows)
    assert r["kind"] == "orphan"


def test_phase1_vs_guard_blocks_group_match():
    # платёж с VS=222 не должен матчиться к T-группе со строками VS=111
    rows = [_row(2, d="300", t="FV-1", vs="111")]
    r = fio_match.match_payment(_pmt(300, dd=date(2026,5,20), vs="222"), rows)
    assert r["kind"] == "orphan"
