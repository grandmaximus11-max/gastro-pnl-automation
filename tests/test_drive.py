"""Tests for kasa.drive — folder lookup/create + doklad upload."""
from unittest.mock import MagicMock, patch


def test_ensure_folder_creates_when_missing():
    from kasa.drive import DriveClient
    client = DriveClient.__new__(DriveClient)
    client.service = MagicMock()
    # No matches found → must create
    client.service.files.return_value.list.return_value.execute.return_value = {"files": []}
    client.service.files.return_value.create.return_value.execute.return_value = {"id": "NEW_ID"}
    folder_id = client.ensure_folder("Uzávěrky", parent_id=None)
    assert folder_id == "NEW_ID"
    client.service.files.return_value.create.assert_called_once()


def test_ensure_folder_returns_existing():
    from kasa.drive import DriveClient
    client = DriveClient.__new__(DriveClient)
    client.service = MagicMock()
    client.service.files.return_value.list.return_value.execute.return_value = {
        "files": [{"id": "EXISTING", "name": "Uzávěrky"}]
    }
    folder_id = client.ensure_folder("Uzávěrky", parent_id=None)
    assert folder_id == "EXISTING"
