"""Средняя (агрегированная) цена скина с Pricempire.

Pricempire собирает цены с множества площадок и даёт агрегированную оценку —
удобно как «средняя рыночная цена». Нужен API-ключ (бесплатный, из личного
кабинета pricempire.com). Цены запрашиваем сразу в целевой валюте. Ответ
большой и лимиты строгие — кэшируем надолго.

ВАЖНО: точный формат ответа может отличаться по версии API, поэтому парсинг
терпимый: берём медиану цен по доступным источникам. Если увидишь, что цифры
в 100 раз больше/меньше — поменяй PRICE_DIVISOR.
"""
from __future__ import annotations

import logging
import statistics
import time
from typing import Any, Optional

import aiohttp

logger = logging.getLogger(__name__)

PRICES_URL = "https://api.pricempire.com/v3/items/prices"
PRICE_DIVISOR = 100  # Pricempire отдаёт цены в центах


def _extract_prices(sources: Any) -> list[float]:
    """Собирает числовые цены из блока источников одного предмета."""
    prices: list[float] = []
    if not isinstance(sources, dict):
        return prices
    for info in sources.values():
        raw = info.get("price") if isinstance(info, dict) else info
        try:
            val = float(raw) / PRICE_DIVISOR
        except (TypeError, ValueError):
            continue
        if val > 0:
            prices.append(val)
    return prices


class PricempireClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_key: str = "",
        currency: str = "EUR",
        cache_ttl: int = 3600,
    ) -> None:
        self._session = session
        self._api_key = api_key
        self._currency = currency
        self._cache_ttl = cache_ttl
        self._cache: dict[str, float] = {}
        self._cache_ts: float = 0.0
        self._cooldown_until: float = 0.0

    def _valid(self) -> bool:
        return bool(self._cache) and (time.time() - self._cache_ts) < self._cache_ttl

    async def _refresh(self) -> None:
        if not self._api_key:
            return
        params = {"api_key": self._api_key, "currency": self._currency}
        try:
            async with self._session.get(
                PRICES_URL, params=params, timeout=aiohttp.ClientTimeout(total=40)
            ) as resp:
                if resp.status != 200:
                    logger.warning("Pricempire вернул %s", resp.status)
                    self._cooldown_until = time.time() + 120
                    return
                payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            logger.warning("Ошибка запроса к Pricempire: %s", exc)
            self._cooldown_until = time.time() + 120
            return

        if not isinstance(payload, dict):
            logger.warning("Неожиданный формат ответа Pricempire: %r", type(payload))
            return

        # ДИАГНОСТИКА: структура одного предмета
        sample = next(iter(payload.items()), None)
        if sample:
            logger.info("PRICEMPIRE [%s] = %r", sample[0], sample[1])

        cache: dict[str, float] = {}
        for name, sources in payload.items():
            prices = _extract_prices(sources)
            if prices:
                cache[name] = round(statistics.median(prices), 2)
        if cache:
            self._cache = cache
            self._cache_ts = time.time()
            logger.info("Средние цены Pricempire обновлены: %d предметов", len(cache))

    async def get_avg(self, market_hash_name: str) -> Optional[float]:
        if not self._api_key:
            return None
        if not self._valid() and time.time() >= self._cooldown_until:
            await self._refresh()
        return self._cache.get(market_hash_name)

    async def ensure_loaded(self) -> int:
        if self._api_key and not self._valid() and time.time() >= self._cooldown_until:
            await self._refresh()
        return len(self._cache)
