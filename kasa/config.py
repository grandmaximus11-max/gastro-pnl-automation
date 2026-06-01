"""Configuration: load .env, expose typed constants."""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Auto-load .env from repo root on import.
# Clear empty shell env vars first so .env can populate them — empty shell vars
# (e.g. `export ANTHROPIC_API_KEY=` in shell profile) otherwise shadow .env values.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SHADOWABLE_KEYS = (
    "BOT_TOKEN_KASA", "SHEETS_ID", "SHARED_AUTH_SHEETS",
    "SHARED_CLIENT_SECRET", "NOTIFY_OWNER_TG_ID",
    "NOTIFY_MANAGER_TG_ID", "ANTHROPIC_API_KEY",
    "DRIVE_UMBRELLA_FOLDER_ID",
)
for _k in _SHADOWABLE_KEYS:
    if _k in os.environ and not os.environ[_k]:
        del os.environ[_k]
load_dotenv(_REPO_ROOT / ".env")

# Business constants
SAZBA_SOLO: int = 160          # Kč/h on solo shifts (Mon-Thu, Sun)
SAZBA_VICE: int = 140          # Kč/h on weekend shifts
KROK_HODIN: float = 0.25       # smallest hour increment
DEFAULT_HOT_ZAC: int = 5000    # fallback initial cash if no previous shift

# Business-day cutoff: a shift closed before this hour belongs to the PREVIOUS
# calendar day. Bar works until ~01:00, close-out happens after midnight — so
# closing at 01:30 on the 26th is still "the 25th's shift". 6 covers any late
# close-out without capturing genuine daytime shifts. Override per-shift with
# `/uzaverka DD.MM.YYYY`.
BUSINESS_DAY_CUTOFF_HOUR: int = 6

# Sheet tab names
TAB_SMENY = "Smeny"
TAB_VYPLATY = "Vyplaty"
TAB_ZAMESTNANCI = "Zamestnanci"
TAB_CHYBY = "Chyby"
TAB_PNL = "P&L"

# Drive root
DRIVE_ROOT_UZAVERKY = "Uzávěrky"


@dataclass(frozen=True)
class Config:
    bot_token: str
    sheets_id: str
    shared_auth_path: str
    shared_client_secret_path: str
    owner_tg_id: int
    manager_tg_id: int | None
    anthropic_api_key: str
    drive_umbrella_id: str | None  # all bot Drive artifacts go under this folder

    @classmethod
    def from_env(cls) -> "Config":
        def _req(key: str) -> str:
            val = os.environ.get(key)
            if not val:
                raise RuntimeError(f"Missing required env var: {key}")
            return val

        manager_raw = os.environ.get("NOTIFY_MANAGER_TG_ID", "").strip()
        umbrella = os.environ.get("DRIVE_UMBRELLA_FOLDER_ID", "").strip()
        return cls(
            bot_token=_req("BOT_TOKEN_KASA"),
            sheets_id=_req("SHEETS_ID"),
            shared_auth_path=_req("SHARED_AUTH_SHEETS"),
            shared_client_secret_path=_req("SHARED_CLIENT_SECRET"),
            owner_tg_id=int(_req("NOTIFY_OWNER_TG_ID")),
            manager_tg_id=int(manager_raw) if manager_raw else None,
            anthropic_api_key=_req("ANTHROPIC_API_KEY"),
            drive_umbrella_id=umbrella or None,
        )
