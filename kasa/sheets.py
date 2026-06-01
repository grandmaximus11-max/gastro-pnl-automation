"""Thin wrapper over gspread — returns dicts, hides gspread objects."""
from __future__ import annotations

from datetime import datetime

import gspread
from google.oauth2.credentials import Credentials

from kasa.config import TAB_SMENY, TAB_VYPLATY, TAB_ZAMESTNANCI, TAB_CHYBY, TAB_PNL, DEFAULT_HOT_ZAC


def _to_float(v) -> float:
    """Parse a number that may arrive as a ru_RU-formatted string.

    The Sheets locale is ru_RU: decimals display with a COMMA ("10,5") and
    thousands with a (narrow) space. gspread.get_all_records() numericises the
    FORMATTED value, so "10,5" → 105 (comma stripped) — silently corrupting
    decimal hours. To avoid that we read Smeny with numericise disabled (all
    strings) and parse here: strip spaces/Kč, comma→dot, then float.
    """
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = (str(v).replace("\xa0", "").replace(" ", "")
         .replace("Kč", "").replace("kč", "").replace(",", "."))
    try:
        return float(s)
    except ValueError:
        return 0.0


def _to_int(v) -> int:
    """Robust int parse over the same ru_RU-formatted strings (rounds)."""
    return int(round(_to_float(v)))


class SheetsClient:
    # In-memory trackers for unknown users (no Zamestnanci row)
    _unknown_attempts: dict[int, int] = {}
    _unknown_blocks: dict[int, str] = {}

    SMENY_COLUMN_ORDER = [
        "smena_id", "datum", "typ", "pocet_lidi", "sazba_h",
        "karta", "spropitne_karta", "karta_pos",
        "hot_zac_celkem",
        "hot_kon_5000", "hot_kon_2000", "hot_kon_1000",
        "hot_kon_500", "hot_kon_200", "hot_kon_100",
        "hot_kon_mince", "hot_kon_celkem", "hot_delta",
        "trzba_pos_hot",
        "eshop_hotove", "eshop_kartou", "eshop_celkem",
        "naklady_celkem", "zalohy_neuhrazene",
        "expected_konec", "rozdil",
        "spropitne_hotov", "spropitne_celkem",
        "total_hodiny", "tip_per_hour", "trzba_bar",
        "jmeno_1", "hodiny_1", "pers_ucet_1", "zaloha_1", "plat_1", "spropitne_1", "k_vyplate_1",
        "jmeno_2", "hodiny_2", "pers_ucet_2", "zaloha_2", "plat_2", "spropitne_2", "k_vyplate_2",
        "jmeno_3", "hodiny_3", "pers_ucet_3", "zaloha_3", "plat_3", "spropitne_3", "k_vyplate_3",
        "zodpovedny", "status", "drive_folder_url", "poznamka",
        "created_at_tg", "created_by_tg",
    ]

    def __init__(self, auth_path: str, sheets_id: str) -> None:
        creds = Credentials.from_authorized_user_file(auth_path)
        gc = gspread.authorize(creds)
        self.spreadsheet = gc.open_by_key(sheets_id)

    def get_zamestnanci(self) -> list[dict]:
        """Read all rows from Zamestnanci. Empty cells appear as ''."""
        ws = self.spreadsheet.worksheet(TAB_ZAMESTNANCI)
        return ws.get_all_records()

    def get_employee_by_jmeno(self, jmeno: str) -> dict | None:
        """Look up Zamestnanci row by jmeno (used by /vyplata to detect HPP).

        Matches case-sensitively on stripped jmeno. Returns None if no match —
        which means the person typed into /uzaverka has no Zamestnanci row
        (legitimate for guest/temp workers paid as plain DPP)."""
        target = jmeno.strip()
        for r in self.get_zamestnanci():
            if str(r.get("jmeno", "")).strip() == target:
                return r
        return None

    def activate_employee(self, tg_user_id: int, tg_username: str, code: str) -> None:
        """Mark row with this code as activated by this tg_user_id, clear code."""
        ws = self.spreadsheet.worksheet(TAB_ZAMESTNANCI)
        rows = ws.get_all_records()
        for idx, row in enumerate(rows, start=2):  # +2 because header is row 1
            if (row.get("aktivacni_kod") or "").strip() == code.strip():
                now = datetime.now().strftime("%d.%m.%Y %H:%M")
                ws.batch_update([
                    {"range": f"A{idx}", "values": [[str(tg_user_id)]]},
                    {"range": f"B{idx}", "values": [[tg_username]]},
                    {"range": f"E{idx}", "values": [["TRUE"]]},        # aktivni
                    {"range": f"G{idx}", "values": [[""]]},            # aktivacni_kod cleared
                    {"range": f"H{idx}", "values": [[now]]},           # aktivovano_at
                ])
                return
        raise RuntimeError(f"Activation code not found: {code}")

    def block_user(self, tg_user_id: int, until: str) -> None:
        """Set Zamestnanci.zablokovan_do for known user, else track in-memory."""
        ws = self.spreadsheet.worksheet(TAB_ZAMESTNANCI)
        rows = ws.get_all_records()
        for idx, row in enumerate(rows, start=2):
            try:
                if int(row.get("tg_user_id") or 0) == tg_user_id:
                    ws.update(values=[[until]], range_name=f"I{idx}")
                    return
            except ValueError:
                continue
        self._unknown_blocks[tg_user_id] = until

    def failed_attempts_get(self, tg_user_id: int) -> int:
        return self._unknown_attempts.get(tg_user_id, 0)

    def failed_attempts_incr(self, tg_user_id: int) -> int:
        self._unknown_attempts[tg_user_id] = self.failed_attempts_get(tg_user_id) + 1
        return self._unknown_attempts[tg_user_id]

    def append_smena(self, smena: dict) -> None:
        """Atomically append one shift row to the Smeny tab, flattening lidi[] into per-person columns."""
        ws = self.spreadsheet.worksheet(TAB_SMENY)
        row = [smena.get(col, "") for col in self.SMENY_COLUMN_ORDER]
        # Flatten lidi list into jmeno_N/hodiny_N/... if not already
        if smena.get("lidi") and not smena.get("jmeno_1"):
            for i, p in enumerate(smena["lidi"][:3], start=1):
                for k in ("jmeno", "hodiny", "pers_ucet", "zaloha", "plat", "spropitne", "k_vyplate"):
                    col = f"{k}_{i}"
                    if col in self.SMENY_COLUMN_ORDER:
                        idx = self.SMENY_COLUMN_ORDER.index(col)
                        row[idx] = p.get(k, "")
        ws.append_row(row, value_input_option="USER_ENTERED")

    def read_last_smena(self) -> dict | None:
        """Return the last (most recent) row from Smeny tab, or None if empty."""
        ws = self.spreadsheet.worksheet(TAB_SMENY)
        rows = ws.get_all_records()
        return rows[-1] if rows else None

    # Banknote denominations used in the obálka count (matches uzaverka.NOMINALY).
    _NOMINALY = (5000, 2000, 1000, 500, 200, 100)

    def read_last_smena_carryover(self) -> dict:
        """Compute the cash that physically STAYS in the till after the most
        recent shift — i.e. the starting cash the NEXT shift should propose.

        Returns {"fond": int, "mince": int}.

        The ending fond (banknotes left for tomorrow) has no dedicated column, so
        we reconstruct it from persisted values. End-of-shift reconciliation set:
            hot_kon_celkem = fond + Σ(hot_kon_<denom> × denom) + hot_kon_mince
        therefore:
            fond = hot_kon_celkem − Σ(hot_kon_<denom> × denom) − hot_kon_mince

        Read with numericise_ignore=['all'] + _to_int so ru_RU thousands spaces
        ("5 000") don't get mis-parsed. Falls back to the 5000 default fond when
        there's no prior shift or the numbers don't reconstruct sanely (e.g. a
        legacy row saved before this formula existed)."""
        ws = self.spreadsheet.worksheet(TAB_SMENY)
        rows = ws.get_all_records(numericise_ignore=['all'])
        if not rows:
            return {"fond": DEFAULT_HOT_ZAC, "mince": 0}
        last = rows[-1]
        obalka = sum(_to_int(last.get(f"hot_kon_{n}")) * n for n in self._NOMINALY)
        mince = _to_int(last.get("hot_kon_mince"))
        celkem = _to_int(last.get("hot_kon_celkem"))
        fond = celkem - obalka - mince
        # Sanity guard: a real fond is a few thousand Kč. Garbage (negative, or
        # absurdly large from a malformed row) → fall back to the standard fond.
        if fond <= 0 or fond > 50000:
            fond = DEFAULT_HOT_ZAC
        return {"fond": fond, "mince": max(0, mince)}

    def append_naklad_to_pnl(
        self,
        *,
        datum: str,
        castka: int,
        popis: str,
        kategorie: str,
        zaplaceno_zdroj: str,
        kdo_zapsal: str,
        doklad_url: str = "",
        smena_id: str | None = None,
        komentar: str = "",
        is_income: bool = False,
        # ── DPH fields (all optional) ─────────────────────────────────
        duzp: str = "",
        sazba_dph: int | str = "",
        zaklad_dph: float | int | str = "",
        dph: float | int | str = "",
        dodavatel: str = "",
        dic: str = "",
        var_symbol: str = "",
        cislo_dokladu: str = "",
        stav_platby: str = "",
        datum_uhrady: str = "",
    ) -> None:
        """Append one row to the P&L tab (22 columns A..V).

        Headers in production:
          A=Квалификация B=Дата оплаты C=Дата D=Сумма расхода E=Сумма дохода
          F=Откуда платилось G=Кто H=Что за статья I=Фактура J=Где
          K=Drive URL L=Комментарий
          M=DUZP N=Sazba DPH O=Základ DPH P=DPH Q=Dodavatel R=DIČ
          S=Var. symbol T=Číslo dokladu U=Stav platby V=Datum úhrady

        If is_income=True, castka goes to column E instead of D.

        DPH fields default to empty. For kasa shifts (cash), the row is
        already paid → defaults set stav_platby='zaplaceno' and
        datum_uhrady=datum unless caller overrides.
        """
        ws = self.spreadsheet.worksheet(TAB_PNL)
        marker = f"[smena {smena_id}] " if smena_id else ""

        # Smart defaults for kasa flow (cash transactions are pre-paid)
        _stav = stav_platby or "zaplaceno"
        _datum_uhrady = datum_uhrady or (datum if _stav == "zaplaceno" else "")

        row = [
            # ── A-L: management view ───────────────────────────────
            kategorie,                       # A Квалификация
            "",                              # B Дата оплаты (cash → empty)
            datum,                           # C Дата
            "" if is_income else castka,     # D Сумма расхода
            castka if is_income else "",     # E Сумма дохода
            zaplaceno_zdroj,                 # F Откуда платилось
            kdo_zapsal,                      # G Кто
            f"{marker}{popis}",              # H Что за статья
            cislo_dokladu,                   # I Фактура (legacy = T)
            "",                              # J Где
            doklad_url,                      # K Drive URL
            komentar,                        # L Комментарий
            # ── M-V: DPH detail ────────────────────────────────────
            duzp,                            # M DUZP
            sazba_dph,                       # N Sazba DPH
            zaklad_dph,                      # O Základ DPH
            dph,                             # P DPH
            dodavatel,                       # Q Dodavatel
            dic,                             # R DIČ
            var_symbol,                      # S Var. symbol
            cislo_dokladu,                   # T Číslo dokladu
            _stav,                           # U Stav platby
            _datum_uhrady,                   # V Datum úhrady
        ]
        ws.append_row(row, value_input_option="USER_ENTERED")

    CHYBY_COLS = ["smena_id", "datum", "typ", "castka", "popis",
                  "notified_adam", "notified_at", "status", "resolved_by",
                  "resolved_at", "resolved_popis"]

    def append_chyba(self, *, smena_id: str, datum: str, typ: str, castka: int, popis: str) -> None:
        """Append a Chyby row. Notification timestamp set at insert; status=open."""
        ws = self.spreadsheet.worksheet(TAB_CHYBY)
        row = [smena_id, datum, typ, castka, popis,
               "TRUE", datetime.now().strftime("%d.%m.%Y %H:%M"),
               "open", "", "", ""]
        ws.append_row(row, value_input_option="USER_ENTERED")

    def read_smeny_in_range(self, from_date: str, to_date: str) -> list[dict]:
        """Return all Smeny rows where datum is between from_date and to_date (inclusive, DD.MM.YYYY).

        numericise_ignore=['all']: keep every cell as its FORMATTED string so
        gspread does NOT mangle ru_RU decimals ("10,5" → 105). Dates stay
        "DD.MM.YYYY" strings (strptime-friendly); numeric parsing is deferred to
        smena_row_to_shift_dict via _to_float/_to_int.
        """
        from datetime import datetime
        ws = self.spreadsheet.worksheet(TAB_SMENY)
        rows = ws.get_all_records(numericise_ignore=['all'])
        f = datetime.strptime(from_date, "%d.%m.%Y")
        t = datetime.strptime(to_date, "%d.%m.%Y")
        result = []
        for r in rows:
            try:
                d = datetime.strptime(str(r.get("datum", "")), "%d.%m.%Y")
                if f <= d <= t:
                    result.append(r)
            except ValueError:
                continue
        return result

    def read_chyby_in_range(self, from_date: str, to_date: str,
                            include_resolved: bool = False) -> list[dict]:
        """Return Chyby rows whose datum is in [from_date, to_date] (DD.MM.YYYY).
        Used by /prehled to flag problem days. By default SKIPS resolved rows
        (status='resolved') — they stay in the sheet for audit but don't show as
        active problems. Pass include_resolved=True to get everything."""
        from datetime import datetime
        ws = self.spreadsheet.worksheet(TAB_CHYBY)
        rows = ws.get_all_records()
        f = datetime.strptime(from_date, "%d.%m.%Y")
        t = datetime.strptime(to_date, "%d.%m.%Y")
        result = []
        for r in rows:
            if not include_resolved and str(r.get("status", "")).strip().lower() == "resolved":
                continue
            try:
                d = datetime.strptime(str(r.get("datum", "")), "%d.%m.%Y")
                if f <= d <= t:
                    result.append(r)
            except (ValueError, TypeError):
                continue
        return result

    def append_vyplata(
        self, *, vyplata_id: str, datum_vyplaty: str, period_from: str,
        period_to: str, jmeno: str, agg: dict, zpusob: str, kym: str,
        dluh_vznikly: int = 0,
        castka_prevodem: int = 0, castka_hotove: int = 0,
    ) -> None:
        """Append one row to the Vyplaty tab (18 columns).

        Phase A added: dluh_vznikly (50-Kč rounding carryover)
        Phase B added: castka_prevodem + castka_hotove (DPP split)
        `agg["k_vyplate"]` is the ROUNDED total = prevodem + hotove.
        `zpusob` is now informational summary ("mix", "hotove", "prevodem") — actual
        split is in the separate columns.
        """
        ws = self.spreadsheet.worksheet(TAB_VYPLATY)
        row = [
            vyplata_id, datum_vyplaty, period_from, period_to, jmeno,
            agg["total_hodiny"], agg["total_plat"], agg["total_spropitne"],
            agg["total_personal_ucet"], agg["total_zalohy"], agg["k_vyplate"],
            "vyplaceno", datum_vyplaty, kym, zpusob,
            dluh_vznikly,
            castka_prevodem, castka_hotove,
        ]
        ws.append_row(row, value_input_option="USER_ENTERED")

    def read_prevod_this_month(self, jmeno: str, now: datetime | None = None) -> int:
        """Sum castka_prevodem for this person within the current calendar month.
        Used to compute remaining DPP transfer headroom (12 000 − this sum)."""
        from datetime import datetime as _dt
        now = now or _dt.now()
        ws = self.spreadsheet.worksheet(TAB_VYPLATY)
        rows = ws.get_all_records()
        total = 0
        for r in rows:
            if str(r.get("jmeno", "")).strip() != jmeno:
                continue
            try:
                d = _dt.strptime(str(r.get("datum_vyplaty", "")), "%d.%m.%Y")
            except ValueError:
                continue
            if d.year == now.year and d.month == now.month:
                try:
                    total += int(r.get("castka_prevodem") or 0)
                except (ValueError, TypeError):
                    continue
        return total

    def read_last_dluh(self, jmeno: str) -> int:
        """Return dluh_vznikly from the MOST RECENT Vyplaty for this person.

        IMPORTANT: dluh_vznikly is a running BALANCE, not a delta. Each new payout
        already includes (absorbs) the previous week's dluh in its calculation, so
        only the most recent value is the carryover for next week. Summing is wrong.

        Example chain:
            W1: dluh +20  (under-paid by 20)
            W2: raw+20 included → new dluh −30  (over-paid by 30 net)
            W3: carry = −30 only (NOT +20−30=−10)
        """
        ws = self.spreadsheet.worksheet(TAB_VYPLATY)
        rows = ws.get_all_records()
        # Walk in reverse — last row matching jmeno wins
        for r in reversed(rows):
            if str(r.get("jmeno", "")).strip() == jmeno:
                try:
                    return int(r.get("dluh_vznikly") or 0)
                except (ValueError, TypeError):
                    return 0
        return 0

    def read_vyplaty_for_period(self, period_from: str, period_to: str) -> dict:
        """Return {jmeno: datum_vyplaty} for people ALREADY paid for this exact
        pay-period (period_from AND period_to match). Used by /vyplata to flag a
        week as already paid and guard against accidental double-pay. Latest
        payout wins (rows are append-only, last match overwrites)."""
        ws = self.spreadsheet.worksheet(TAB_VYPLATY)
        rows = ws.get_all_records()
        paid: dict[str, str] = {}
        for r in rows:
            if (str(r.get("period_from", "")).strip() == period_from
                    and str(r.get("period_to", "")).strip() == period_to):
                jm = str(r.get("jmeno", "")).strip()
                if jm:
                    paid[jm] = str(r.get("datum_vyplaty", "")).strip()
        return paid

    @staticmethod
    def smena_row_to_shift_dict(row: dict) -> dict:
        """Convert a flat Smeny-tab row (jmeno_1/hodiny_1/... per-person columns)
        back into the shape that kalkulace.aggregate_week expects."""
        lidi = []
        for i in (1, 2, 3):
            j = row.get(f"jmeno_{i}")
            if j and str(j).strip():
                lidi.append({
                    "jmeno": str(j).strip(),
                    "hodiny": _to_float(row.get(f"hodiny_{i}")),
                    "pers_ucet": _to_int(row.get(f"pers_ucet_{i}")),
                    "zaloha": _to_int(row.get(f"zaloha_{i}")),
                })
        return {
            "sazba_h": _to_int(row.get("sazba_h")) or 140,
            "spropitne_karta": _to_int(row.get("spropitne_karta")),
            "spropitne_hotov": _to_int(row.get("spropitne_hotov")),
            "lidi": lidi,
        }
