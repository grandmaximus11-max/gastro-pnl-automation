"""Claude wrapper: classify a free-form náklad description to a valid P&L category."""
import anthropic

VALID_CATEGORIES = {
    # Expense categories (B/F-series) — used by classify_naklad
    "B", "BB", "BCOLA", "BI", "BL", "BM", "BMAKRO", "BMAX",
    "BO", "BP", "BPIV", "BPIZ", "BSIR", "BT", "BV", "BVV",
    "VRAT",  # NEW (25.05.2026): vrácení / refundy od dodavatele
    "FE", "FN", "FNB", "FO", "FPOS", "FW", "F",
    # Income categories (P-series) — bot writes directly, no classification needed
    "PK",    # Tržba kartou (POS card)
    "PH",    # Tržba hotově (POS cash)
    "PESH",  # Prodej e-shop (tabák, uhlí etc.)
}

CATEGORY_CHEATSHEET = """
B      = běžné jiné (různé drobné výdaje, nezařazené)
BB     = benzin (palivo do auta)
BCOLA  = Cola — dodavatel Cola (Coca-Cola, Sprite, Kofola, Tonic, Fanta)
BI     = investice (vybavení baru, nábytek, dlouhodobý majetek)
BL     = led (Zanzibar = dodavatel ledu; SAMOTNÝ led, ne sirupy!)
BM     = marketing (reklama, propagace, vizuály, sociální sítě, polygrafie)
BMAKRO = Makro nákup (Makro Cash&Carry — potraviny, drogerie, nápoje)
BMAX   = osobní Maks (osobní výdaje vlastníka)
BO     = běžné ostatní (NEZAŘAZENO — když si nejsi jistý)
BP     = běžné provozní (drogerie, ubrousky, papíry, čisticí prostředky)
BPIV   = pivo (sudy, lahve, dodavatelé piva: Plzeňský Prazdroj, Bernard, ...)
BPIZ   = pizza (přísady, krabice, suroviny pro pizzu)
BSIR   = sirupy (sirupy do nápojů: Zanzibar SIRUP, Frontline, Monin, J Granny SIRUP)
BT     = tabák (tabák pro shisha bar: Darkside, Tear, Kacle, J Granny TABÁK, fólie, uhlíky)
BV     = výplaty (mzdy zaměstnancům, zálohy zaměstnancům)
BVV    = vedení (výdaje za management, konzultace, účetnictví)
VRAT   = vrácení/refund (vrácené zboží dodavateli, refundy — kladná částka znamená vratku)
FE     = energie (elektřina, voda, plyn)
FN     = nájem (nájem prostor baru)
FNB    = nájem bytu (nájem zaměstnaneckého bytu)
FO     = odvoz odpadu (svoz odpadu, recyklace, kontejnery)
FPOS   = kasa/POS (Dotykačka, platební terminál, monthly fees)
FW     = internet (internet, telefonní služby)
F      = bankovní poplatky (provize z karet, bank fees, transakční poplatky)

DŮLEŽITÉ rozlišení:
- "Zanzibar" SAMOTNÝ (bez sirupu/ledu) → BL (jejich hlavní zboží je led)
- "Zanzibar sirup" / "sirup od Zanzibaru" → BSIR (přípravek na nápoje)
- "Cipa" = nezařazený dodavatel → BO
- "Frontline" = sirupy/ovocné přísady → BSIR
- "Darkside" / "Tear" / "Kacle" → BT (tabák pro vodní dýmky)
- Pokud popis obsahuje "vraceni" / "refund" / "vraceny" → VRAT
"""

_PROMPT = f"""Klasifikuj následující popis nákladu z baru Demo do JEDNÉ z následujících kategorií. Odpověz POUZE kódem kategorie (např. "BL"), nic víc.

{CATEGORY_CHEATSHEET}

Pokud si NENÍ jasné kam patří → "BO" (běžné ostatní).

Popis: """


def classify_naklad(popis: str, api_key: str) -> str:
    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=10,
        messages=[{"role": "user", "content": _PROMPT + popis}],
    )
    raw = resp.content[0].text.strip().upper()
    if raw in VALID_CATEGORIES:
        return raw
    return "BO"
