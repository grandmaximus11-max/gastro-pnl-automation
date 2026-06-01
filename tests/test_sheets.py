"""Tests for kasa.sheets — gspread wrapper with mocked client."""
from unittest.mock import MagicMock, patch


def test_sheets_client_initialization():
    from kasa.sheets import SheetsClient
    with patch("kasa.sheets.gspread.authorize") as mock_auth, \
         patch("kasa.sheets.Credentials.from_authorized_user_file") as mock_creds:
        mock_gc = MagicMock()
        mock_sh = MagicMock()
        mock_auth.return_value = mock_gc
        mock_gc.open_by_key.return_value = mock_sh
        mock_creds.return_value = MagicMock()

        client = SheetsClient(
            auth_path="/tmp/auth.json",
            sheets_id="abc123",
        )

        assert client.spreadsheet is mock_sh
        mock_gc.open_by_key.assert_called_once_with("abc123")


def test_get_zamestnanci_returns_list_of_dicts():
    from kasa.sheets import SheetsClient
    with patch("kasa.sheets.gspread.authorize"), \
         patch.object(SheetsClient, "__init__", lambda self, *_, **__: None):
        client = SheetsClient.__new__(SheetsClient)
        client.spreadsheet = MagicMock()
        ws = MagicMock()
        ws.get_all_records.return_value = [
            {"tg_user_id": 123456789, "jmeno": "Max", "role": "majitel", "aktivni": True},
            {"tg_user_id": "", "jmeno": "Mia", "role": "bartender", "aktivni": False},
        ]
        client.spreadsheet.worksheet.return_value = ws
        rows = client.get_zamestnanci()
        assert len(rows) == 2
        assert rows[0]["jmeno"] == "Max"


def test_activate_employee_writes_row():
    from kasa.sheets import SheetsClient
    client = SheetsClient.__new__(SheetsClient)
    ws = MagicMock()
    ws.get_all_records.return_value = [
        {"jmeno": "Mia", "aktivacni_kod": "DEMO-MIA-7384", "aktivni": "FALSE"}
    ]
    client.spreadsheet = MagicMock()
    client.spreadsheet.worksheet.return_value = ws

    client.activate_employee(tg_user_id=300, tg_username="m_tg", code="DEMO-MIA-7384")
    # Should write tg_user_id, tg_username, aktivni=TRUE, clear code, set aktivovano_at
    # Verify ws.batch_update was called
    assert ws.batch_update.called or ws.update.called


def test_append_smena_writes_row(sample_smena_data):
    from kasa.sheets import SheetsClient
    client = SheetsClient.__new__(SheetsClient)
    ws = MagicMock()
    client.spreadsheet = MagicMock()
    client.spreadsheet.worksheet.return_value = ws

    # Add fully-populated computed fields (as bot would before write)
    sample_smena_data.update({
        "smena_id": "16.05.2026",
        "spropitne_hotov": 723,
        "rozdil": 723,
        "expected_konec": 11764,
        "naklady_celkem": 0,
        "zalohy_neuhrazene": 0,
        "zodpovedny": "Hugo",
        "status": "ok",
        "drive_folder_url": "",
        "created_by_tg": "hugo_tg",
    })
    client.append_smena(sample_smena_data)
    ws.append_row.assert_called_once()


def test_read_last_smena_returns_dict():
    from kasa.sheets import SheetsClient
    client = SheetsClient.__new__(SheetsClient)
    ws = MagicMock()
    ws.get_all_records.return_value = [
        {"smena_id": "15.05.2026", "hot_kon_celkem": 11000},
        {"smena_id": "16.05.2026", "hot_kon_celkem": 12487},
    ]
    client.spreadsheet = MagicMock()
    client.spreadsheet.worksheet.return_value = ws
    last = client.read_last_smena()
    assert last["smena_id"] == "16.05.2026"  # last in list
    assert last["hot_kon_celkem"] == 12487


def test_read_last_smena_empty_returns_none():
    from kasa.sheets import SheetsClient
    client = SheetsClient.__new__(SheetsClient)
    ws = MagicMock()
    ws.get_all_records.return_value = []
    client.spreadsheet = MagicMock()
    client.spreadsheet.worksheet.return_value = ws
    assert client.read_last_smena() is None


def test_append_naklad_to_pnl():
    from kasa.sheets import SheetsClient
    client = SheetsClient.__new__(SheetsClient)
    ws = MagicMock()
    client.spreadsheet = MagicMock()
    client.spreadsheet.worksheet.return_value = ws

    client.append_naklad_to_pnl(
        datum="16.05.2026",
        castka=500,
        popis="led od Zanzibaru",
        kategorie="BL",
        zaplaceno_zdroj="Hotovost Demo",
        kdo_zapsal="Hugo",
        doklad_url="https://drive.google.com/file/d/abc",
        smena_id="16.05.2026",
    )
    ws.append_row.assert_called_once()
    args = ws.append_row.call_args
    row = args.args[0] if args.args else args.kwargs.get("values")
    # 22 columns (A..V): A-L management view + M-V DPH detail
    assert len(row) == 22
    assert row[0] == "BL"            # A kategorie (Квалификация)
    assert row[3] == 500             # D castka (Сумма расхода)
    assert row[4] == ""              # E доход (empty for expense)
    assert row[5] == "Hotovost Demo"  # F zdroj (Откуда платилось)
    # popis (H = Что за статья) prefixed with smena marker
    assert "[smena 16.05.2026]" in row[7]
    assert row[10] == "https://drive.google.com/file/d/abc"  # K Drive URL
    assert row[11] == ""             # L Комментарий (default empty)
    # Cash/kasa default: paid immediately. (Transfer payouts in /vyplata pass
    # stav_platby="neuhrazeno" → U="neuhrazeno", V="" until FIO matches.)
    assert row[20] == "zaplaceno"    # U Stav platby (default)
    assert row[21] == "16.05.2026"   # V Datum úhrady (= datum when zaplaceno)
