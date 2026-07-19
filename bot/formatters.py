"""Форматирование сообщений для Telegram (HTML parse mode)."""
from __future__ import annotations

from datetime import datetime, timezone
from html import escape

from analysis.arbitrage import Opportunity
from sources.market_csgo import market_url


def _age_str(created_at: str) -> str:
    """Человекочитаемая «свежесть» листинга по времени выставления."""
    if not created_at:
        return ""
    try:
        ts = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return ""
    delta = datetime.now(timezone.utc) - ts
    secs = int(delta.total_seconds())
    if secs < 0:
        secs = 0
    if secs < 60:
        return f"{secs} сек назад"
    if secs < 3600:
        return f"{secs // 60} мин назад"
    if secs < 86400:
        return f"{secs // 3600} ч назад"
    return f"{secs // 86400} дн назад"


def _market_stability_line(ms) -> str:
    """Основная строка стабильности — по рынку market.csgo."""
    label = {
        "stable": "🟢 стабильный рынок",
        "medium": "🟡 средняя стабильность",
        "unstable": "🔴 нестабильный рынок",
        "unknown": "⚪ нет данных",
    }.get(ms.label, "⚪ нет данных")
    parts = [f"📊 Рынок market.csgo: {label}", f"объём {ms.volume} лот."]
    if ms.spread is not None:
        parts.append(f"отклонение цены {ms.spread * 100:.0f}%")
    return " · ".join(parts)


def _stability_line(rep) -> str:
    """Второстепенная строка — история цены по продажам CSFloat."""
    label = {
        "stable": "🟢 стабильная",
        "medium": "🟡 средняя",
        "unstable": "🔴 скачет",
        "unknown": "⚪ мало данных",
    }.get(rep.label, "⚪ мало данных")
    trend = {
        "up": "растёт ↗",
        "down": "падает ↘",
        "flat": "ровный →",
        "unknown": "—",
    }.get(rep.trend, "—")
    if rep.label == "unknown":
        return f"📈 История CSFloat: {label} (продаж: {rep.sample})"
    return (
        f"📈 История CSFloat: {label} (разброс {rep.cv * 100:.0f}%) · тренд {trend}"
    )


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

    lines = [f"🎯 <b>{prefix}{name}</b>"]

    # Бейдж «топ по флоату» из базы CSFloat
    if o.is_rank_find and o.float_rank:
        lines.append(
            f"🏅 <b>ТОП #{o.float_rank} по {escape(o.rank_kind)} флоату</b> "
            f"(редкий предмет из базы CSFloat!)"
        )

    lines.append(f"Износ: {escape(l.wear_name or '—')} | Float: <code>{float_str}</code>")
    # Ранг/сид, если известны
    rank_bits = []
    if l.float_rank:
        rank_bits.append(f"ранг по флоату #{l.float_rank} ({escape(l.rank_kind)})")
    if l.paint_seed is not None:
        rank_bits.append(f"сид {l.paint_seed}")
    if rank_bits:
        lines.append("🎲 " + " · ".join(rank_bits))

    lines += [
        "",
        f"🛒 Купить на {escape(l.source_name)}: <b>{s}{o.buy_price:.2f}</b>",
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

    # CSFloat Appraiser (оценка) — справочно, для глаза
    cr = o.csfloat_route
    if cr:
        emoji = "🟢" if cr.profit_abs > 0 else "🔴"
        lines.append(
            f"{emoji} CSFloat Appraiser (оценка): {s}{cr.gross_price:.2f} "
            f"→ на руки {s}{cr.net_price:.2f} "
            f"(<b>{cr.profit_abs:+.2f}{s} / {cr.profit_pct:+.1f}%</b>) "
            f"<i>— справочно</i>"
        )

    # Ориентир Buff163 (эталон рыночной цены, как показывает BetterFloat)
    if o.buff_start or o.buff_order:
        bits = []
        if o.buff_start:
            bits.append(f"лоты {s}{o.buff_start:.2f}")
        if o.buff_order:
            bits.append(f"ордер {s}{o.buff_order:.2f}")
        lines.append("🅱️ <b>Buff163</b> (за весь износ): " + " · ".join(bits))

    # Средняя цена скина по многим площадкам (Pricempire)
    if o.pricempire_avg:
        pe_line = f"💹 <b>Средняя цена (Pricempire):</b> {s}{o.pricempire_avg:.2f}"
        if o.pricempire_avg > 0:
            pe_pct = round(o.buy_price / o.pricempire_avg * 100.0)
            mark = "🟢" if pe_pct <= 90 else ("🟡" if pe_pct <= 100 else "🔴")
            pe_line += f" · {mark} куплено за {pe_pct}% от средней"
        lines.append(pe_line)

        # Продать на CSFloat по цене Buff (за конкретный флоат) + профит
        est = o.buff_float_estimate
        br = o.buff_route
        if est:
            float_str2 = (
                f"{l.float_value:.4f}".rstrip("0").rstrip(".")
                if l.float_value is not None else "—"
            )
            line = f"🎯 Продать на CSFloat по Buff (флоат {float_str2}): <b>{s}{est:.2f}</b>"
            if br:
                emoji = "🟢" if br.profit_abs > 0 else "🔴"
                line += (
                    f" → на руки {s}{br.net_price:.2f} "
                    f"({emoji}<b>{br.profit_abs:+.2f}{s} / {br.profit_pct:+.1f}%</b>)"
                )
            lines.append(line)
            pct = o.buff_pct
            if pct is not None:
                mark = "🟢" if pct <= 90 else ("🟡" if pct <= 100 else "🔴")
                lines.append(f"   {mark} куплено за <b>{pct:.0f}%</b> от цены Buff")

    lines += [
        "",
        f"💧 Ликвидность: <b>{o.liquidity}</b>/100",
    ]
    if o.market:
        lines.append(f"   market.csgo: объём {o.market.volume} лот(ов)")
    if l.source == "csfloat":
        lines.append(f"   CSFloat: {l.reference_quantity} листингов")
    lines.append(f"📏 Отступ флоата от границы износа: {o.float_edge_distance}")

    # Актуальность оффера: когда выставлен + сколько наблюдают
    age = _age_str(l.created_at)
    actuality = []
    if age:
        actuality.append(f"🕐 Выставлен {age}")
    actuality.append(f"👀 Наблюдают: {l.watchers}")
    lines.append(" | ".join(actuality))

    status_line = {
        "active": "✅ <b>Ещё доступен</b> (проверено только что)",
        "sold": "❌ <b>Уже куплен/снят</b>",
        "unknown": "⚠️ Статус не проверен — жми ссылку и смотри сам",
    }.get(o.live_status, "🟡 Активен на момент скана")
    lines.append(status_line)

    if o.market_stability is not None:
        lines.append(_market_stability_line(o.market_stability))
    if o.stability is not None:
        lines.append(_stability_line(o.stability))

    lines += [
        "",
        f"🔗 <a href=\"{l.url}\">Открыть на {escape(l.source_name)}</a> · "
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
