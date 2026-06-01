import base64
import json
import os
import re
import tempfile
from datetime import date, datetime, timedelta, timezone
from datetime import time as dt_time
from email.utils import parsedate

import anthropic
import fio_match
import gspread
import httpx
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PicklePersistence,
    filters,
)

load_dotenv(override=True)  # shell may have empty env vars shadowing .env

# ── Валидация переменных окружения ────────────────────────────

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("Ошибка: BOT_TOKEN не найден в .env")
if not re.match(r"^\d+:[A-Za-z0-9_-]{35,}$", BOT_TOKEN):
    raise SystemExit(f"Ошибка: BOT_TOKEN некорректен: {BOT_TOKEN!r}")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
if not ANTHROPIC_API_KEY:
    raise SystemExit("Ошибка: ANTHROPIC_API_KEY не найден в .env")

FIO_TOKEN          = os.getenv("FIO_TOKEN", "")
NOTIFY_CHAT_ID_ENV = os.getenv("NOTIFY_CHAT_ID", "")

# Gmail
GMAIL_LABEL_NAME = os.getenv("GMAIL_LABEL_NAME", "DemoBot")
GMAIL_SUBJECTS   = [s.strip() for s in os.getenv(
    "GMAIL_SUBJECTS", "faktura,invoice,фактура,rechnung,счёт"
).split(",") if s.strip()]

# Drive папка для фактур: Demo Faktury / 5.26 / 8.5 / file.pdf
_FAKTURY_FOLDER_ID: str = os.getenv("DRIVE_FAKTURY_FOLDER_ID", "")

# Кэш Drive folder IDs (persistent: чтобы не звать Drive API при каждом старте)
DRIVE_CACHE_FILE = "drive_folder_cache.json"


def _load_drive_cache() -> dict:
    if os.path.exists(DRIVE_CACHE_FILE):
        try:
            return json.load(open(DRIVE_CACHE_FILE))
        except Exception:
            return {}
    return {}


def _save_drive_cache(cache: dict) -> None:
    try:
        with open(DRIVE_CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        print(f"Drive cache save error: {e}")


_drive_cache: dict = _load_drive_cache()

# Список ID уже импортированных FIO транзакций (чтоб не дублировать при повторных проверках)
FIO_IMPORTED_FILE = "fio_imported_ids.json"


def _load_fio_imported() -> set[str]:
    if os.path.exists(FIO_IMPORTED_FILE):
        try:
            return set(json.load(open(FIO_IMPORTED_FILE)))
        except Exception:
            return set()
    return set()


def _save_fio_imported(ids: set[str]) -> None:
    try:
        with open(FIO_IMPORTED_FILE, "w") as f:
            json.dump(sorted(ids), f)
    except Exception as e:
        print(f"FIO imported save error: {e}")

# Chat ID для уведомлений: сначала из .env, потом из файла (сохраняется /start)
CHAT_ID_FILE = "notify_chat_id.txt"


def load_notify_chat_id() -> str:
    if NOTIFY_CHAT_ID_ENV:
        return NOTIFY_CHAT_ID_ENV
    if os.path.exists(CHAT_ID_FILE):
        return open(CHAT_ID_FILE).read().strip()
    return ""


def save_notify_chat_id(chat_id: str) -> None:
    with open(CHAT_ID_FILE, "w") as f:
        f.write(chat_id)


# ── Google Sheets ─────────────────────────────────────────────

# Sheet ID берётся из .env (SHEETS_ID). Если не задан — fallback на PROD.
# Это позволяет переключаться между TEST и PROD без правки кода: меняем .env.
_SHEET_ID_FALLBACK = "YOUR_SHEET_ID"  # PROD
SHEET_ID         = os.getenv("SHEETS_ID", _SHEET_ID_FALLBACK).strip()
SHEET_URL        = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit?gid=0#gid=0"
print(f"[sheets] using {'TEST' if SHEET_ID != _SHEET_ID_FALLBACK else 'PROD'} sheet: {SHEET_ID}")
DRIVE_FOLDER_NAME = "Demo Receipts"

# Credentials из authorized_user.json (создаётся gspread.oauth один раз)
with open("authorized_user.json") as _f:
    _user_info = json.load(_f)

creds = Credentials.from_authorized_user_info(_user_info, _user_info.get("scopes"))
if creds.expired and creds.refresh_token:
    creds.refresh(Request())

gc          = gspread.authorize(creds)
spreadsheet = gc.open_by_url(SHEET_URL)   # нужен для batch_update (цвета)
worksheet   = spreadsheet.sheet1

# ── Google Drive ──────────────────────────────────────────────

drive = build("drive", "v3", credentials=creds)


def get_or_create_drive_folder(name: str) -> str:
    resp = drive.files().list(
        q=f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false",
        fields="files(id, name)",
        spaces="drive",
    ).execute()
    files = resp.get("files", [])
    if files:
        folder_id = files[0]["id"]
        print(f"📁 Папка Drive найдена: {name} (id={folder_id})")
        return folder_id
    folder = drive.files().create(
        body={"name": name, "mimeType": "application/vnd.google-apps.folder"},
        fields="id",
    ).execute()
    folder_id = folder["id"]
    print(f"📁 Папка Drive создана: {name} (id={folder_id})")
    return folder_id


_env_folder_id = re.sub(r"\?.*$", "", os.getenv("GDRIVE_FOLDER_ID", "").strip())
if _env_folder_id:
    DRIVE_FOLDER_ID = _env_folder_id
    print(f"📁 Используется папка из .env (id={DRIVE_FOLDER_ID})")
else:
    DRIVE_FOLDER_ID = get_or_create_drive_folder(DRIVE_FOLDER_NAME)


def upload_to_drive(file_path: str, filename: str, mime_type: str = "image/jpeg",
                    folder_id: str = "") -> str:
    """Загружает файл в папку Drive. folder_id — конкретная папка, иначе DRIVE_FOLDER_ID."""
    target = folder_id or DRIVE_FOLDER_ID
    media     = MediaFileUpload(file_path, mimetype=mime_type, resumable=False)
    file_meta = {"name": filename, "parents": [target]}
    uploaded  = drive.files().create(
        body=file_meta, media_body=media, fields="id, webViewLink"
    ).execute()
    drive.permissions().create(
        fileId=uploaded["id"],
        body={"type": "anyone", "role": "reader"},
    ).execute()
    return uploaded.get("webViewLink", "")


# ── Gmail API ─────────────────────────────────────────────────

_gmail_svc = None   # ленивая инициализация


def get_gmail_service():
    """
    Gmail использует ОТДЕЛЬНЫЕ креды (authorized_gmail.json) —
    другой аккаунт чем Sheets/Drive. Запусти `python3 setup_gmail.py` один раз.
    """
    global _gmail_svc
    if _gmail_svc is None:
        if not os.path.exists("authorized_gmail.json"):
            raise SystemExit(
                "Нет authorized_gmail.json — запусти `python3 setup_gmail.py` "
                "и авторизуйся под owner@example.com"
            )
        with open("authorized_gmail.json") as f:
            gmail_info = json.load(f)
        gmail_creds = Credentials.from_authorized_user_info(
            gmail_info, gmail_info.get("scopes")
        )
        if gmail_creds.expired and gmail_creds.refresh_token:
            gmail_creds.refresh(Request())
        _gmail_svc = build("gmail", "v1", credentials=gmail_creds)
    return _gmail_svc


def ensure_gmail_label(svc) -> str:
    """Создаёт метку GMAIL_LABEL_NAME в Gmail если нет. Возвращает label_id."""
    labels = svc.users().labels().list(userId="me").execute().get("labels", [])
    for lbl in labels:
        if lbl["name"] == GMAIL_LABEL_NAME:
            return lbl["id"]
    new_lbl = svc.users().labels().create(
        userId="me",
        body={
            "name": GMAIL_LABEL_NAME,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        },
    ).execute()
    print(f"Gmail: метка '{GMAIL_LABEL_NAME}' создана (id={new_lbl['id']})")
    return new_lbl["id"]


def get_faktury_root_id() -> str:
    """
    Возвращает id корневой папки для фактур/чеков.
    Это 'Faktury/Učtenky' (реальная структура на Drive) — задаётся через
    DRIVE_FAKTURY_FOLDER_ID в .env (id=YOUR_DRIVE_FOLDER_ID).
    """
    global _FAKTURY_FOLDER_ID
    if _FAKTURY_FOLDER_ID:
        return _FAKTURY_FOLDER_ID
    cached = _drive_cache.get("faktury_root")
    if cached:
        _FAKTURY_FOLDER_ID = cached
        return cached
    # Fallback: если id не задан, создаём по имени 'Faktury'
    _FAKTURY_FOLDER_ID = get_or_create_drive_folder("Faktury")
    _drive_cache["faktury_root"] = _FAKTURY_FOLDER_ID
    _save_drive_cache(_drive_cache)
    return _FAKTURY_FOLDER_ID


def get_drive_invoice_folder(d: date) -> str:
    """
    Возвращает id вложенной папки для фактуры/чека по дате документа:
        Faktury/Učtenky / {месяц}.{год_4цифр} / {день}.{месяц} /
    Пример: 8 мая 2026 → Faktury/Učtenky / 5.2026 / 8.5 /
    Формат "5.2026" (4 цифры года) совпадает с уже существующими папками
    4.2026 и 5.2026. Папки 1.26, 2.26, 3.26 не используются — это исторический
    формат, для архива.
    """
    root_id    = get_faktury_root_id()
    month_name = f"{d.month}.{d.year}"         # 5.2026
    month_id   = get_or_create_subfolder(root_id, month_name)
    day_name   = f"{d.day}.{d.month}"          # 8.5
    return get_or_create_subfolder(month_id, day_name)


def get_or_create_subfolder(parent_id: str, name: str) -> str:
    """Ищет вложенную папку по имени, создаёт если нет. Использует persistent-кэш."""
    cache_key = f"sub:{parent_id}/{name}"
    cached = _drive_cache.get(cache_key)
    if cached:
        return cached
    resp = drive.files().list(
        q=(f"name='{name}' and '{parent_id}' in parents "
           f"and mimeType='application/vnd.google-apps.folder' and trashed=false"),
        fields="files(id)",
        spaces="drive",
    ).execute()
    files = resp.get("files", [])
    if files:
        folder_id = files[0]["id"]
    else:
        folder = drive.files().create(
            body={"name": name, "mimeType": "application/vnd.google-apps.folder",
                  "parents": [parent_id]},
            fields="id",
        ).execute()
        folder_id = folder["id"]
        print(f"📁 Drive: создана папка '{name}' (id={folder_id})")
    _drive_cache[cache_key] = folder_id
    _save_drive_cache(_drive_cache)
    return folder_id


def _find_attachments(parts: list) -> list[tuple[str, str, str]]:
    """Рекурсивно ищет PDF и фото вложения. Возвращает [(filename, mime, attachment_id)]."""
    result = []
    for part in parts:
        fname = part.get("filename", "")
        mime  = part.get("mimeType", "")
        att_id = part.get("body", {}).get("attachmentId", "")
        if fname and att_id and (mime == "application/pdf" or mime.startswith("image/")):
            result.append((fname, mime, att_id))
        if "parts" in part:
            result.extend(_find_attachments(part["parts"]))
    return result


# ── Цвет строки в Google Sheets ───────────────────────────────

# ── Цветовая схема таблицы (из оригинала) ────────────────────
_COL_COLORS = {
    # col_index: backgroundColor
    0:  {"red": 1.0,   "green": 1.0,   "blue": 1.0  },  # A — белый
    1:  {"red": 1.0,   "green": 1.0,   "blue": 1.0  },  # B — белый (переопределяется если есть дата)
    2:  {"red": 0.949, "green": 0.949, "blue": 0.949},  # C — светло-серый (Дата)
    3:  {"red": 0.957, "green": 0.800, "blue": 0.800},  # D — бледно-розовый (Расход)
    4:  {"red": 0.851, "green": 0.914, "blue": 0.827},  # E — бледно-зелёный (Доход)
    5:  {"red": 1.0,   "green": 1.0,   "blue": 1.0  },  # F — белый
    6:  {"red": 1.0,   "green": 1.0,   "blue": 1.0  },  # G — белый
    7:  {"red": 0.949, "green": 0.949, "blue": 0.949},  # H — светло-серый (Описание)
    8:  {"red": 1.0,   "green": 1.0,   "blue": 1.0  },  # I — белый
    9:  {"red": 1.0,   "green": 1.0,   "blue": 1.0  },  # J — белый
    10: {"red": 0.937, "green": 0.937, "blue": 0.937},  # K — серый (Комментарий)
}
_DUE_RED = {"red": 1.0, "green": 0.8, "blue": 0.8}   # B — дата оплаты (неоплачено)


def apply_row_colors(row_num: int, payment_due_date: str = "") -> None:
    """
    Применяет стандартную цветовую схему к строке row_num (1-based).
    Если payment_due_date задан — красит только ячейку B красным.
    """
    sheet_id = worksheet.id
    requests = []

    for col_idx, bg in _COL_COLORS.items():
        # Колонка B: красная если есть дата, иначе белая
        if col_idx == 1:
            bg = _DUE_RED if payment_due_date else bg
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": row_num - 1,
                    "endRowIndex": row_num,
                    "startColumnIndex": col_idx,
                    "endColumnIndex": col_idx + 1,
                },
                "cell": {"userEnteredFormat": {"backgroundColor": bg}},
                "fields": "userEnteredFormat.backgroundColor",
            }
        })

    spreadsheet.batch_update({"requests": requests})


# ── Na kontrolu staging tab ────────────────────────────────────

NA_KONTROLU_TAB = "Na kontrolu"
NA_KONTROLU_HEADER = [
    "Datum platby", "Částka", "Směr", "Protistrana", "Zpráva",
    "VS", "FIO_ID", "Návrh kategorie", "Stav", "Poznámka",
]


def get_na_kontrolu_ws():
    """Вернуть worksheet 'Na kontrolu', создав с заголовком если нет."""
    sh = worksheet.spreadsheet
    try:
        return sh.worksheet(NA_KONTROLU_TAB)
    except Exception:
        ws = sh.add_worksheet(title=NA_KONTROLU_TAB, rows=200, cols=len(NA_KONTROLU_HEADER))
        ws.append_row(NA_KONTROLU_HEADER, value_input_option="USER_ENTERED")
        return ws


def append_na_kontrolu(payment: dict, navrh_kategorie: str = "") -> None:
    """Добавить платёж-сироту в Na kontrolu со статусом open."""
    ws = get_na_kontrolu_ws()
    smer = "příjem" if payment.get("signed", 0) > 0 else "výdaj"
    row = [
        payment["date"].strftime("%d.%m.%Y"),
        payment["amount"],
        smer,
        payment.get("dodavatel", ""),
        payment.get("info", "")[:200],
        payment.get("var_symbol", ""),
        payment["id"],
        navrh_kategorie,
        "open",
        "",
    ]
    ws.append_row(row, value_input_option="USER_ENTERED")


def list_na_kontrolu_open() -> list:
    """Вернуть [(row_number, cells)] для строк со Stav=open."""
    ws = get_na_kontrolu_ws()
    out = []
    for i, r in enumerate(ws.get_all_values()[1:], start=2):
        if len(r) > 8 and r[8].strip().lower() == "open":
            out.append((i, r))
    return out


def resolve_na_kontrolu(row_number: int, note: str = "") -> None:
    """Пометить строку Na kontrolu как resolved (Stav=колонка 9, Poznámka=10)."""
    ws = get_na_kontrolu_ws()
    ws.update_cell(row_number, 9, "resolved")
    if note:
        ws.update_cell(row_number, 10, note)


def find_na_kontrolu_by_fio_id(fio_id: str):
    """Найти 1-based номер OPEN-строки Na kontrolu по FIO_ID (колонка G, idx 6).
    Возвращает None если строки нет ИЛИ она уже resolved (защита от устаревших кнопок)."""
    ws = get_na_kontrolu_ws()
    for i, r in enumerate(ws.get_all_values()[1:], start=2):
        if (len(r) > 6 and r[6].strip() == fio_id
                and (len(r) <= 8 or r[8].strip().lower() == "open")):
            return i
    return None


# ---------------------------------------------------------------------------
# Sloučeno log tab + merge_into_row  (Task 3)
# ---------------------------------------------------------------------------

_COL_LETTER = {10: "K", 13: "N", 14: "O", 15: "P", 16: "Q", 18: "S", 19: "T"}

SLOUCENO_TAB = "Sloučeno"
SLOUCENO_HEADER = ["Datum", "Popis", "Zdroj nový", "Řádek P&L", "Doplněná pole", "Klíč"]


def get_slouceno_ws():
    sh = worksheet.spreadsheet
    try:
        return sh.worksheet(SLOUCENO_TAB)
    except Exception:
        ws = sh.add_worksheet(title=SLOUCENO_TAB, rows=200, cols=len(SLOUCENO_HEADER))
        ws.append_row(SLOUCENO_HEADER, value_input_option="USER_ENTERED")
        return ws


def log_slouceno(popis: str, row_number: int, missing: dict, key: str) -> None:
    ws = get_slouceno_ws()
    fields = ", ".join(sorted(_COL_LETTER.get(i, str(i)) for i in missing))
    ws.append_row(
        [datetime.now().strftime("%d.%m.%Y %H:%M"), popis, "manual/gmail",
         str(row_number), fields, key],
        value_input_option="USER_ENTERED",
    )


def merge_into_row(row_number: int, missing: dict) -> None:
    """Вписать недостающие поля {col_index(0-based): value} в существующую строку."""
    for idx0, val in missing.items():
        worksheet.update_cell(row_number, idx0 + 1, val)  # gspread 1-based


# ---------------------------------------------------------------------------
# Kontrola tab I/O  (Task 3)
# ---------------------------------------------------------------------------

KONTROLA_TAB = "Kontrola"

_KONTROLA_SECTIONS = [
    ("overdue",     "🔴 Просрочено",    lambda x: f"#{x['row']} · {x['amount']} Kč · {x['desc']} · срок {x['due']} ({x['days_over']}д просрочки)"),
    ("soon",        "🟠 Скоро срок",     lambda x: f"#{x['row']} · {x['amount']} Kč · {x['desc']} · через {x['days_left']}д ({x['due']})"),
    ("long_unpaid", "🟡 Долго висит",    lambda x: f"#{x['row']} · {x['amount']} Kč · {x['desc']} · {x['op_date']} ({x['days_old']}д)"),
    ("late_paid",   "🔵 Поздняя оплата", lambda x: f"#{x['row']} · {x['amount']} Kč · {x['desc']} · лаг {x['lag_days']}д"),
    ("na_kontrolu", "🟣 На проверке",     lambda x: f"{x['datum']} · {x['amount']} Kč · {x['who']}"),
    ("no_document", "⚪ Нет документа",   lambda x: f"#{x['row']} · {x['amount']} Kč · {x['desc']} · {x['op_date']}"),
]


def get_kontrola_ws():
    sh = worksheet.spreadsheet
    try:
        return sh.worksheet(KONTROLA_TAB)
    except Exception:
        return sh.add_worksheet(title=KONTROLA_TAB, rows=400, cols=1)


def rebuild_kontrola_tab(scan: dict, today_str: str, period: str) -> int:
    """Полностью перезаписать вкладку Kontrola из результата scan_anomalies.
    Возвращает общее число проблем."""
    ws = get_kontrola_ws()
    total = sum(len(scan.get(k, [])) for k, _, _ in _KONTROLA_SECTIONS)
    lines = [[f"Kontrola · обновлено {today_str} · период {period}"], [""]]
    for key, title, fmt in _KONTROLA_SECTIONS:
        items = scan.get(key, [])
        lines.append([f"{title} ({len(items)})"])
        for it in items[:50]:
            lines.append([f"   {fmt(it)}"])
        lines.append([""])
    lines.append([f"Итог: {'✅ чисто' if total == 0 else f'⚠️ {total} проблем'}"])
    ws.clear()
    ws.update(range_name="A1", values=lines, value_input_option="USER_ENTERED")
    return total


# ---------------------------------------------------------------------------

def color_row(row_num: int, color: str) -> None:
    """
    Красит / сбрасывает только ячейку B{row_num}.
    color: "red"   → бледно-красный (неоплаченная фактура)
           "clear" → белый (оплачено)
    row_num: 1-based
    """
    sheet_id = worksheet.id
    bg = _DUE_RED if color == "red" else {"red": 1.0, "green": 1.0, "blue": 1.0}

    spreadsheet.batch_update({
        "requests": [{
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": row_num - 1,
                    "endRowIndex": row_num,
                    "startColumnIndex": 1,
                    "endColumnIndex": 2,
                },
                "cell": {"userEnteredFormat": {"backgroundColor": bg}},
                "fields": "userEnteredFormat.backgroundColor",
            }
        }]
    })


# ── Claude AI ─────────────────────────────────────────────────

ai = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """
Ты бухгалтер бара Demo (Прага). Классифицируй операцию и верни ТОЛЬКО валидный JSON — без пояснений.

КАТЕГОРИИ РАСХОДОВ:
B      - Běžný náklad (общий)
BB     - Běžný Benz (топливо, бензин)
BCOLA  - Běžné Cola (кола, газировка)
BI     - Běžný Investiční (инвестиции в бизнес, оборудование)
BL     - Běžný Led (лёд)
BM     - Běžný Marketingový (маркетинг, реклама, Facebook, подписки)
BMAKRO - Běžné Makro (любые закупки в Макро)
BMAX   - Běžný náklad Max (топливо и личные расходы директора)
BO     - Běžné Ostatní (прочее: алкоголь не из Макро, разные товары)
BP     - Běžný Provozní (операционное: инвентарь, химия, расходники, посуда)
BPIV   - Běžné Pivo (пиво, санация пивных линий)
BPIZ   - Běžné Pizza (пицца)
BSIR   - Běžné Sirupy (сиропы: Zanzibar, Monin, пюре)
BT     - Běžný Tabák (табак, уголь для кальяна)
BV     - Běžný Variábilní — mzdy a dohody (зарплаты HPP + выплаты по DPP/DPČ, mimo DPH)
BSOC   - Sociální a zdravotní pojištění (отчисления работодателя, mimo DPH, sazba_dph=0)
DANE   - Daně (daň ze závislé činnosti и прочие налоги, mimo DPH, sazba_dph=0)
BVV    - Běžný Variábilní Vedení (зарплата руководства: Adam, Max)
FE     - Fixní Energie (электричество, Tedom)
FN     - Fixní Najem (аренда помещения)
FNB    - Fixní Najem Byt (аренда квартиры)
FO     - Fixní Odpad (вывоз мусора, FCC)
FPOS   - Fixní POS (касса, Dotykacka)
FW     - Fixní Wifi (интернет, телефон, O2)
F      - Fixní прочий (банковские сборы, комиссии карты)

КАТЕГОРИИ ДОХОДОВ:
PK     - Tržba kartou (выручка с карт, эквайринг: Global Payments, ČSOB karty, ComGate)
PH     - Tržba hotově (выручка наличными — поступления в кассу)

ИСТОЧНИК ОПЛАТЫ (source) — DEFAULT для текстового ввода = "Hotovost Demo Bistro":
"BU Demo Bistro"       - банк. Используй ТОЛЬКО если есть явный hint: фактура с převodem, аренда, крупный счёт ≥2000 Kč, или слово "переводом"/"с карты"/"банк".
"Hotovost Demo Bistro" - DEFAULT для всего остального текстового ввода (мелкие, средние расходы, личная оплата). Это твой fallback когда нет других hints.
"Hotovost Demo"     - ТОЛЬКО если в тексте явно «бармен купил», «со стойки», «с кассы бара», «Adam заплатил», или это закрытие смены. Не используй для обычного ввода Максима.

МЕСТО (where):
"Demo" - в баре | "Mail" - по почте | "Disk" - диск/архив | "Auto" - в дороге | "Web" - онлайн

ФАКТУРА (invoice):
"Faktura" - счёт-фактура | "Uctenka" - кассовый чек | "" - нет документа

ДАТА ОПЛАТЫ (payment_due_date):
Если документ — Faktura, найди поле "Datum splatnosti" (срок оплаты) и верни в формате "д.м" (пример: "19.5").
Если дата сплатности не указана или документ не является фактурой — верни "".

ПЕРИОДИЧЕСКИЕ РАСХОДЫ (зарплаты/налоги/соц) — правило даты:
Если расход относится к месяцу-периоду («zarplata za květen», «daně za 05/2026»,
«sociální za duben»), поле "date" = ПОСЛЕДНИЙ ДЕНЬ месяца-периода (ISO формат),
НЕ дата ввода и НЕ дата платежа. Пример: «зарплата за май 2026» → "date":"2026-05-31".
"payment_due_date" — реальный срок оплаты (обычно начало следующего месяца).

ПРИМЕРЫ:
- "лёд 400"                  → BL, Hotovost Demo Bistro, Uctenka, "", payment_due_date: ""
- "Adam купил лёд 400"     → BL, Hotovost Demo, Uctenka, Demo, payment_due_date: ""  (явный hint про бармена)
- "Занзибар сиропы 5724"     → BSIR, BU Demo Bistro, Faktura, Disk, payment_due_date: "19.5"  (фактура → перевод)
- "Макро 9858 наличкой"      → BMAKRO, Hotovost Demo Bistro, Uctenka, "", payment_due_date: ""  (явный hint про наличные)
- "Макро 9858"               → BMAKRO, BU Demo Bistro, Faktura, Disk, payment_due_date: ""  (большая сумма → банк)
- "зарплата Hugo 8851"      → BV, BU Demo Bistro, "", "", payment_due_date: ""
- "зарплата за май 2026"     → BV, BU Demo Bistro, "", "", date: "2026-05-31", payment_due_date: "10.6"  (периодический расход → дата = конец периода)
- "sociální za květen"       → BSOC, BU Demo Bistro, "", "", date: "2026-05-31", payment_due_date: "20.6"  (отчисления работодателя)
- "daně ze mzdy za květen"   → DANE, BU Demo Bistro, "", "", date: "2026-05-31", payment_due_date: "20.6"  (daň ze závislé činnosti)
- "аренда 36500"             → FN, BU Demo Bistro, SK, Mail, payment_due_date: "1.6"
- "электричество 8000"       → FE, BU Demo Bistro, Faktura, Mail, payment_due_date: "15.5"

Формат ответа (строго JSON):
{
  "category": "BMAKRO",
  "amount": "3458",
  "description": "Makro 5.5",
  "source": "BU Demo Bistro",
  "date": "5.5",
  "invoice": "Faktura",
  "where": "Disk",
  "payment_due_date": ""
}

─────────────────────────────────────────────────────────────
ДОПОЛНИТЕЛЬНЫЕ DPH-ПОЛЯ — только если документ Faktura/PDF и видна rekapitulace DPH
─────────────────────────────────────────────────────────────

Если перед тобой настоящая фактура (PDF, фото с печатью, видна rekapitulace DPH в виде
«Sazba | Základ daně | Daň» внизу документа), извлеки ещё эти поля и добавь к JSON:

- "duzp"          — Datum uskutečnění zdanitelného plnění, ИСО формат "YYYY-MM-DD"
                    (если в документе не указано явно → равно дате выставления)
- "dodavatel"     — полное название поставщика (firma) — например "Makro Cash & Carry ČR s.r.o."
- "dic"           — DIČ поставщика, формат "CZxxxxxxxx" (CZ + 8-10 цифр), или ""
- "var_symbol"    — variabilní symbol платежа (обычно номер фактуры), или ""
- "cislo_dokladu" — номер фактуры (Číslo faktury / Faktura č.), или ""
- "dph_lines"     — массив разбивки по ставкам DPH:
                    [{"sazba": 21, "zaklad": 1000.00, "dph": 210.00},
                     {"sazba": 12, "zaklad": 500.00, "dph": 60.00}]

Правила dph_lines:
- Если в фактуре одна ставка — массив из одного элемента
- Если §56 osvobozeno (например najem) или поставщик не plátce DPH — [{"sazba": 0, "zaklad": <total>, "dph": 0}]
- zaklad + dph должны равняться сумме по ставке (с tolerance ±1 Kč на округление)
- Никогда не выдумывай DPH. Если на документе нет rekapitulace — НЕ возвращай dph_lines, верни []

Если документ — Uctenka (краткий кассовый чек без полной rekapitulace) — duzp/dodavatel/dic
опционально (возвращай что явно видно, иначе ""). dph_lines возвращай только если на чеке
есть явный split по ставкам, иначе [].

Если документа нет (ручной текст "лёд 400") — ВСЕ DPH-поля пустые ("" и []).

Пример полного ответа для фактуры MAKRO с двумя ставками:
{
  "category": "BMAKRO",
  "amount": "3458",
  "description": "Makro 5.5 — еда + напитки",
  "source": "BU Demo Bistro",
  "date": "5.5",
  "invoice": "Faktura",
  "where": "Disk",
  "payment_due_date": "19.5",
  "duzp": "2026-05-05",
  "dodavatel": "Makro Cash & Carry ČR s.r.o.",
  "dic": "CZ26450691",
  "var_symbol": "20260505",
  "cislo_dokladu": "FV2026-05-1234",
  "dph_lines": [
    {"sazba": 21, "zaklad": 2400.00, "dph": 504.00},
    {"sazba": 12, "zaklad": 477.68, "dph": 57.32}
  ]
}
"""


def extract_json(raw: str) -> dict:
    cleaned = re.sub(r"```(?:json)?\s*", "", raw).replace("```", "").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[\s\S]*\}", cleaned)
    if match:
        return json.loads(match.group())
    return json.loads(raw)


def _to_ddmmyyyy(s) -> str:
    """Normalize any date input to DD.MM.YYYY (HARD RULE from CLAUDE.md).

    Accepts:
    - "" or None              → ""
    - "2026-05-08" (ISO)       → "08.05.2026"
    - "20.5" (Czech short, год выводится из today) → "20.05.<current year>"
    - "20.5.2026" (Czech full) → "20.05.2026"
    - "20.5.26" (двузначный год) → "20.05.2026" (21st century)
    - "20.05.2026" (уже правильно) → "20.05.2026"

    Pre-existing bug fixed: Claude returns "date":"20.5" short format;
    bot was writing it as-is into Sheets, violating DD.MM.YYYY hard rule.
    Now все date поля проходят через эту функцию в normalize_dph_fields.
    """
    if not s or not isinstance(s, str):
        return ""
    s = s.strip()
    if not s:
        return ""

    # ISO: YYYY-MM-DD (Claude returns this for DUZP)
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        y, mm, dd = m.groups()
        return f"{int(dd):02d}.{int(mm):02d}.{int(y):04d}"

    # Czech short / full: D.M, D.M.YY, D.M.YYYY
    m = re.match(r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?$", s)
    if m:
        dd, mm, y = m.groups()
        if y is None:
            y = str(date.today().year)  # year inferred from today (Claude prompt сообщает контекст)
        elif len(y) == 2:
            y = "20" + y                # 26 → 2026 (21st century assumed)
        return f"{int(dd):02d}.{int(mm):02d}.{int(y):04d}"

    print(f"[date] не смог распарсить {s!r}, оставляю как есть")
    return s


def normalize_dph_fields(d: dict) -> dict:
    """Convert Claude's JSON output to bot-friendly flat fields.

    Все date поля нормализуются в DD.MM.YYYY (HARD RULE):
    - "date", "payment_due_date", "duzp", "datum_uhrady" — proходят через _to_ddmmyyyy

    DPH fields:
    - "dph_lines"[0]: flattened to sazba_dph / zaklad_dph / dph for single-rate
    - dodavatel / dic / var_symbol / cislo_dokladu: passed through

    Multi-rate handling (Step 3-ext, TODO):
    Currently if dph_lines has >1 entry, writes only first rate to single row.
    Future: split into N rows tied by cislo_dokladu.
    """
    # Normalize all date fields to DD.MM.YYYY
    for date_field in ("date", "payment_due_date", "duzp", "datum_uhrady"):
        if date_field in d:
            d[date_field] = _to_ddmmyyyy(d.get(date_field, ""))

    # Flatten dph_lines[0] into flat fields for build_row
    lines = d.get("dph_lines") or []
    if isinstance(lines, list) and lines:
        if len(lines) > 1:
            total_base = sum(line.get("zaklad", 0) for line in lines)
            total_dph = sum(line.get("dph", 0) for line in lines)
            rates = [line.get("sazba") for line in lines]
            print(f"⚠️ multi-rate фактура ({len(lines)} ставок: {rates}) — "
                  f"current bot пишет ОДНУ строку (TODO split). "
                  f"base_total={total_base} dph_total={total_dph}")
        first = lines[0]
        d.setdefault("sazba_dph", first.get("sazba", ""))
        d.setdefault("zaklad_dph", first.get("zaklad", ""))
        d.setdefault("dph", first.get("dph", ""))
    return d


async def ask_claude_text(text: str) -> dict:
    today = date.today().strftime("%-d.%-m")
    resp = ai.messages.create(
        model="claude-opus-4-5",
        max_tokens=800,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Сегодня {today}. Расход: {text}"}],
    )
    raw = resp.content[0].text
    print(f"[Claude text] {raw[:200]}")
    return normalize_dph_fields(extract_json(raw))


async def ask_claude_file(file_b64: str, media_type: str = "image/jpeg") -> dict:
    today       = date.today().strftime("%-d.%-m")
    prompt_text = (
        f"Сегодня {today}. Это чек или фактура чешского бара. "
        "Внимательно прочитай: поставщик, IČO/DIČ, сумма (Celkem k úhradě / CELKEM), "
        "дата выставления, DUZP (Datum uskutečnění zdanitelného plnění), "
        "номер фактуры, variabilní symbol, дата оплаты (Datum splatnosti), "
        "и rekapitulace DPH (Sazba/Základ/Daň). "
        "Верни ТОЛЬКО JSON без пояснений."
    )

    if media_type == "application/pdf":
        file_block = {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": file_b64},
        }
    else:
        file_block = {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": file_b64},
        }

    resp = ai.messages.create(
        model="claude-opus-4-5",
        max_tokens=1200,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": [file_block, {"type": "text", "text": prompt_text}]}],
    )
    raw = resp.content[0].text
    print(f"[Claude file/{media_type}] {raw[:300]}")
    return normalize_dph_fields(extract_json(raw))


# ── Вспомогательные функции ───────────────────────────────────

def parse_date_str(s: str) -> date | None:
    """
    Конвертирует строку даты в объект date.
    Поддерживаемые форматы: "5.5", "05.05", "5.5.2026", "05.05.2026"
    Префикс ✅ игнорируется.
    """
    if not s:
        return None
    s = s.strip().lstrip("✅").strip()
    if not s:
        return None

    # д.м.гггг
    m = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$", s)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None

    # д.м (предполагаем текущий год)
    m = re.match(r"^(\d{1,2})\.(\d{1,2})$", s)
    if m:
        try:
            return date(date.today().year, int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None

    return None


def normalize_amount(s: str) -> str:
    cleaned = re.sub(r"[^\d.]", "", s.replace(",", ".").replace("\xa0", ""))
    try:
        return str(round(float(cleaned)))
    except ValueError:
        return cleaned


def normalize_date(s: str) -> str:
    s = s.strip()
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return f"{int(m.group(3))}.{int(m.group(2))}"
    m = re.match(r"(\d{1,2})\.(\d{1,2})", s)
    if m:
        return f"{int(m.group(1))}.{int(m.group(2))}"
    return s


def desc_tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-zа-яёA-ZА-ЯЁ0-9]{3,}", s.lower()))


def token_similarity(a: str, b: str) -> int:
    return len(desc_tokens(a) & desc_tokens(b))


def find_duplicates(data: dict) -> list[str]:
    """Smart дедупликация. Использует var_symbol/číslo dokladu как definitive key,
    а amount/date — только как weak heuristic fallback.

    Логика:
    1. Если у новой записи есть VS — ищем точное совпадение по VS:
       - совпало → DEFINITIVE DUPLICATE (warning + кнопка «всё равно добавить»)
       - не совпало → различные транзакции, не предупреждаем (даже если amount совпал!)
    2. Если у новой нет VS, проверяем číslo dokladu аналогично.
    3. Если ни VS ни číslo — fallback на amount+date heuristic (с допуском что
       совпадение может быть случайным).
    4. Special case: новая имеет VS/číslo, старая пустая + amount совпал → подсказка
       «возможно та же транзакция, обнови старую row вместо создания новой».
    """
    warnings: list[str] = []
    try:
        rows = worksheet.get_all_values()[1:]
    except Exception:
        return []

    new_amount   = normalize_amount(data.get("amount", ""))
    new_date     = normalize_date(data.get("date", ""))
    new_desc     = data.get("description", "").strip()
    new_category = data.get("category", "").upper().strip()
    new_vs       = str(data.get("var_symbol", "")).strip()
    new_doklad   = str(data.get("cislo_dokladu", "")).strip()

    alerted_rows: set[int] = set()

    # ── Phase 1: VS / číslo dokladu match (definitive) ──
    if new_vs or new_doklad:
        for idx, row in enumerate(rows[-300:]):
            if len(row) < 20:
                continue
            row_vs     = row[18].strip() if len(row) > 18 else ""
            row_doklad = row[19].strip() if len(row) > 19 else ""

            # Exact VS match — same invoice
            if new_vs and row_vs and row_vs == new_vs:
                warnings.append(
                    f"🆔 *VS={new_vs}* уже есть в P&L\n"
                    f"   → «{row[7].strip()}» / {row[3].strip()} Kč ({row[2].strip()})\n"
                    f"   ⚠️ Это **та же фактура** — пропусти, обнови старую если нужно."
                )
                alerted_rows.add(idx)
                if len(warnings) >= 3:
                    return warnings

            # Exact číslo dokladu match
            elif new_doklad and row_doklad and row_doklad == new_doklad:
                warnings.append(
                    f"📄 *Číslo dokladu {new_doklad}* уже есть в P&L\n"
                    f"   → «{row[7].strip()}» / {row[3].strip()} Kč ({row[2].strip()})\n"
                    f"   ⚠️ Это **та же фактура** — пропусти."
                )
                alerted_rows.add(idx)
                if len(warnings) >= 3:
                    return warnings

        # If we found VS/doklad match — return early, don't do weak heuristic
        if warnings:
            return warnings

    # ── Phase 2: weak heuristic — amount + date / category ──
    # Only if we DON'T have VS/číslo to discriminate, OR no row had matching VS/číslo.
    for idx, row in enumerate(rows[-300:]):
        if len(row) < 8 or idx in alerted_rows:
            continue

        row_date     = normalize_date(row[2])
        row_amount   = normalize_amount(row[3])
        row_desc     = row[7].strip()
        row_category = row[0].strip().upper()
        row_vs       = row[18].strip() if len(row) > 18 else ""

        # Special case: новая с VS, старая без — возможно та же транзакция
        # (manual cash entry → потом приходит фактура с VS)
        if (
            new_vs and not row_vs
            and new_amount and row_amount == new_amount
            and row_date == new_date
        ):
            warnings.append(
                f"🔗 *Возможный merge*: row {idx+2} имеет ту же сумму+дату но **БЕЗ VS**.\n"
                f"   → «{row_desc}» / новая VS={new_vs}\n"
                f"   ⚠️ Возможно это **та же транзакция** (manual entry + фактура).\n"
                f"   Если да — добавь VS={new_vs} в существующую row, пропусти эту."
            )
            alerted_rows.add(idx)
            if len(warnings) >= 3:
                break
            continue

        # 1. Та же сумма + тот же день (только если оба VS пусты или равны)
        if (
            new_amount and row_amount == new_amount and row_date == new_date
            and (not new_vs or not row_vs or new_vs == row_vs)
        ):
            warnings.append(
                f"💰 Та же сумма *{data['amount']} Kč* уже есть за {new_date}\n"
                f"   → «{row_desc}» / {row[5].strip() if len(row)>5 else ''}"
            )
            alerted_rows.add(idx)
            continue

        # 2. Та же сумма + категория (другая дата)
        if (
            new_amount and row_amount == new_amount
            and new_category and row_category == new_category
            and row_date != new_date
        ):
            warnings.append(
                f"🔁 Та же сумма *{data['amount']} Kč* в категории `{new_category}`\n"
                f"   → «{row_desc}» ({row[2].strip()})"
            )
            alerted_rows.add(idx)
            continue

        # 3. Похожее описание (2+ общих слова) за тот же день
        if (
            new_desc and row_desc
            and row_date == new_date
            and token_similarity(new_desc, row_desc) >= 2
        ):
            warnings.append(
                f"📝 Похожее описание за {new_date}\n"
                f"   → «{row_desc}» ({row[3].strip()} Kč)"
            )
            alerted_rows.add(idx)

        if len(warnings) >= 3:
            break

    return warnings


def find_recorded_match(data: dict) -> list | None:
    """Сверка при вводе: ищет УЖЕ записанную строку P&L по definitive ключу
    (var_symbol или číslo dokladu). Возвращает саму строку (list из ячеек A..V)
    при точном совпадении, иначе None.

    В отличие от find_duplicates (который выдаёт warning-строки для UI),
    этот хелпер отдаёт сырую строку — чтобы построить позитивную карточку
    сверки «✅ найдено, всё чётко» с реальным статусом оплаты из колонок U/V.

    Только Phase 1 (VS / číslo dokladu). Weak heuristic (amount+date) сюда
    НЕ берём — он слишком ненадёжен для утверждения «это точно та же фактура».
    """
    new_vs     = str(data.get("var_symbol", "")).strip()
    new_doklad = str(data.get("cislo_dokladu", "")).strip()
    if not new_vs and not new_doklad:
        return None

    try:
        rows = worksheet.get_all_values()[1:]
    except Exception:
        return None

    for row in rows[-300:]:
        if len(row) < 20:
            continue
        row_vs     = row[18].strip()  # S — var_symbol
        row_doklad = row[19].strip()  # T — číslo dokladu
        if new_vs and row_vs and row_vs == new_vs:
            return row
        if new_doklad and row_doklad and row_doklad == new_doklad:
            return row
    return None


def build_reconciliation_card(row: list) -> str:
    """Строит дружелюбную карточку сверки для уже записанной фактуры.

    Колонки строки (0-indexed): B(1)=срок оплаты, C(2)=дата операции,
    D(3)=расход, H(7)=описание, A(0)=категория,
    U(20)=stav_platby (zaplaceno/nezaplaceno/castecne), V(21)=datum_uhrady.

    Все даты в строке уже хранятся в формате DD.MM.YYYY — конвертация не нужна,
    просто подставляй значения как есть.
    """
    description = row[7].strip() if len(row) > 7 else "—"
    amount      = row[3].strip() if len(row) > 3 else "—"
    category    = row[0].strip() if len(row) > 0 else "—"

    # Шапка сверки — расход и документ найдены
    head = (
        f"✅ *Найдено в P&L — всё чётко*\n\n"
        f"📝 {description}\n"
        f"💰 Расход: *{amount} Kč*  ·  категория `{category}`"
    )

    # TODO(human): построй строку статуса оплаты `status_line` из колонок строки.
    # Прочитай stav_platby из row[20] (колонка U) и реши:
    #   • "zaplaceno"    → "✅ Оплачено {row[21]}"  (V — фактическая дата оплаты;
    #                       если V пустой — просто "✅ Оплачено")
    #   • "nezaplaceno"  → "🗓 Срок оплаты: {row[1]}, ещё НЕ оплачено"  (B — splatnost;
    #                       если B пустой — "🗓 Срок оплаты не указан, ещё НЕ оплачено")
    #   • "castecne"     → "🟡 Оплачено частично"
    #   • что-то ещё / пусто → "❔ Статус оплаты неизвестен"
    # Аккуратно проверяй len(row), чтобы не словить IndexError на коротких pre-DPH строках.
    stav = row[20].strip().lower() if len(row) > 20 else ""
    if stav == "zaplaceno":
        paid = row[21].strip() if len(row) > 21 else ""
        status_line = f"✅ Оплачено {paid}".strip()
    elif stav == "nezaplaceno":
        due = row[1].strip() if len(row) > 1 else ""
        status_line = (
            f"🗓 Срок оплаты: {due}, ещё НЕ оплачено" if due
            else "🗓 Срок оплаты не указан, ещё НЕ оплачено"
        )
    elif stav == "castecne":
        status_line = "🟡 Оплачено частично"
    else:
        status_line = "❔ Статус оплаты неизвестен"

    return f"{head}\n{status_line}"


def confirm_keyboard(has_duplicates: bool = False) -> InlineKeyboardMarkup:
    add_label = "⚠️ Всё равно добавить" if has_duplicates else "✅ Добавить"
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(add_label,   callback_data="confirm"),
        InlineKeyboardButton("❌ Отмена", callback_data="cancel"),
    ]])


def format_preview(d: dict, warnings: list[str] | None = None) -> str:
    drive_line = f"\n🔗 [Файл на Drive]({d['drive_url']})" if d.get("drive_url") else ""
    due_line   = f"\n🗓 Срок оплаты: *{d['payment_due_date']}*" if d.get("payment_due_date") else ""
    warn_block = ""
    if warnings:
        warn_block = f"\n\n⚠️ *Возможный дубль:*\n" + "\n".join(warnings)
    return (
        f"📋 *Проверь запись:*\n\n"
        f"📂 Категория: `{d['category']}`\n"
        f"💰 Сумма:     `{d['amount']} Kč`\n"
        f"📝 Описание:  {d['description']}\n"
        f"💳 Источник:  {d['source']}\n"
        f"📅 Дата:      {d['date']}"
        f"{due_line}\n"
        f"🧾 Фактура:   {d.get('invoice') or '—'}\n"
        f"📍 Место:     {d.get('where') or '—'}"
        f"{drive_line}"
        f"{warn_block}"
    )


def _find_waiting_orphan(data: dict):
    """Найти open-строку Na kontrolu, совпадающую с вносимой фактурой по сумме +
    (поставщик ИЛИ дата ±3 дня). Возвращает (row_number, cells) или None."""
    try:
        amount = str(normalize_amount(data.get("amount", "")))
        ddate = parse_date_str(data.get("date", ""))
        for rn, cells in list_na_kontrolu_open():
            if len(cells) < 2 or cells[1].strip() != amount:
                continue
            nk_date = parse_date_str(cells[0]) if len(cells) > 0 else None
            close = bool(ddate and nk_date and abs((ddate - nk_date).days) <= 3)
            dod_ok = bool(data.get("dodavatel") and len(cells) > 3 and cells[3].strip()
                          and data["dodavatel"].strip().lower() == cells[3].strip().lower())
            if close or dod_ok:
                return (rn, cells)
    except Exception as e:
        print(f"_find_waiting_orphan error: {e}")
    return None


async def send_entry_preview(msg, context, data: dict) -> None:
    """Показывает превью перед записью.

    Сначала ищет ТОЧНЫЙ матч (VS / číslo dokladu) среди уже записанных строк.
    Если нашёлся — фактура/чек уже в P&L → показываем позитивную карточку
    сверки «✅ Найдено — всё чётко» со статусом оплаты, но кнопку «всё равно
    добавить» оставляем (вдруг это правда новая запись с тем же VS).
    Если точного матча нет — обычное превью с weak-дублями.
    """
    context.user_data["pending"] = data
    nk_match = _find_waiting_orphan(data)
    if nk_match:
        data["_nk_row"] = nk_match[0]
        data["stav_platby"] = "zaplaceno"
        if len(nk_match[1]) > 0:
            data["datum_uhrady"] = nk_match[1][0].strip()
    import pnl_dedup
    try:
        rows = [(i, r) for i, r in enumerate(worksheet.get_all_values()[1:], start=2)]
    except Exception:
        rows = []
    decision = pnl_dedup.classify_entry(data, rows)

    if decision["kind"] == "strong_dup":
        matched_cells = next((c for (n, c) in rows if n == decision["row_number"]), None)
        if matched_cells:
            await msg.edit_text(
                build_reconciliation_card(matched_cells)
                + "\n\n_Если это всё-таки новая запись — добавь принудительно._",
                parse_mode="Markdown",
                reply_markup=confirm_keyboard(has_duplicates=True),
                disable_web_page_preview=True,
            )
            return

    if decision["kind"] == "weak_enrich":
        try:
            merge_into_row(decision["row_number"], decision["missing"])
            key = data.get("var_symbol") or data.get("cislo_dokladu") or ""
            log_slouceno(data.get("description", "")[:40], decision["row_number"],
                         decision["missing"], key)
        except Exception as e:
            print(f"merge error: {e}")
        context.user_data.pop("pending", None)
        await msg.edit_text(
            f"🔗 Объединено с уже записанной строкой (P&L #{decision['row_number']}). "
            f"Дописал недостающие поля, дубль не создан.",
            parse_mode="Markdown",
        )
        return

    if decision["kind"] == "weak_no_enrich":
        try:
            append_na_kontrolu({
                "date": parse_date_str(data.get("date", "")) or date.today(),
                "amount": str(normalize_amount(data.get("amount", ""))),
                "signed": -1.0, "dodavatel": data.get("dodavatel", ""),
                "info": data.get("description", ""), "var_symbol": "",
                "id": f"weak_{data.get('amount','')}_{data.get('date','')}",
            }, navrh_kategorie=data.get("category", ""))
        except Exception as e:
            print(f"park weak_no_enrich error: {e}")
        context.user_data.pop("pending", None)
        await msg.edit_text(
            "🟠 Похоже на повтор без новых данных — отправил на проверку (/kontrola), "
            "в P&L не добавил.",
        )
        return

    # clean → обычное превью с кнопками
    warnings = find_duplicates(data)
    await msg.edit_text(
        format_preview(data, warnings),
        parse_mode="Markdown",
        reply_markup=confirm_keyboard(has_duplicates=bool(warnings)),
        disable_web_page_preview=True,
    )


def find_insert_index(new_date_str: str, all_rows: list[list]) -> int:
    """Вычисляет 1-based номер строки, КУДА вставить новую запись, чтобы лист
    оставался отсортированным по возрастанию даты операции (колонка C, index 2).

    Аргументы:
      new_date_str — дата новой записи в формате DD.MM.YYYY (data["date"]).
      all_rows     — результат worksheet.get_all_values() (включая строку
                     заголовков в all_rows[0]).

    Возвращает:
      Номер строки для worksheet.insert_row(values, index=<это число>).
      Если новая дата — самая поздняя (или дату распарсить не удалось),
      возвращает len(all_rows) + 1 → это эквивалент append_row в самый низ.

    Семантика порядка:
      Вставляем ПЕРЕД первой существующей строкой с данными, у которой дата в
      колонке C строго больше новой. Строки с той же датой остаются выше новой
      (стабильный порядок — новое за старым в пределах дня). Строки с пустой/
      непарсящейся датой в C НЕ должны двигать точку вставки (пропускаем их).
    """
    new_date = parse_date_str(new_date_str)
    if new_date is None:
        return len(all_rows) + 1

    # TODO(human): пройди по строкам с данными (all_rows[1:], т.е. начиная со
    # строки 2 на листе) и найди первую, чья дата в колонке C (row[2]) строго
    # больше new_date — верни её 1-based номер строки на листе.
    #
    # Подсказки:
    #   • Используй enumerate(all_rows[1:], start=2) — start=2 даёт тебе сразу
    #     номер строки на листе (строка 1 = заголовки).
    #   • Дату парси через parse_date_str(row[2]) — он вернёт date или None.
    #   • Если parse_date_str вернул None (пустая ячейка / мусор) — пропусти строку
    #     (continue), такие строки не двигают точку вставки.
    #   • Первая строка с row_date > new_date → return её номер (вставим перед ней).
    #   • Если ни одна не больше → return len(all_rows) + 1 (новая дата самая поздняя).
    #   • Осторожно с короткими строками: len(row) > 2 перед обращением к row[2].
    for i, row in enumerate(all_rows[1:], start=2):
        if len(row) <= 2:
            continue
        row_date = parse_date_str(row[2])
        if row_date is None:
            continue
        if row_date > new_date:
            return i          # вставляем ПЕРЕД этой строкой
    return len(all_rows) + 1  # новая дата самая поздняя → в конец


def insert_row_sorted(row_data: list, date_str: str, due_date: str = "") -> int:
    """Записывает строку в P&L на позицию по дате операции (date_str, DD.MM.YYYY),
    чтобы лист оставался отсортированным по возрастанию даты.

    Если дата самая поздняя — обычный append в конец. Иначе insert_row в
    вычисленную позицию (строки ниже сдвигаются вместе со своим форматированием).
    Возвращает номер строки, куда реально записалось (для apply_row_colors / логов).
    """
    all_rows   = worksheet.get_all_values()
    insert_idx = find_insert_index(date_str, all_rows)
    if insert_idx > len(all_rows):
        worksheet.append_row(row_data, value_input_option="USER_ENTERED")
    else:
        worksheet.insert_row(row_data, index=insert_idx, value_input_option="USER_ENTERED")
    try:
        apply_row_colors(insert_idx, due_date)
    except Exception as e:
        print(f"apply_row_colors error: {e}")
    return insert_idx


def build_row(d: dict) -> list:
    """Собирает 22-колоночную строку для P&L.

    Структура:
      A-L: management view (категория, даты, суммы, описание, drive url, комментарий)
      M-V: DPH detail (DUZP, sazba, base, dph, dodavatel, DIČ, var.symbol,
           číslo dokladu, stav platby, datum úhrady)

    Все DPH поля optional — если Claude не извлёк их (например для bank txns
    из FIO без сопровождающей фактуры), пишутся как пустые ячейки. В этом
    случае строка совместима с pre-DPH таблицей.
    """
    is_income = d.get("_is_income", False)
    expense   = "" if is_income else d["amount"]
    income    = d["amount"] if is_income else ""
    who       = d.get("_who", "Max")

    # stav_platby: если есть due date — неоплачено, иначе — оплачено
    # (callable может override через d["stav_platby"])
    default_stav = "nezaplaceno" if d.get("payment_due_date") else "zaplaceno"
    stav_platby  = d.get("stav_platby", default_stav)

    # datum_úhrady: для уже оплаченного (cash, bank) дефолт = дата операции
    default_uhrady = d["date"] if stav_platby == "zaplaceno" else ""
    datum_uhrady   = d.get("datum_uhrady", default_uhrady)

    return [
        # ── A-L: management view (12 cols) ────────────────────────
        d["category"],                    # A — Квалификация
        d.get("payment_due_date", ""),    # B — Срок оплаты (datum splatnosti)
        d["date"],                        # C — Дата операции
        expense,                          # D — Сумма расхода (gross, s DPH)
        income,                           # E — Сумма дохода (gross)
        d["source"],                      # F — Источник
        who,                              # G — Кто записал
        d["description"],                 # H — Описание
        d.get("invoice", ""),             # I — Фактура (legacy — дублирует T)
        d.get("where", ""),               # J — Место
        d.get("drive_url", ""),           # K — Ссылка Drive
        d.get("comment", ""),             # L — Комментарий
        # ── M-V: DPH detail (10 cols) ─────────────────────────────
        d.get("duzp", ""),                # M — Datum uskutečnění zdanitelného plnění
        d.get("sazba_dph", ""),           # N — 21 / 12 / 0
        d.get("zaklad_dph", ""),          # O — Základ DPH (bez DPH)
        d.get("dph", ""),                 # P — DPH в Kč
        d.get("dodavatel", ""),           # Q — Имя поставщика
        d.get("dic", ""),                 # R — DIČ (CZxxxxxxxx)
        d.get("var_symbol", ""),          # S — Variabilní symbol для FIO match
        d.get("cislo_dokladu", ""),       # T — Номер фактуры
        stav_platby,                      # U — zaplaceno / nezaplaceno / castecne
        datum_uhrady,                     # V — Фактическая дата оплаты
    ]


def make_filename(d: dict, suffix: str = ".jpg") -> str:
    slug = re.sub(r"[^\w]", "_", d.get("description", "file"))[:30]
    return f"{d.get('date', 'unknown')}_{d.get('category', 'X')}_{slug}{suffix}"


# ── FIO Banka API ─────────────────────────────────────────────

FIO_API_BASE = "https://fioapi.fio.cz/v1/rest"


def _parse_fio_date(s: str) -> date | None:
    """FIO формат: '2026-05-08+0200' → date."""
    if not s:
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


async def fetch_fio_transactions() -> list[dict]:
    """
    Загружает транзакции из FIO Banka за последние 30 дней.
    Использует /periods/ вместо /last/ — работает без предварительной инициализации.
    """
    if not FIO_TOKEN:
        return []
    date_to   = date.today().strftime("%Y-%m-%d")
    date_from = (date.today() - timedelta(days=30)).strftime("%Y-%m-%d")
    url = f"{FIO_API_BASE}/periods/{FIO_TOKEN}/{date_from}/{date_to}/transactions.json"
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            resp = await client.get(url)
            if resp.status_code == 409:
                # Rate limit FIO: можно делать запрос не чаще 1 раза в 30 сек
                print("FIO API: rate limit (409) — попробуй позже")
                return []
            resp.raise_for_status()
            data = resp.json()
            return (
                data.get("accountStatement", {})
                    .get("transactionList", {})
                    .get("transaction") or []
            )
        except httpx.HTTPStatusError as e:
            print(f"FIO API HTTP error {e.response.status_code}: {e}")
            return []
        except Exception as e:
            print(f"FIO API error: {e}")
            return []


ORPHAN_DIGEST_THRESHOLD = 5


async def _notify_orphans(orphans: list, app, notify_chat_id) -> None:
    """Push по каждой сироте с 3 кнопками; если их >порога — один дайджест."""
    if not orphans or not notify_chat_id or not app:
        return
    if len(orphans) > ORPHAN_DIGEST_THRESHOLD:
        total = 0
        for o in orphans:
            try:
                total += int(o["amount"])
            except (ValueError, TypeError):
                pass
        await app.bot.send_message(
            chat_id=notify_chat_id,
            text=(f"🟠 *{len(orphans)} платежей на проверку* (сумма ~{total} Kč)\n"
                  f"Открой список: /kontrola"),
            parse_mode="Markdown",
        )
        return
    for o in orphans:
        smer = "приход" if o.get("signed", 0) > 0 else "расход"
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ В P&L",          callback_data=f"nk_pnl:{o['id']}"),
            InlineKeyboardButton("📎 Пришлю фактуру", callback_data=f"nk_wait:{o['id']}"),
            InlineKeyboardButton("🗑 Отклонить",      callback_data=f"nk_no:{o['id']}"),
        ]])
        try:
            await app.bot.send_message(
                chat_id=notify_chat_id,
                text=(f"🟠 Банк, {smer} *{o['amount']} Kč* → {o.get('dodavatel') or '—'} "
                      f"({o['date'].strftime('%d.%m.%Y')}). В P&L нет соответствия."),
                parse_mode="Markdown", reply_markup=kb,
            )
        except Exception as e:
            print(f"[_notify_orphans] error: {e}")


async def check_fio_and_update(app=None) -> int:
    """
    Двухфазная проверка FIO Banka:
      Фаза 1: отметить оплаченные фактуры (B → '✅ 19.5', цвет → белый)
      Фаза 2: импортировать неизвестные транзакции как новые строки
              (используя имя контрагента + сообщение из банка, классифицированы Claude)
    Возвращает: общее число изменений (отмеченных + добавленных).
    """
    if not FIO_TOKEN:
        return 0

    transactions = await fetch_fio_transactions()
    if not transactions:
        return 0

    imported_ids = _load_fio_imported()

    # Парсим транзакции в удобный формат
    payments: list[dict] = []
    for txn in transactions:
        col1  = txn.get("column1")  or {}   # Objem (сумма со знаком)
        col0  = txn.get("column0")  or {}   # Datum
        col22 = txn.get("column22") or {}   # ID pohybu (уникальный FIO ID)
        col10 = txn.get("column10") or {}   # Název protiúčtu (имя контрагента)
        col16 = txn.get("column16") or {}   # Zpráva pro příjemce
        col5  = txn.get("column5")  or {}   # VS — Variabilní symbol (для FIO match)
        col7  = txn.get("column7")  or {}   # Uživatelská identifikace
        val   = col1.get("value")
        if val is None:
            continue
        signed   = float(val)
        amount   = str(round(abs(signed)))
        txn_date = _parse_fio_date(col0.get("value", ""))
        if not txn_date:
            continue
        txn_id = str(col22.get("value", "")) or f"{txn_date}_{amount}_{col10.get('value','')}"
        # Combine all message sources into info string for Claude context
        info   = " ".join([
            str(col10.get("value", "")),
            str(col16.get("value", "")),
            str(col7.get("value", "")),
        ]).strip()
        # Variabilní symbol — for FIO matching (Phase 1) + record in S column
        var_symbol = str(col5.get("value", "") or "").strip()
        # Counterparty name — useful for Q (dodavatel) when filled
        dodavatel = str(col10.get("value", "") or "").strip()
        payments.append({
            "id": txn_id, "amount": amount, "date": txn_date,
            "info": info, "signed": signed,
            "var_symbol": var_symbol,
            "dodavatel": dodavatel,
        })

    if not payments:
        return 0

    try:
        all_rows = worksheet.get_all_values()
    except Exception:
        return 0

    notify_chat_id = load_notify_chat_id()
    paid_count     = 0

    # rows как (row_number, cells) для fio_match
    indexed_rows = [(i, r) for i, r in enumerate(all_rows[1:], start=2)]

    orphans: list[dict] = []
    for p in payments:
        if p["id"] in imported_ids:
            continue
        result = fio_match.match_payment(p, indexed_rows)

        if result["kind"] == "paid":
            v_date = p["date"].strftime("%d.%m.%Y")
            for rn in result["row_numbers"]:
                row_cells = all_rows[rn - 1]
                col_b = row_cells[1].strip() if len(row_cells) > 1 else ""
                b_val = col_b if col_b.startswith("✅") else f"✅ {col_b}".strip()
                try:
                    worksheet.update_cell(rn, 2, b_val)         # B = ✅ срок
                    worksheet.update_cell(rn, 21, "zaplaceno")  # U
                    worksheet.update_cell(rn, 22, v_date)       # V
                    color_row(rn, "clear")
                except Exception as e:
                    print(f"[FIO verify] write error row {rn}: {e}")
                paid_count += 1
                # пометить строку оплаченной в памяти, чтобы второй платёж
                # того же прохода не сматчился к ней повторно
                cells = all_rows[rn - 1]
                while len(cells) <= 20:
                    cells.append("")
                cells[20] = "zaplaceno"
            imported_ids.add(p["id"])
            if notify_chat_id and app:
                first = all_rows[result["row_numbers"][0] - 1]
                desc = first[7].strip() if len(first) > 7 else "—"
                try:
                    await app.bot.send_message(
                        chat_id=notify_chat_id,
                        text=(f"✅ *Фактура оплачена!*\n📝 {desc}\n"
                              f"💰 {p['amount']} Kč · {v_date}"),
                        parse_mode="Markdown",
                    )
                except Exception as e:
                    print(f"[FIO verify] notify error: {e}")
        else:
            # ambiguous или orphan → парк в Na kontrolu, НЕ в P&L
            try:
                append_na_kontrolu(p)
            except Exception as e:
                print(f"[FIO verify] append_na_kontrolu error: {e}")
            imported_ids.add(p["id"])
            orphans.append(p)

    _save_fio_imported(imported_ids)
    await _notify_orphans(orphans, app, notify_chat_id)
    print(f"FIO verify: {paid_count} paid, {len(orphans)} parked to Na kontrolu")
    return paid_count + len(orphans)


# ── Фоновые задачи (JobQueue) ─────────────────────────────────

async def check_due_invoices_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Ежедневная задача в 9:00 UTC (11:00 Prague CEST).
    Уведомляет о фактурах, срок которых сегодня или через 3 дня.
    """
    notify_chat_id = load_notify_chat_id()
    if not notify_chat_id:
        print("check_due_invoices_job: NOTIFY_CHAT_ID не задан — пропуск")
        return

    today = date.today()
    soon  = today + timedelta(days=3)

    try:
        all_rows = worksheet.get_all_values()
    except Exception as e:
        print(f"check_due_invoices_job sheet error: {e}")
        return

    for row in all_rows[1:]:
        if len(row) < 4:
            continue
        col_b = row[1].strip()
        if not col_b or col_b.startswith("✅"):
            continue

        # Раз FIO на паузе, B не всегда штампуется ✅ — проверяем и колонку U.
        stav = row[20].strip().lower() if len(row) > 20 else ""
        if stav == "zaplaceno":
            continue

        due_date = parse_date_str(col_b)
        if not due_date:
            continue

        description = row[7].strip() if len(row) > 7 else "—"
        amount      = row[3].strip() if len(row) > 3 else "—"

        if due_date == today:
            text = (
                f"🔴 *Сегодня последний день оплаты!*\n\n"
                f"📝 {description}\n"
                f"💰 {amount} Kč · срок: {col_b}"
            )
        elif due_date == soon:
            text = (
                f"⚠️ *Через 3 дня срок оплаты ({col_b})*\n\n"
                f"📝 {description}\n"
                f"💰 {amount} Kč"
            )
        else:
            continue

        try:
            await context.bot.send_message(
                chat_id=notify_chat_id, text=text, parse_mode="Markdown"
            )
        except Exception as e:
            print(f"Notification error: {e}")


# Go-live: нормальная работа началась с 1 июня 2026. Скан целостности
# игнорирует всё до этой даты (forward-only) — легаси-хвосты не показываем.
KONTROLA_PERIOD_START = date(2026, 6, 1)


async def _run_kontrola_scan(bot, notify_chat_id, force_summary: bool = False) -> dict:
    """Скан + перезапись вкладки Kontrola + push критичного. Возвращает scan dict."""
    import kontrola
    import calendar
    try:
        pnl_rows = [(i, r) for i, r in enumerate(worksheet.get_all_values()[1:], start=2)]
    except Exception as e:
        print(f"kontrola scan: P&L read error: {e}")
        return {}
    nk_rows = list_na_kontrolu_open()
    today = date.today()
    scan = kontrola.scan_anomalies(pnl_rows, nk_rows, today,
                                   period_start=KONTROLA_PERIOD_START)

    period = f"{today.month}.{today.year}"
    try:
        rebuild_kontrola_tab(scan, today.strftime("%d.%m.%Y"), period)
    except Exception as e:
        print(f"kontrola rebuild error: {e}")

    if not notify_chat_id or not bot:
        return scan

    n_over = len(scan.get("overdue", []))
    n_nk = len(scan.get("na_kontrolu", []))
    last_day = calendar.monthrange(today.year, today.month)[1]
    month_end = (last_day - today.day) <= 2

    if force_summary or n_over or n_nk or month_end:
        total = sum(len(v) for v in scan.values())
        header = "🧾 *Месяц закрывается!*\n" if (month_end and not force_summary) else "📊 *Kontrola*\n"
        text = (
            f"{header}"
            f"🔴 Просрочено: {n_over}\n"
            f"🟠 Скоро срок: {len(scan.get('soon', []))}\n"
            f"🟡 Долго висит: {len(scan.get('long_unpaid', []))}\n"
            f"🔵 Поздняя оплата: {len(scan.get('late_paid', []))}\n"
            f"🟣 На проверке: {n_nk}\n"
            f"⚪ Нет документа: {len(scan.get('no_document', []))}\n"
            f"Итог: {'✅ чисто' if total == 0 else f'⚠️ {total} проблем'} · детали во вкладке Kontrola"
        )
        try:
            await bot.send_message(chat_id=notify_chat_id, text=text, parse_mode="Markdown")
        except Exception as e:
            print(f"kontrola notify error: {e}")
    return scan


async def kontrola_scan_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ежедневно: пересобрать вкладку Kontrola, push если критично/конец месяца."""
    notify_chat_id = load_notify_chat_id()
    await _run_kontrola_scan(context.bot, notify_chat_id, force_summary=False)


async def cmd_audit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Пересобрать Kontrola и прислать полную сводку по запросу."""
    msg = await update.message.reply_text("📊 Считаю...")
    notify_chat_id = str(update.effective_chat.id)
    await _run_kontrola_scan(context.bot, notify_chat_id, force_summary=True)
    await msg.edit_text("✅ Готово — сводка выше, детали во вкладке Kontrola.")


async def check_gmail_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Ежедневная задача в 12:00 UTC (14:00 Praha CEST).
    Сканирует Gmail на фактуры → Drive → Telegram подтверждение.
    """
    notify_chat_id = load_notify_chat_id()
    if not notify_chat_id:
        print("check_gmail_job: NOTIFY_CHAT_ID не задан — пропуск")
        return

    try:
        svc      = get_gmail_service()
        label_id = ensure_gmail_label(svc)
    except Exception as e:
        print(f"check_gmail_job: Gmail init error: {e}")
        return

    # Поиск: есть вложение + тема содержит ключевые слова + метка НЕ стоит
    # Исключаем не-фактуры: objednávka (заказ), proforma, potvrzení (подтверждение), návrh (черновик)
    # Это предотвращает phantom rows из писем-заказов до приезда настоящей фактуры.
    subj_part = " OR ".join(f"subject:{s}" for s in GMAIL_SUBJECTS)
    exclude_subjects = [
        "objednávka", "objednavka",
        "proforma",
        "potvrzení", "potvrzeni",
        "návrh", "navrh",
        "nabídka", "nabidka",
        "purchase order",
    ]
    exclude_part = " ".join(f"-subject:{s}" for s in exclude_subjects)
    query = f"has:attachment ({subj_part}) {exclude_part} -label:{GMAIL_LABEL_NAME}"

    try:
        result   = svc.users().messages().list(userId="me", q=query, maxResults=30).execute()
        messages = result.get("messages", [])
    except Exception as e:
        print(f"check_gmail_job: Gmail search error: {e}")
        return

    if not messages:
        print("check_gmail_job: новых фактур нет")
        return

    print(f"check_gmail_job: найдено писем: {len(messages)}")

    for msg_ref in messages:
        msg_id = msg_ref["id"]
        try:
            msg     = svc.users().messages().get(userId="me", id=msg_id, format="full").execute()
            headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
            subject = headers.get("Subject", "—")[:60]
            sender  = headers.get("From", "—")[:60]

            # Дата письма → "8.5"
            raw_date = headers.get("Date", "")
            parsed   = parsedate(raw_date)
            if parsed:
                email_day = date(parsed[0], parsed[1], parsed[2])
            else:
                email_day = date.today()
            date_str = f"{email_day.day}.{email_day.month}"

            # Ищем вложения
            attachments = _find_attachments(msg["payload"].get("parts", []))
            if not attachments:
                # Нет PDF/фото — просто помечаем
                svc.users().messages().modify(
                    userId="me", id=msg_id,
                    body={"addLabelIds": [label_id]},
                ).execute()
                continue

            date_folder_id = get_drive_invoice_folder(email_day)

            for idx, (fname, mime, att_id) in enumerate(attachments):
                try:
                    # Скачиваем вложение
                    att   = svc.users().messages().attachments().get(
                        userId="me", messageId=msg_id, id=att_id
                    ).execute()
                    raw   = base64.urlsafe_b64decode(att["data"])

                    # Сохраняем raw во temp, при необходимости конвертируем HEIC→JPEG
                    in_suffix = ".pdf" if mime == "application/pdf" else ".jpg"
                    tmp = tempfile.NamedTemporaryFile(suffix=in_suffix, delete=False)
                    tmp.write(raw)
                    tmp.close()
                    send_path, send_mime = ensure_claude_compatible(tmp.name, mime)

                    # Drive + Claude получают уже совместимый формат
                    suffix  = ".pdf" if send_mime == "application/pdf" else ".jpg"
                    drv_name = f"{date_str}_{fname}" if fname.endswith(suffix) else f"{date_str}_{fname}{suffix}"
                    drive_url = upload_to_drive(send_path, drv_name, send_mime, folder_id=date_folder_id)

                    with open(send_path, "rb") as _f:
                        file_b64 = base64.standard_b64encode(_f.read()).decode()
                    data_dict = await ask_claude_file(file_b64, send_mime)
                    data_dict["drive_url"] = drive_url

                    # Чистим temp-файлы (оригинал + конвертированный если был)
                    try:
                        os.unlink(tmp.name)
                    except OSError:
                        pass
                    if send_path != tmp.name:
                        try:
                            os.unlink(send_path)
                        except OSError:
                            pass

                    # Сохраняем в bot_data для подтверждения
                    pkey = f"gm_{msg_id[:10]}_{idx}"
                    context.bot_data[pkey] = data_dict

                    # Telegram: превью + кнопки
                    warnings = find_duplicates(data_dict)
                    await context.bot.send_message(
                        chat_id=notify_chat_id,
                        text=(
                            f"📧 *Фактура из Gmail*\n"
                            f"От: `{sender}`\n"
                            f"Тема: `{subject}`\n"
                            f"Файл: `{fname}`\n\n"
                        ) + format_preview(data_dict, warnings),
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("✅ Добавить",    callback_data=f"gm_ok:{pkey}"),
                            InlineKeyboardButton("❌ Пропустить", callback_data=f"gm_no:{pkey}"),
                        ]]),
                        disable_web_page_preview=True,
                    )

                except Exception as e:
                    print(f"check_gmail_job: ошибка вложения '{fname}': {e}")

            # Помечаем письмо как обработанное
            svc.users().messages().modify(
                userId="me", id=msg_id,
                body={"addLabelIds": [label_id]},
            ).execute()

        except Exception as e:
            print(f"check_gmail_job: ошибка письма {msg_id}: {e}")


async def fio_check_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Периодическая задача каждые 6 часов: проверить FIO Banka."""
    await check_fio_and_update(app=context.application)


# ── Команды ───────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    save_notify_chat_id(chat_id)
    await update.message.reply_text(
        f"👋 *Бот Demo активирован!*\n\n"
        f"🔔 Уведомления будут приходить сюда.\n\n"
        f"📋 *Команды:*\n"
        f"  /check — проверить FIO Banka сейчас\n"
        f"  /gmail — сканировать Gmail сейчас\n"
        f"  /due — неоплаченные фактуры\n"
        f"  /stats — сводка за месяц\n"
        f"  /find <слово> — поиск\n"
        f"  /undo — удалить последнюю запись\n"
        f"  /help — все команды\n\n"
        f"Chat ID: `{chat_id}`",
        parse_mode="Markdown",
    )


async def cmd_gmail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ручной запуск Gmail-скана (обычно в 12:00 UTC автоматом)."""
    msg = await update.message.reply_text("📧 Сканирую Gmail...")
    try:
        await check_gmail_job(context)
        await msg.edit_text("✅ Gmail-скан завершён (см. сообщения выше)")
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка Gmail: `{e}`", parse_mode="Markdown")


async def cmd_due(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Список неоплаченных фактур, отсортированных по сроку."""
    try:
        all_rows = worksheet.get_all_values()
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка чтения таблицы: {e}")
        return

    today = date.today()
    items: list[tuple[date, str, str, str]] = []  # (due_date, b_str, amount, desc)

    seen_doklady = {}   # T -> index in items
    for row in all_rows[1:]:
        if len(row) < 4:
            continue
        stav = row[20].strip().lower() if len(row) > 20 else ""
        if stav == "zaplaceno":
            continue
        col_b = row[1].strip()
        if not col_b or col_b.startswith("✅"):
            continue
        d = parse_date_str(col_b)
        if not d:
            continue
        amount = row[3].strip() if len(row) > 3 else "—"
        desc   = row[7].strip() if len(row) > 7 else "—"
        t = row[19].strip() if len(row) > 19 else ""
        if t and t in seen_doklady:
            idx = seen_doklady[t]
            prev = items[idx]
            new_amount = str((int(normalize_amount(prev[2]) or 0)) + (int(normalize_amount(amount) or 0)))
            items[idx] = (prev[0], prev[1], new_amount, prev[3])
            continue
        items.append((d, col_b, amount, desc))
        if t:
            seen_doklady[t] = len(items) - 1

    if not items:
        await update.message.reply_text("✅ Неоплаченных фактур нет")
        return

    items.sort(key=lambda x: x[0])

    lines = [f"📋 *Неоплаченные фактуры:* ({len(items)})\n"]
    for d, col_b, amount, desc in items[:30]:
        days = (d - today).days
        if days < 0:
            marker = f"🔴 ПРОСРОЧКА {-days}д"
        elif days == 0:
            marker = "🔴 СЕГОДНЯ"
        elif days <= 3:
            marker = f"⚠️ через {days}д"
        else:
            marker = f"через {days}д"
        lines.append(f"`{col_b}` · *{amount} Kč* · {desc[:40]}\n   _{marker}_")

    if len(items) > 30:
        lines.append(f"\n_…и ещё {len(items)-30}_")

    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True
    )


async def cmd_kontrola(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Список open-строк Na kontrolu (fallback к push)."""
    rows = list_na_kontrolu_open()
    if not rows:
        await update.message.reply_text("✅ На проверке ничего нет.")
        return
    lines = [f"🟣 *На проверке:* ({len(rows)})\n"]
    for _, c in rows[:30]:
        datum = c[0] if len(c) > 0 else "?"
        castka = c[1] if len(c) > 1 else "?"
        who = (c[3] if len(c) > 3 and c[3].strip() else (c[4][:30] if len(c) > 4 else "—"))
        stav = c[9] if len(c) > 9 and c[9].strip() else "open"
        lines.append(f"`{datum}` · *{castka} Kč* · {who} · _{stav}_")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Сводка за текущий месяц: расходы и доходы по категориям."""
    try:
        all_rows = worksheet.get_all_values()
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")
        return

    today = date.today()
    expenses: dict[str, float] = {}
    incomes: dict[str, float]  = {}
    total_exp = 0.0
    total_inc = 0.0
    count     = 0

    for row in all_rows[1:]:
        if len(row) < 4:
            continue
        d = parse_date_str(row[2].strip()) if len(row) > 2 else None
        if not d or d.month != today.month or d.year != today.year:
            continue
        cat   = row[0].strip().upper() or "—"
        amt_d = row[3].strip() if len(row) > 3 else ""
        amt_e = row[4].strip() if len(row) > 4 else ""
        try:
            if amt_d:
                v = float(normalize_amount(amt_d))
                expenses[cat] = expenses.get(cat, 0) + v
                total_exp += v
                count += 1
            if amt_e:
                v = float(normalize_amount(amt_e))
                incomes[cat] = incomes.get(cat, 0) + v
                total_inc += v
                count += 1
        except ValueError:
            pass

    if count == 0:
        await update.message.reply_text(f"📊 За {today.month}.{today.year} записей нет")
        return

    lines = [f"📊 *Статистика за {today.month}.{today.year % 100}*\n"]
    if expenses:
        lines.append("📤 *Расходы:*")
        for cat, v in sorted(expenses.items(), key=lambda x: -x[1]):
            lines.append(f"  `{cat:6}` {v:>10,.0f} Kč".replace(",", " "))
        lines.append(f"  *Итого:* {total_exp:,.0f} Kč".replace(",", " "))
    if incomes:
        lines.append("\n📥 *Доходы:*")
        for cat, v in sorted(incomes.items(), key=lambda x: -x[1]):
            lines.append(f"  `{cat:6}` {v:>10,.0f} Kč".replace(",", " "))
        lines.append(f"  *Итого:* {total_inc:,.0f} Kč".replace(",", " "))
    if expenses and incomes:
        balance = total_inc - total_exp
        lines.append(f"\n💼 *Баланс:* {balance:+,.0f} Kč".replace(",", " "))
    lines.append(f"\n_Записей: {count}_")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_undo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Откатить последнюю запись (с подтверждением)."""
    try:
        all_rows = worksheet.get_all_values()
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")
        return

    if len(all_rows) <= 1:
        await update.message.reply_text("ℹ️ Таблица пуста")
        return

    last_row     = all_rows[-1]
    last_row_num = len(all_rows)
    cat   = last_row[0] if len(last_row) > 0 else "—"
    amt_d = last_row[3] if len(last_row) > 3 else ""
    amt_e = last_row[4] if len(last_row) > 4 else ""
    desc  = last_row[7] if len(last_row) > 7 else "—"
    amount = amt_d or amt_e or "—"

    context.user_data["undo_row_num"] = last_row_num
    await update.message.reply_text(
        f"⚠️ *Удалить последнюю строку?*\n\n"
        f"Строка {last_row_num}\n"
        f"📂 `{cat}` · {amount} Kč\n"
        f"📝 {desc}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🗑 Удалить", callback_data="undo_yes"),
            InlineKeyboardButton("❌ Отмена",  callback_data="undo_no"),
        ]]),
    )


async def cmd_find(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Поиск по описанию (колонка H). Использование: /find занзибар"""
    if not context.args:
        await update.message.reply_text(
            "Использование: `/find <слово>`\nПример: `/find занзибар`",
            parse_mode="Markdown",
        )
        return

    q = " ".join(context.args).lower().strip()
    try:
        all_rows = worksheet.get_all_values()
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")
        return

    matches = []
    for i, row in enumerate(all_rows[1:], start=2):
        desc = row[7].lower() if len(row) > 7 else ""
        cat  = row[0].lower() if len(row) > 0 else ""
        if q in desc or q in cat:
            matches.append((i, row))

    if not matches:
        await update.message.reply_text(f"🔍 Ничего не найдено: `{q}`", parse_mode="Markdown")
        return

    lines = [f"🔍 *Найдено: {len(matches)}* по `{q}`\n"]
    for i, row in matches[-15:]:  # последние 15
        cat   = row[0] if len(row) > 0 else "—"
        date_ = row[2] if len(row) > 2 else "—"
        amt_d = row[3] if len(row) > 3 else ""
        amt_e = row[4] if len(row) > 4 else ""
        desc  = row[7] if len(row) > 7 else "—"
        amount = amt_d or amt_e or "—"
        lines.append(f"_стр.{i}_ · `{cat}` · {date_} · *{amount} Kč*\n   {desc[:50]}")

    if len(matches) > 15:
        lines.append(f"\n_…показаны последние 15 из {len(matches)}_")

    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Список всех команд."""
    await update.message.reply_text(
        "📋 *Команды бота Demo:*\n\n"
        "🚀 `/start` — активировать уведомления\n"
        "💳 `/check` — проверить FIO Banka сейчас\n"
        "📧 `/gmail` — сканировать Gmail сейчас\n"
        "📅 `/due` — неоплаченные фактуры\n"
        "📊 `/stats` — сводка за месяц\n"
        "🔍 `/find <слово>` — поиск по таблице\n"
        "🗑 `/undo` — удалить последнюю запись\n"
        "ℹ️ `/help` — это сообщение\n\n"
        "*Также можно:*\n"
        "• Текст: `расход 1200 занзибар сиропы`\n"
        "• Фото чека\n"
        "• PDF-фактуру",
        parse_mode="Markdown",
    )


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ручная проверка FIO Banka."""
    if not FIO_TOKEN:
        await update.message.reply_text(
            "⚠️ FIO\\_TOKEN не настроен в .env\n"
            "Добавь: `FIO_TOKEN=твой_токен_из_fio_banka`",
            parse_mode="Markdown",
        )
        return
    msg   = await update.message.reply_text("🔄 Проверяю FIO Banka...")
    count = await check_fio_and_update(app=context.application)
    if count > 0:
        await msg.edit_text(
            f"✅ Изменений в таблице: *{count}*\n"
            f"(см. уведомления выше — оплаты + авто-импорт)",
            parse_mode="Markdown",
        )
    else:
        await msg.edit_text("ℹ️ Новых транзакций для обработки нет")


# ── Обработчики Telegram ──────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Принимает свободную форму. Сlaude разбирает что есть.

    Старый формат `расход 1200 занзибар` тоже работает — это просто
    одна из форм. Free-form text типа «купил у Zanzibaru лёд за 400»
    тоже распознаётся.
    """
    text = update.message.text.strip()

    # Игнорируем явные не-payment сообщения (приветствия, благодарности и т.п.)
    if len(text) < 4:
        return  # слишком короткое — не реагируем
    if re.match(r"^(привет|здравств|спасибо|hi|hello|ok|ок)\b", text, re.IGNORECASE):
        await update.message.reply_text(
            "👋 Готов записывать расходы. Просто напиши свободно: «лёд 400» "
            "или «купил у Zanzibaru сиропы за 5к» — я разберу.",
        )
        return

    # Старая форма «расход X описание» — нормализуем (убираем префикс)
    body = re.sub(r"^расход\s*", "", text, flags=re.IGNORECASE).strip()
    msg  = await update.message.reply_text("🤔 Классифицирую...")

    try:
        data = await ask_claude_text(body)
    except json.JSONDecodeError:
        await msg.edit_text("❌ Claude вернул некорректный ответ. Попробуй ещё раз.")
        return
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}")
        return

    # Safety: если Claude не нашёл amount или category — не показываем preview
    amount = str(data.get("amount", "")).strip()
    category = str(data.get("category", "")).strip()
    if not amount or amount in ("0", "0.0") or not category:
        await msg.edit_text(
            "🤷 Я не понял что записать. Напиши с суммой:\n"
            "Пример: «лёд 400» или «5к на пиво у Kousek»",
        )
        return

    await send_entry_preview(msg, context, data)


# Типы, которые Claude vision принимает напрямую. Остальные image/* (в т.ч.
# image/heic с iPhone, отправленный «как файл») конвертируем в JPEG.
_CLAUDE_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


def ensure_claude_compatible(file_path: str, mime_type: str) -> tuple[str, str]:
    """Гарантирует формат, понятный Claude vision.

    PDF и поддерживаемые картинки → возвращаются как есть.
    HEIC/HEIF и любые другие image/* → конвертируются в JPEG (новый temp-файл,
    его обязан удалить вызывающий). Возвращает (path, media_type).
    При ошибке конвертации возвращает оригинал (бот не падает — Claude вернёт
    ошибку, которую обработает _process_file).
    """
    if mime_type == "application/pdf" or mime_type in _CLAUDE_IMAGE_TYPES:
        return file_path, mime_type
    try:
        from PIL import Image
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except Exception:
            pass  # не-HEIC форматы Pillow откроет и без плагина
        img = Image.open(file_path).convert("RGB")
        out = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        out.close()
        img.save(out.name, "JPEG", quality=90)
        print(f"[convert] {mime_type} → image/jpeg ({file_path} → {out.name})")
        return out.name, "image/jpeg"
    except Exception as e:
        print(f"[convert] не удалось сконвертировать {mime_type}: {e}")
        return file_path, mime_type


async def _process_file(file_path: str, mime_type: str, msg, context) -> None:
    """Общая логика для фото и документов: Claude + Drive."""
    # HEIC/HEIF и прочие нестандартные форматы → JPEG (и для Claude, и для Drive).
    orig_path = file_path
    file_path, mime_type = ensure_claude_compatible(file_path, mime_type)
    _converted = file_path != orig_path

    suffix = ".pdf" if mime_type == "application/pdf" else (".png" if "png" in mime_type else ".jpg")

    try:
        with open(file_path, "rb") as f:
            file_b64 = base64.standard_b64encode(f.read()).decode()

        try:
            data = await ask_claude_file(file_b64, mime_type)
        except (json.JSONDecodeError, ValueError):
            await msg.edit_text(
                "❌ Не удалось разобрать ответ Claude.\n"
                "Попробуй переслать фото как *документ* (без сжатия) "
                "или введи расход вручную: расход 5400 Kousek piva",
                parse_mode="Markdown",
            )
            return
        except Exception as e:
            await msg.edit_text(f"❌ Ошибка: `{e}`", parse_mode="Markdown")
            return

        await msg.edit_text("☁️ Загружаю в Google Drive...")
        try:
            filename  = make_filename(data, suffix)
            # Папка по дате документа: Demo Faktury / 5.26 / 8.5 /
            parsed_d  = parse_date_str(data.get("date", ""))
            if parsed_d:
                folder_id = get_drive_invoice_folder(parsed_d)
            else:
                folder_id = get_faktury_root_id()   # fallback: корень Demo Faktury
            drive_url = upload_to_drive(file_path, filename, mime_type, folder_id=folder_id)
            data["drive_url"] = drive_url
        except Exception as e:
            data["drive_url"] = ""
            print(f"Drive upload error: {e}")

        await send_entry_preview(msg, context, data)
    finally:
        # Удаляем temp-файл конвертации (оригинал чистит вызывающий хендлер)
        if _converted:
            try:
                os.unlink(file_path)
            except OSError:
                pass


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg  = await update.message.reply_text("🔍 Распознаю чек...")
    file = await update.message.photo[-1].get_file()
    tmp  = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    try:
        await file.download_to_drive(tmp.name)
        await _process_file(tmp.name, "image/jpeg", msg, context)
    finally:
        os.unlink(tmp.name)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    doc  = update.message.document
    mime = doc.mime_type or ""

    if mime == "application/pdf":
        suffix = ".pdf"
    elif mime.startswith("image/"):
        suffix = ".png" if "png" in mime else ".jpg"
    else:
        await update.message.reply_text("📎 Поддерживаются: фото, jpg/png, PDF.")
        return

    label = "📄 Читаю PDF-фактуру..." if mime == "application/pdf" else "🔍 Распознаю документ..."
    msg   = await update.message.reply_text(label)
    file  = await doc.get_file()
    tmp   = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        await file.download_to_drive(tmp.name)
        await _process_file(tmp.name, mime, msg, context)
    finally:
        os.unlink(tmp.name)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.data == "confirm":
        data = context.user_data.pop("pending", None)
        if not data:
            await query.edit_message_text("⚠️ Нет данных для сохранения.")
            return

        row_data = build_row(data)
        print(f"[insert_row_sorted] {len(row_data)} колонок: {row_data}")
        new_row_num = insert_row_sorted(
            row_data, data.get("date", ""), data.get("payment_due_date", "")
        )
        print(f"[insert_row_sorted] записано в строку {new_row_num}")

        nk_row = data.get("_nk_row")
        if nk_row:
            try:
                resolve_na_kontrolu(nk_row, "закрыто присланной фактурой")
            except Exception as e:
                print(f"resolve_na_kontrolu error: {e}")

        drive_note = " · 📎 файл сохранён"               if data.get("drive_url")        else ""
        due_note   = f" · 🗓 оплата до {data['payment_due_date']}" if data.get("payment_due_date") else ""
        await query.edit_message_text(
            f"✅ Сохранено: *{data['amount']} Kč* — {data['description']}{drive_note}{due_note}",
            parse_mode="Markdown",
        )

    elif query.data == "cancel":
        context.user_data.pop("pending", None)
        await query.edit_message_text("❌ Отменено")

    elif query.data.startswith("gm_ok:"):
        pkey = query.data[6:]
        data = context.bot_data.pop(pkey, None)
        if not data:
            await query.edit_message_text("⚠️ Данные не найдены (возможно бот перезапускался).")
            return
        row_data = build_row(data)
        print(f"[gmail insert_row_sorted] {len(row_data)} колонок: {row_data}")
        insert_row_sorted(
            row_data, data.get("date", ""), data.get("payment_due_date", "")
        )
        drive_note = " · 📎 Drive" if data.get("drive_url") else ""
        due_note   = f" · 🗓 до {data['payment_due_date']}" if data.get("payment_due_date") else ""
        await query.edit_message_text(
            f"✅ Сохранено: *{data['amount']} Kč* — {data['description']}{drive_note}{due_note}",
            parse_mode="Markdown",
        )

    elif query.data.startswith("gm_no:"):
        pkey = query.data[6:]
        context.bot_data.pop(pkey, None)
        await query.edit_message_text("⏭ Пропущено")

    elif query.data == "undo_yes":
        row_num = context.user_data.pop("undo_row_num", None)
        if not row_num:
            await query.edit_message_text("⚠️ Нечего удалять")
            return
        try:
            worksheet.delete_rows(row_num)
            await query.edit_message_text(f"🗑 Строка {row_num} удалена")
        except Exception as e:
            await query.edit_message_text(f"❌ Ошибка удаления: `{e}`", parse_mode="Markdown")

    elif query.data == "undo_no":
        context.user_data.pop("undo_row_num", None)
        await query.edit_message_text("❌ Отменено")

    elif query.data.startswith("nk_no:"):
        fio_id = query.data[len("nk_no:"):]
        rn = find_na_kontrolu_by_fio_id(fio_id)
        if rn:
            resolve_na_kontrolu(rn, "отклонено вручную")
        await query.edit_message_text("🗑 Отклонено, помечено resolved.")

    elif query.data.startswith("nk_wait:"):
        fio_id = query.data[len("nk_wait:"):]
        rn = find_na_kontrolu_by_fio_id(fio_id)
        if rn:
            get_na_kontrolu_ws().update_cell(rn, 10, "čeká na fakturu")
        await query.edit_message_text(
            "📎 Жду фактуру. Пришли её сюда — свяжу с этим платежом.")

    elif query.data.startswith("nk_pnl:"):
        fio_id = query.data[len("nk_pnl:"):]
        rn = find_na_kontrolu_by_fio_id(fio_id)
        if not rn:
            await query.edit_message_text("⚠️ Строка уже обработана.")
            return
        nk = get_na_kontrolu_ws().get_all_values()[rn - 1]
        # nk columns: [Datum,Částka,Směr,Protistrana,Zpráva,VS,FIO_ID,Návrh,Stav,Pozn]
        data = {
            "category": (nk[7].strip() if len(nk) > 7 and nk[7].strip() else "BO"),
            "amount": nk[1].strip() if len(nk) > 1 else "",
            "date": nk[0].strip() if len(nk) > 0 else "",
            "description": (nk[3].strip() if len(nk) > 3 and nk[3].strip()
                            else (nk[4][:40].strip() if len(nk) > 4 else "FIO платёж")),
            "source": "BU Demo Bistro",
            "var_symbol": nk[5].strip() if len(nk) > 5 else "",
            "stav_platby": "zaplaceno",
            "datum_uhrady": nk[0].strip() if len(nk) > 0 else "",
            "payment_due_date": "",
            "_is_income": (len(nk) > 2 and nk[2].strip().lower() == "příjem"),
            "_who": "Bank",
        }
        row_data = build_row(data)
        insert_row_sorted(row_data, data["date"], "")
        resolve_na_kontrolu(rn, "внесено в P&L")
        await query.edit_message_text(f"✅ Внесено в P&L: {data['amount']} Kč.")


# ── Запуск ────────────────────────────────────────────────────

async def set_bot_commands(app) -> None:
    """Регистрирует выпадающее меню команд (список по «/» в Telegram)."""
    await app.bot.set_my_commands([
        BotCommand("check",    "Проверить FIO Banka сейчас"),
        BotCommand("due",      "Неоплаченные фактуры по сроку"),
        BotCommand("kontrola", "Платежи на проверке (Na kontrolu)"),
        BotCommand("audit",    "Сводка целостности (Kontrola)"),
        BotCommand("stats",    "Сводка за месяц"),
        BotCommand("gmail",    "Сканировать Gmail сейчас"),
        BotCommand("find",     "Поиск по описанию/категории"),
        BotCommand("undo",     "Удалить последнюю запись"),
        BotCommand("help",     "Список команд"),
        BotCommand("start",    "Активировать уведомления"),
    ])


def main() -> None:
    # PicklePersistence: bot_data, user_data, chat_data сохраняются на диск
    # → пендинг Gmail-фактуры с кнопками ✅/❌ переживают перезапуск
    persistence = PicklePersistence(filepath="bot_state.pkl")
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .persistence(persistence)
        .post_init(set_bot_commands)
        .build()
    )

    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("gmail", cmd_gmail))
    app.add_handler(CommandHandler("due",      cmd_due))
    app.add_handler(CommandHandler("kontrola", cmd_kontrola))
    app.add_handler(CommandHandler("audit",    cmd_audit))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("find",  cmd_find))
    app.add_handler(CommandHandler("undo",  cmd_undo))

    # Сообщения
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Фоновые задачи
    jq = app.job_queue
    if jq is None:
        print("⚠️ JobQueue недоступен — установи: pip install 'python-telegram-bot[job-queue]'")
    else:
        # Ежедневно в 9:00 UTC (= 11:00 Praha CEST) — проверка сроков оплаты
        jq.run_daily(check_due_invoices_job, time=dt_time(9, 0, tzinfo=timezone.utc))
        print("📅 Ежедневная проверка дат оплаты: 09:00 UTC")

        # Ежедневно в 9:30 UTC — сканирование целостности (Kontrola tab)
        jq.run_daily(kontrola_scan_job, time=dt_time(9, 30, tzinfo=timezone.utc))
        print("🧾 Kontrola integrity scan: 09:30 UTC")

        # Ежедневно в 12:00 UTC (= 14:00 Praha CEST) — Gmail фактуры
        jq.run_daily(check_gmail_job, time=dt_time(12, 0, tzinfo=timezone.utc))
        print("📧 Gmail мониторинг фактур: 12:00 UTC")

        # FIO автопроверка управляется флагом FIO_AUTOCHECK (default "1").
        # Поставить FIO_AUTOCHECK=0 в .env чтобы поставить на паузу (логика
        # FIO-сверки в доработке). Ручная команда /check продолжает работать.
        _fio_auto = os.getenv("FIO_AUTOCHECK", "1") == "1"
        if FIO_TOKEN and _fio_auto:
            # Первая проверка через 60 секунд после старта, затем каждые 6 часов
            jq.run_repeating(fio_check_job, interval=6 * 3600, first=60)
            print("💳 FIO Banka автопроверка: каждые 6 часов")
        elif FIO_TOKEN and not _fio_auto:
            print("⏸️  FIO автопроверка на паузе (FIO_AUTOCHECK=0)")
        else:
            print("ℹ️  FIO_TOKEN не задан — автопроверка FIO отключена")

    notify = load_notify_chat_id()
    if notify:
        print(f"🔔 Уведомления: chat_id={notify}")
    else:
        print("⚠️  NOTIFY_CHAT_ID не задан — отправь /start боту чтобы активировать уведомления")

    print("Бот запущен. Ctrl+C для остановки.")
    app.run_polling()


if __name__ == "__main__":
    main()
