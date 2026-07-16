"""Форматирование сообщений для Telegram (HTML parse mode)."""
from __future__ import annotations

from html import escape

from analysis.arbitrage import Opportunity
from sources.market_csgo import market_url


def _wear_tag(o: Opportunity) -> str:
    tags = []
    if o.listing.is_stattrak:
        tags.append("StatTrak™")
    if o.listing.is_souvenir:
        tags.append("Souvenir")
    return (" ".join(tags) + " ") if tags else ""


def format_opportunity(o: Opportunity) -> str:
    l = o.listing
    best = o.best
    s = o.currency_symbol
    name = escape(l.market_hash_name)
    prefix = escape(_wear_tag(o))

    float_str = (
        f"{l.float_value:.6f}".rstrip("0").rstrip(".")
        if l.float_value is not None
        else "—"
    )

    lines = [
        f"🎯 <b>{prefix}{name}</b>",
        f"Износ: {escape(l.wear_name or '—')} | Float: <code>{float_str}</code>",
        "",
        f"🛒 Купить на CSFloat: <b>{s}{o.buy_price:.2f}</b>",
        "",
        "💰 <b>Куда продать (профит с учётом комиссий):</b>",
    ]

    # market.csgo — конкретная рыночная цена
    mr = o.market_route
    if mr and o.market:
        emoji = "🟢" if mr.profit_abs > 0 else "🔴"
        lines.append(
            f"{emoji} market.csgo (мин. цена): {s}{mr.gross_price:.2f} "
            f"→ на руки {s}{mr.net_price:.2f} "
            f"(<b>{mr.profit_abs:+.2f}{s} / {mr.profit_pct:+.1f}%</b>)"
        )

    # CSFloat по средней цене — справочно, для глаза
    cr = o.csfloat_route
    if cr:
        emoji = "🟢" if cr.profit_abs > 0 else "🔴"
        lines.append(
            f"{emoji} По средней цене CSFloat: {s}{cr.gross_price:.2f} "
            f"→ на руки {s}{cr.net_price:.2f} "
            f"(<b>{cr.profit_abs:+.2f}{s} / {cr.profit_pct:+.1f}%</b>) "
            f"<i>— средняя, справочно</i>"
        )

    lines += [
        "",
        f"💧 Ликвидность: <b>{o.liquidity}</b>/100",
    ]
    if o.market:
        lines.append(f"   market.csgo: объём {o.market.volume} лот(ов)")
    lines.append(f"   CSFloat: {l.reference_quantity} листингов")
    lines.append(f"📏 Отступ флоата от границы износа: {o.float_edge_distance}")

    lines += [
        "",
        f"🔗 <a href=\"{l.url}\">Открыть на CSFloat</a> · "
        f"🛒 <a href=\"{market_url(l.market_hash_name)}\">Найти на market.csgo</a>",
        f"⭐ Лучший путь: <b>{escape(best.venue)}</b> "
        f"({best.profit_abs:+.2f}{s} / {best.profit_pct:+.1f}%)",
    ]
    return "\n".join(lines)


def format_summary(opportunities: list[Opportunity], limit: int = 10) -> str:
    if not opportunities:
        return "😴 Сейчас выгодных сделок по заданным фильтрам не найдено."
    top = opportunities[:limit]
    header = f"📊 Найдено сделок: <b>{len(opportunities)}</b>. Топ {len(top)}:\n"
    rows = []
    for i, o in enumerate(top, 1):
        b = o.best
        s = o.currency_symbol
        rows.append(
            f"{i}. <b>{escape(o.listing.market_hash_name)}</b> — "
            f"куп. {s}{o.buy_price:.2f} → +{b.profit_abs:.2f}{s} "
            f"({b.profit_pct:+.1f}%, {escape(b.venue)}), "
            f"ликв. {o.liquidity}"
        )
    return header + "\n".join(rows)
