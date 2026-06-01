"""Push notifications to manager/owner via Telegram bot context."""
from telegram import Bot


def _fmt_kc(n) -> str:
    """Czech thousands separator (regular space)."""
    return f"{int(n):,}".replace(",", " ")


def format_vyplata_owner_msg(period: tuple[str, str], rows: list[dict]) -> str:
    """Owner notification: full per-person summary + 'to transfer' action block.

    `rows` = aggregated payout dicts with jmeno, prevodem, hotove, is_hpp.
    Cash payouts are paid by the manager from the envelope; transfers are what
    the owner must send by bank.
    """
    from_, to_ = period
    lines = [f"💰 Výplata {from_}–{to_} · potvrdil manažer", "━" * 14]
    total_hotove = 0
    transfers: list[tuple[str, int]] = []
    for r in rows:
        if r.get("is_hpp"):
            lines.append(f"💼 {r['jmeno']} (HPP) → měsíčně")
            continue
        prevodem = int(r.get("prevodem", 0))
        hotove = int(r.get("hotove", 0))
        if prevodem > 0 and hotove > 0:
            lines.append(f"👤 {r['jmeno']} · {_fmt_kc(prevodem + hotove)} Kč 🔄")
            lines.append(f"      💳 {_fmt_kc(prevodem)} převodem · 💵 {_fmt_kc(hotove)} hotově")
        elif prevodem > 0:
            lines.append(f"👤 {r['jmeno']} · {_fmt_kc(prevodem)} Kč 💳 převodem")
        else:
            lines.append(f"👤 {r['jmeno']} · {_fmt_kc(hotove)} Kč 💵 hotově")
        total_hotove += hotove
        if prevodem > 0:
            transfers.append((r["jmeno"], prevodem))
    lines.append("━" * 14)
    lines.append(f"💵 Hotově (z obálky): {_fmt_kc(total_hotove)} Kč")
    if transfers:
        lines.append("💳 K ODESLÁNÍ PŘEVODEM:")
        tt = 0
        for jm, amt in transfers:
            lines.append(f"   • {jm} — {_fmt_kc(amt)} Kč")
            tt += amt
        lines.append(f"   CELKEM: {_fmt_kc(tt)} Kč")
    else:
        lines.append("💳 Převodem: nic")
    return "\n".join(lines)


async def push_vyplata_owner(bot: Bot, owner_tg_id: int | None, period, rows) -> None:
    """Send the payout summary to the owner. No-op if no owner_tg_id."""
    if not owner_tg_id:
        return
    await bot.send_message(
        chat_id=owner_tg_id, text=format_vyplata_owner_msg(period, rows),
    )


async def push_chyba_alert(bot: Bot, manager_tg_id: int | None, smena: dict, chyba_castka: int) -> None:
    """Send a 'pokladna nesedí' alert to the manager. No-op if no manager_tg_id."""
    if not manager_tg_id:
        return
    msg = (
        f"⚠️ Pokladna nesedí — směna {smena['datum']}\n"
        f"Rozdíl: −{chyba_castka} Kč\n"
        f"Zodpovědný: {smena.get('zodpovedny','?')}\n"
        f"Popis: {smena.get('chyba_popis','—')}"
    )
    await bot.send_message(chat_id=manager_tg_id, text=msg)


async def push_overnight_alert(bot: Bot, manager_tg_id: int | None, smena: dict, diff: int) -> None:
    """Notify the manager that today's starting cash differs from the previous
    shift's end (overnight discrepancy). Info-level — the bartender doesn't
    resolve it, just continues. No-op if no manager_tg_id."""
    if not manager_tg_id:
        return
    sign = "+" if diff > 0 else "−"
    msg = (
        f"🌙 Overnight rozdíl pokladny — směna {smena['datum']}\n"
        f"Start dnes:    {smena.get('overnight_entered', '?')} Kč\n"
        f"Minulý konec:  {smena.get('overnight_proposed', '?')} Kč\n"
        f"Rozdíl:        {sign}{abs(diff)} Kč\n"
        f"Zodpovědný: {smena.get('zodpovedny','?')}"
    )
    await bot.send_message(chat_id=manager_tg_id, text=msg)


async def push_pokladna_diff_alert(bot: Bot, manager_tg_id: int | None, smena: dict, diff: int) -> None:
    """Notify the manager that the counted till didn't match POS by `diff`
    (POS surplus positive, but bot's count differs — e.g. a banknote missing
    from the obálka → −100). No-op if no manager_tg_id."""
    if not manager_tg_id:
        return
    kind = "chybí v obálce" if diff < 0 else "přebývá v kase"
    msg = (
        f"⚠️ Rozdíl pokladny — směna {smena['datum']}\n"
        f"Bot vs POS:  {diff:+} Kč ({kind})\n"
        f"Tržba hotově: {smena.get('trzba_pos_hot','?')} Kč\n"
        f"Obálka:       {smena.get('hot_kon_celkem','?')} Kč\n"
        f"POS spropitné: {smena.get('spropitne_hotov','?')} Kč\n"
        f"Zodpovědný: {smena.get('zodpovedny','?')}"
    )
    await bot.send_message(chat_id=manager_tg_id, text=msg)
