"""Клиент CSFloat: получение активных buy_now листингов.

Документация публичного API: https://docs.csfloat.com/
Основной эндпоинт — GET /api/v1/listings, отдаёт активные лоты вместе с
`reference.predicted_price` (оценка справедливой/средней цены самим CSFloat)
и `reference.quantity` (кол-во листингов данного предмета = индикатор ликвидности).
"""
from __future__ import annotations

import logging
import statistics
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import quote

import aiohttp

logger = logging.getLogger(__name__)

BASE_URL = "https://csfloat.com/api/v1"

# Сколько последних продаж усредняем и как долго кэшируем медиану (сек)
SALES_SAMPLE = 15
SALES_CACHE_TTL = 3600      # успешный результат храним час
SALES_NEG_CACHE_TTL = 900   # если истории нет/ошибка — не долбим 15 минут


@dataclass
class CsFloatListing:
    """Один листинг на CSFloat, приведённый к удобному виду. Цены — в USD."""

    listing_id: str
    market_hash_name: str
    buy_price: float          # цена покупки (buy_now), USD
    predicted_price: float    # оценка средней цены продажи на CSFloat, USD
    reference_quantity: int   # сколько таких листингов на CSFloat (ликвидность)
    float_value: Optional[float]
    wear_name: str
    is_stattrak: bool
    is_souvenir: bool

    @property
    def url(self) -> str:
        return f"https://csfloat.com/item/{self.listing_id}"


def _cents_to_usd(value: Any) -> float:
    try:
        return round(float(value) / 100.0, 2)
    except (TypeError, ValueError):
        return 0.0


def _parse_listing(raw: dict[str, Any]) -> Optional[CsFloatListing]:
    try:
        item = raw.get("item") or {}
        reference = raw.get("reference") or {}
        name = item.get("market_hash_name")
        if not name:
            return None
        return CsFloatListing(
            listing_id=str(raw.get("id", "")),
            market_hash_name=name,
            buy_price=_cents_to_usd(raw.get("price")),
            predicted_price=_cents_to_usd(reference.get("predicted_price")),
            reference_quantity=int(reference.get("quantity") or 0),
            float_value=(
                float(item["float_value"])
                if item.get("float_value") is not None
                else None
            ),
            wear_name=item.get("wear_name") or "",
            is_stattrak=bool(item.get("is_stattrak")),
            is_souvenir=bool(item.get("is_souvenir")),
        )
    except (TypeError, ValueError) as exc:
        logger.warning("Не удалось разобрать листинг CSFloat: %s", exc)
        return None


class CsFloatClient:
    def __init__(self, session: aiohttp.ClientSession, api_key: str = "") -> None:
        self._session = session
        self._api_key = api_key
        # Кэш медианы продаж: name -> (время, значение|None)
        self._sales_cache: dict[str, tuple[float, Optional[float]]] = {}

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = self._api_key
        return headers

    async def get_listings(
        self,
        limit: int = 50,
        sort_by: str = "most_recent",
        min_price: Optional[float] = None,
        max_price: Optional[float] = None,
    ) -> list[CsFloatListing]:
        """Возвращает список активных buy_now листингов."""
        params: dict[str, Any] = {
            "limit": max(1, min(limit, 50)),
            "sort_by": sort_by,
            "type": "buy_now",
        }
        if min_price:
            params["min_price"] = int(min_price * 100)
        if max_price:
            params["max_price"] = int(max_price * 100)

        try:
            async with self._session.get(
                f"{BASE_URL}/listings",
                params=params,
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error(
                        "CSFloat вернул %s: %s", resp.status, body[:200]
                    )
                    return []
                payload = await resp.json()
        except (aiohttp.ClientError, TimeoutError) as exc:
            logger.error("Ошибка запроса к CSFloat: %s", exc)
            return []

        # Ответ может быть {"data": [...]} или просто [...]
        rows = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            logger.error("Неожиданный формат ответа CSFloat: %r", type(payload))
            return []

        listings = [_parse_listing(r) for r in rows]
        return [l for l in listings if l is not None]

    async def get_sales_median(self, market_hash_name: str) -> Optional[float]:
        """Медиана цен последних реальных продаж предмета на CSFloat, USD.

        Берётся из истории сделок (эндпоинт history/<name>/sales). Результат
        кэшируется. Если история недоступна или формат неожиданный —
        возвращает None (вызывающий код откатывается на оценку predicted_price).
        """
        now = time.time()
        cached = self._sales_cache.get(market_hash_name)
        if cached and (now - cached[0]) < (
            SALES_CACHE_TTL if cached[1] is not None else SALES_NEG_CACHE_TTL
        ):
            return cached[1]

        median = await self._fetch_sales_median(market_hash_name)
        self._sales_cache[market_hash_name] = (now, median)
        return median

    async def _fetch_sales_median(self, market_hash_name: str) -> Optional[float]:
        url = f"{BASE_URL}/history/{quote(market_hash_name, safe='')}/sales"
        try:
            async with self._session.get(
                url,
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status != 200:
                    return None
                payload = await resp.json()
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            logger.debug("История продаж CSFloat недоступна для %s: %s",
                         market_hash_name, exc)
            return None

        rows = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(rows, list) or not rows:
            return None

        prices: list[float] = []
        for row in rows[:SALES_SAMPLE]:
            if not isinstance(row, dict):
                continue
            # цена продажи может лежать в разных полях, все в центах
            raw = row.get("price") or row.get("total_price") or row.get("sold_price")
            if raw is None:
                continue
            try:
                prices.append(float(raw) / 100.0)
            except (TypeError, ValueError):
                continue

        if not prices:
            return None
        return round(statistics.median(prices), 2)
