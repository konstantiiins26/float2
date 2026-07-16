"""Клиент market.csgo.com (TM Market): прайс-лист и ликвидность.

Эндпоинт GET /api/v2/prices/USD.json отдаёт разом весь рынок: по каждому
предмету — минимальная цена (`price`) и объём (`volume`, кол-во лотов).
Ключ для прайс-листа не обязателен. Данные кэшируются, чтобы не дёргать
market.csgo на каждый листинг CSFloat.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

import aiohttp

logger = logging.getLogger(__name__)

PRICES_URL = "https://market.csgo.com/api/v2/prices/USD.json"


@dataclass
class MarketPrice:
    market_hash_name: str
    price: float     # минимальная цена продажи на market.csgo, USD
    volume: int      # кол-во доступных лотов (ликвидность)


class MarketCsgoClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_key: str = "",
        cache_ttl: int = 300,
    ) -> None:
        self._session = session
        self._api_key = api_key
        self._cache_ttl = cache_ttl
        self._cache: dict[str, MarketPrice] = {}
        self._cache_ts: float = 0.0

    def _is_cache_valid(self) -> bool:
        return bool(self._cache) and (time.time() - self._cache_ts) < self._cache_ttl

    async def _refresh(self) -> None:
        params: dict[str, Any] = {}
        if self._api_key:
            params["key"] = self._api_key
        try:
            async with self._session.get(
                PRICES_URL,
                params=params,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    logger.error("market.csgo вернул %s", resp.status)
                    return
                # content_type у них иногда text/plain — не проверяем строго
                payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            logger.error("Ошибка запроса к market.csgo: %s", exc)
            return

        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            logger.error("Неожиданный формат прайс-листа market.csgo")
            return

        cache: dict[str, MarketPrice] = {}
        for it in items:
            name = it.get("market_hash_name")
            if not name:
                continue
            try:
                cache[name] = MarketPrice(
                    market_hash_name=name,
                    price=float(it.get("price") or 0),
                    volume=int(float(it.get("volume") or 0)),
                )
            except (TypeError, ValueError):
                continue

        if cache:
            self._cache = cache
            self._cache_ts = time.time()
            logger.info("Прайс-лист market.csgo обновлён: %d предметов", len(cache))

    async def get_price(self, market_hash_name: str) -> Optional[MarketPrice]:
        if not self._is_cache_valid():
            await self._refresh()
        return self._cache.get(market_hash_name)

    async def ensure_loaded(self) -> int:
        """Гарантирует, что прайс-лист загружен. Возвращает размер кэша."""
        if not self._is_cache_valid():
            await self._refresh()
        return len(self._cache)
