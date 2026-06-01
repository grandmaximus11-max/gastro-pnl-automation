"""Drive API wrapper: folders, doklad upload."""
from __future__ import annotations

from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload


class DriveClient:
    def __init__(self, auth_path: str) -> None:
        creds = Credentials.from_authorized_user_file(auth_path)
        self.service = build("drive", "v3", credentials=creds, cache_discovery=False)

    def ensure_folder(self, name: str, parent_id: str | None) -> str:
        """Find folder by exact name (under parent), or create. Returns folder id."""
        q_parts = [
            f"name = '{name}'",
            "mimeType = 'application/vnd.google-apps.folder'",
            "trashed = false",
        ]
        if parent_id:
            q_parts.append(f"'{parent_id}' in parents")
        q = " and ".join(q_parts)
        res = self.service.files().list(q=q, fields="files(id, name)").execute()
        files = res.get("files", [])
        if files:
            return files[0]["id"]

        body = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
        if parent_id:
            body["parents"] = [parent_id]
        created = self.service.files().create(body=body, fields="id").execute()
        return created["id"]

    def upload_doklad(self, local_path: str, parent_folder_id: str, target_name: str | None = None) -> str:
        """Upload a file to Drive folder. Returns webViewLink URL."""
        path = Path(local_path)
        name = target_name or path.name
        media = MediaFileUpload(str(path), resumable=False)
        body = {"name": name, "parents": [parent_folder_id]}
        file = self.service.files().create(
            body=body, media_body=media, fields="webViewLink"
        ).execute()
        return file["webViewLink"]

    def get_folder_webview(self, folder_id: str) -> str:
        return f"https://drive.google.com/drive/folders/{folder_id}"
