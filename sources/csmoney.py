"""Клиент CS.MONEY (экспериментальный источник покупки).

ВАЖНО: у CS.MONEY нет официального публичного API, и стоит защита от ботов
(Cloudflare). Этот клиент дёргает их внутренний эндпоинт sell-orders с
браузероподобными заголовками. Работать может нестабильно — при блокировке
просто вернёт пустой список, не ломая остальной поток бота.

Схема ответа у CS.MONEY меняется между версиями API, поэтому парсинг сделан
максимально терпимым: имя/цену/флоат ищем в нескольких возможных полях.
"""
from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import quote

import aiohttp

from sources.csfloat import CsFloatListing

logger = logging.getLogger(__name__)

# Внутренний эндпоинт витрины CS.MONEY
SELL_ORDERS_URL = "https://cs.money/2.0/market/sell-orders"

BROWSER_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://cs.money/market/buy/",
    "Origin": "https://cs.money",
}


def _dig(d: dict, *paths: tuple[str, ...]) -> Any:
    """Достаёт первое непустое значение по нескольким путям вида (a, b, c)."""
    for path in paths:
        cur: Any = d
        ok = True
        for key in path:
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            else:
                ok = False
                break
        if ok and cur not in (None, ""):
            return cur
    return None


def _parse_item(raw: dict[str, Any], usd_rate: float) -> Optional[CsFloatListing]:
    try:
        name = _dig(
            raw,
            ("asset", "names", "full"),
            ("fullName",),
            ("name",),
            ("market_hash_name",),
        )
        if not name:
            return None
        price_usd = _dig(
            raw,
            ("price",),
            ("pricing", "computed"),
            ("pricing", "default"),
            ("sellPrice",),
        )
        if price_usd is None:
            return None
        buy_price = round(float(price_usd) * usd_rate, 2)
        if buy_price <= 0:
            return None

        float_raw = _dig(raw, ("asset", "float"), ("float",), ("floatValue",))
        float_value = None
        if float_raw is not None:
            try:
                float_value = float(float_raw)
            except (TypeError, ValueError):
                float_value = None

        item_id = str(_dig(raw, ("id",), ("assetId",), ("asset", "id")) or "")
        is_stattrak = "StatTrak" in name
        is_souvenir = "Souvenir" in name

        return CsFloatListing(
            listing_id=item_id,
            market_hash_name=str(name),
            buy_price=buy_price,
            predicted_price=0.0,       # у CS.MONEY нет оценки «средней» цены CSFloat
            reference_quantity=0,      # ликвидность считаем по market.csgo
            float_value=float_value,
            wear_name="",
            is_stattrak=is_stattrak,
            is_souvenir=is_souvenir,
            source="csmoney",
            page_url=f"https://cs.money/market/buy/?search={quote(str(name))}",
        )
    except (TypeError, ValueError) as exc:
        logger.debug("Не удалось разобрать предмет CS.MONEY: %s", exc)
        return None


class CsMoneyClient:
    def __init__(self, session: aiohttp.ClientSession, usd_rate: float = 1.0) -> None:
        self._session = session
        self._usd_rate = usd_rate if usd_rate > 0 else 1.0

    async def get_listings(self, limit: int = 60) -> list[CsFloatListing]:
        params = {
            "limit": max(1, min(limit, 60)),
            "offset": 0,
            "sort": "price",
            "order": "asc",
        }
        try:
            async with self._session.get(
                SELL_ORDERS_URL,
                params=params,
                headers=BROWSER_HEADERS,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    logger.warning(
                        "CS.MONEY вернул %s (возможно, блокировка Cloudflare)",
                        resp.status,
                    )
                    return []
                payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            logger.warning("Ошибка запроса к CS.MONEY: %s", exc)
            return []

        rows = payload.get("items") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            logger.warning("Неожиданный формат ответа CS.MONEY")
            return []

        items = [_parse_item(r, self._usd_rate) for r in rows if isinstance(r, dict)]
        return [i for i in items if i is not None]
