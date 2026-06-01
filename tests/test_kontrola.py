from datetime import date
import kontrola


def _row(num, *, b="", c="", d="", h="", i="", k="", q="", u="nezaplaceno", v=""):
    cells = [""] * 22
    cells[1], cells[2], cells[3], cells[7] = b, c, d, h
    cells[8], cells[10], cells[16] = i, k, q
    cells[20], cells[21] = u, v
    return (num, cells)


def _nk(num, *, datum="", castka="", smer="výdaj", who="", stav="open"):
    cells = [""] * 10
    cells[0], cells[1], cells[2], cells[3], cells[8] = datum, castka, smer, who, stav
    return (num, cells)


TODAY = date(2026, 5, 30)


def test_overdue():
    rows = [_row(2, b="20.05.2026", d="5000", h="Nájem", u="nezaplaceno")]
    s = kontrola.scan_anomalies(rows, [], TODAY)
    assert len(s["overdue"]) == 1
    assert s["overdue"][0]["days_over"] == 10
    assert s["overdue"][0]["row"] == 2


def test_soon():
    rows = [_row(2, b="01.06.2026", d="3000", h="Elektřina", u="nezaplaceno")]
    s = kontrola.scan_anomalies(rows, [], TODAY)
    assert len(s["soon"]) == 1
    assert s["soon"][0]["days_left"] == 2


def test_long_unpaid():
    rows = [_row(2, c="20.04.2026", d="900", h="Stará faktura", u="nezaplaceno")]
    s = kontrola.scan_anomalies(rows, [], TODAY)
    assert len(s["long_unpaid"]) == 1
    assert s["long_unpaid"][0]["days_old"] == 40


def test_paid_rows_not_in_unpaid_buckets():
    rows = [_row(2, b="20.05.2026", c="20.04.2026", d="900", u="zaplaceno", v="25.05.2026")]
    s = kontrola.scan_anomalies(rows, [], TODAY)
    assert s["overdue"] == [] and s["long_unpaid"] == []


def test_na_kontrolu_passthrough():
    nk = [_nk(2, datum="08.05.2026", castka="5724", who="Zanzibar")]
    s = kontrola.scan_anomalies([], nk, TODAY)
    assert len(s["na_kontrolu"]) == 1
    assert s["na_kontrolu"][0]["amount"] == "5724"


def test_late_paid():
    rows = [_row(2, c="01.04.2026", d="900", u="zaplaceno", v="25.05.2026")]
    s = kontrola.scan_anomalies(rows, [], TODAY)
    assert len(s["late_paid"]) == 1
    assert s["late_paid"][0]["lag_days"] == 54


def test_late_paid_within_30d_ignored():
    rows = [_row(2, c="01.05.2026", d="900", u="zaplaceno", v="10.05.2026")]
    s = kontrola.scan_anomalies(rows, [], TODAY)
    assert s["late_paid"] == []


def test_no_document():
    rows = [_row(2, c="10.05.2026", d="1200", h="Hotovost nákup", i="", k="")]
    s = kontrola.scan_anomalies(rows, [], TODAY)
    assert len(s["no_document"]) == 1
    assert s["no_document"][0]["row"] == 2


def test_no_document_satisfied_by_drive():
    rows = [_row(2, c="10.05.2026", d="1200", k="http://drive/x")]
    s = kontrola.scan_anomalies(rows, [], TODAY)
    assert s["no_document"] == []


def test_income_row_not_no_document():
    rows = [_row(2, c="10.05.2026", d="")]
    s = kontrola.scan_anomalies(rows, [], TODAY)
    assert s["no_document"] == []


def test_period_start_excludes_legacy():
    # forward-only: майская строка (до 1 июня) полностью игнорируется,
    # июньская — учитывается
    rows = [
        _row(2, b="20.05.2026", c="15.05.2026", d="5000", h="May legacy", u="nezaplaceno"),
        _row(3, b="20.06.2026", c="15.06.2026", d="3000", h="June", u="nezaplaceno"),
    ]
    s = kontrola.scan_anomalies(rows, [], date(2026, 6, 18), period_start=date(2026, 6, 1))
    assert all(it["row"] != 2 for it in s["overdue"])
    assert all(it["row"] != 2 for it in s["long_unpaid"])
    assert all(it["row"] != 2 for it in s["no_document"])
    assert any(it["row"] == 3 for it in s["soon"])  # June row due in 2 days


def test_no_period_start_includes_all():
    # без cutoff легаси по-прежнему видно (обратная совместимость)
    rows = [_row(2, b="20.05.2026", c="15.05.2026", d="5000", u="nezaplaceno")]
    s = kontrola.scan_anomalies(rows, [], date(2026, 6, 18))
    assert len(s["overdue"]) == 1
