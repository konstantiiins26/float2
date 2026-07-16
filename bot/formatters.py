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
        f"🛒 Купить на CSFloat: <b>${o.buy_price:.2f}</b>",
        f"📈 Средняя цена CSFloat: <b>${o.csfloat_avg_price:.2f}</b> "
        f"<i>(оценка)</i>",
    ]
    if o.sales_median:
        median_line = f"📊 Медиана реальных продаж CSFloat: <b>${o.sales_median:.2f}</b>"
        if o.median_profit_abs is not None:
            emoji = "🟢" if o.median_profit_abs > 0 else "🔴"
            median_line += (
                f" {emoji} профит по средней: "
                f"<b>{o.median_profit_abs:+.2f}$ / {o.median_profit_pct:+.1f}%</b>"
            )
        lines.append(median_line)
    if o.market:
        lines.append(
            f"🟠 Цена на market.csgo: <b>${o.market.price:.2f}</b> "
            f"<i>(мин., для быстрой продажи)</i>"
        )

    lines.append("")
    for r in o.routes:
        emoji = "🟢" if r.profit_abs > 0 else "🔴"
        lines.append(
            f"{emoji} Продажа на {escape(r.venue)}: ${r.gross_price:.2f} "
            f"→ на руки ${r.net_price:.2f} "
            f"(<b>{r.profit_abs:+.2f}$ / {r.profit_pct:+.1f}%</b>)"
        )

    lines += [
        "",
        f"💧 Ликвидность: <b>{o.liquidity}</b>/100",
    ]
    if o.market:
        lines.append(
            f"   market.csgo: объём {o.market.volume} лот(ов)"
        )
    lines.append(f"   CSFloat: {l.reference_quantity} листингов")
    lines.append(f"📏 Отступ флоата от границы износа: {o.float_edge_distance}")

    links = [f"🔗 <a href=\"{l.url}\">Открыть на CSFloat</a>"]
    links.append(
        f"🛒 <a href=\"{market_url(l.market_hash_name)}\">Найти на market.csgo</a>"
    )
    lines += [
        "",
        " · ".join(links),
        f"⭐ Лучший путь: <b>{escape(best.venue)}</b> "
        f"({best.profit_abs:+.2f}$ / {best.profit_pct:+.1f}%)",
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
        rows.append(
            f"{i}. <b>{escape(o.listing.market_hash_name)}</b> — "
            f"куп. ${o.buy_price:.2f} → +{b.profit_abs:.2f}$ "
            f"({b.profit_pct:+.1f}%, {escape(b.venue)}), "
            f"ликв. {o.liquidity}"
        )
    return header + "\n".join(rows)
