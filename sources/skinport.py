"""Клиент Skinport (источник покупки).

У Skinport есть публичный API каталога цен — ключ для чтения НЕ нужен:
GET https://api.skinport.com/v1/items?app_id=730&currency=EUR
Отдаёт по каждому предмету: min_price, median_price, quantity, ссылки.
Цены сразу в запрошенной валюте (конвертация не требуется).

Лимит запросов строгий (~5 запросов / 5 минут), поэтому весь каталог
кэшируем и обновляем нечасто.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

import aiohttp

from sources.csfloat import CsFloatListing

logger = logging.getLogger(__name__)

ITEMS_URL = "https://api.skinport.com/v1/items"
APP_ID_CS2 = 730
SUPPORTED = {"USD", "EUR", "GBP", "RUB", "CAD", "AUD", "CHF", "PLN", "SEK", "NOK", "TRY", "BRL", "CNY", "CZK", "DKK"}


def _parse_item(raw: dict[str, Any]) -> Optional[CsFloatListing]:
    name = raw.get("market_hash_name")
    min_price = raw.get("min_price")
    if not name or min_price in (None, 0):
        return None
    try:
        buy_price = round(float(min_price), 2)
    except (TypeError, ValueError):
        return None
    if buy_price <= 0:
        return None

    page = raw.get("market_page") or raw.get("item_page") or "https://skinport.com/market"
    return CsFloatListing(
        listing_id=str(name),
        market_hash_name=str(name),
        buy_price=buy_price,
        predicted_price=0.0,                 # продаём на market.csgo, оценка CSFloat не нужна
        reference_quantity=int(raw.get("quantity") or 0),
        float_value=None,
        wear_name="",
        is_stattrak="StatTrak" in name,
        is_souvenir="Souvenir" in name,
        source="skinport",
        page_url=str(page),
    )


class SkinportClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        currency: str = "EUR",
        api_key: str = "",
        cache_ttl: int = 600,
    ) -> None:
        self._session = session
        self._currency = currency if currency in SUPPORTED else "EUR"
        self._api_key = api_key  # для чтения цен не требуется; на будущее
        self._cache_ttl = cache_ttl
        self._cache: list[CsFloatListing] = []
        self._cache_ts: float = 0.0

    async def get_listings(self) -> list[CsFloatListing]:
        if self._cache and (time.time() - self._cache_ts) < self._cache_ttl:
            return self._cache

        params = {"app_id": APP_ID_CS2, "currency": self._currency, "tradable": 0}
        try:
            async with self._session.get(
                ITEMS_URL,
                params=params,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    logger.warning("Skinport вернул %s", resp.status)
                    return self._cache
                payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            logger.warning("Ошибка запроса к Skinport: %s", exc)
            return self._cache

        if not isinstance(payload, list):
            logger.warning("Неожиданный формат ответа Skinport")
            return self._cache

        listings = [i for i in (_parse_item(r) for r in payload if isinstance(r, dict)) if i]
        if listings:
            self._cache = listings
            self._cache_ts = time.time()
            logger.info("Каталог Skinport обновлён: %d предметов", len(listings))
        return self._cache
