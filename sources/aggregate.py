"""Средняя цена скина по МНОГИМ площадкам сразу (без ключей).

Берём публичный агрегатор цен csgotrader.app (prices_v6.json) — он собирает
цены с ~15 площадок: Steam, Buff163, Skinport, CS.MONEY, DMarket, Market.CSGO,
Waxpeer, BitSkins, LootFarm, CS.Deals, SwapGG и др. По каждому предмету берём
медиану доступных цен = «средняя рыночная цена».

Ключ не нужен. Файл большой — кэшируем надолго. Цены в USD → конвертируем.
"""
from __future__ import annotations

import json
import logging
import statistics
import time
from typing import Any, Optional

import aiohttp

logger = logging.getLogger(__name__)

PRICES_URL = "https://prices.csgotrader.app/latest/prices_v6.json"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}

# Ключи, по которым в разных источниках лежит цена (пробуем по очереди)
_PRICE_KEYS = (
    "price", "starting_at", "suggested_price",
    "last_24h", "last_7d", "last_30d", "avg", "min",
)


def _extract_one(node: Any) -> Optional[float]:
    """Достаёт цену из блока одного источника (форматы у всех разные)."""
    if isinstance(node, bool):
        return None
    if isinstance(node, (int, float)):
        return float(node) if node > 0 else None
    if isinstance(node, str):
        try:
            v = float(node)
            return v if v > 0 else None
        except ValueError:
            return None
    if isinstance(node, dict):
        for key in _PRICE_KEYS:
            if key in node:
                v = _extract_one(node[key])  # напр. starting_at = {"price": X}
                if v:
                    return v
    return None


# Steam исключаем из средней: цена завышена (комиссия 15%) — это выброс
_EXCLUDED = {"steam", "steam_listing", "steam_volume"}


def _item_prices(sources: Any) -> list[float]:
    prices: list[float] = []
    if not isinstance(sources, dict):
        return prices
    for key, node in sources.items():
        if str(key).lower() in _EXCLUDED:
            continue
        v = _extract_one(node)
        if v:
            prices.append(v)
    return prices


class AggregateClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        usd_rate: float = 1.0,
        cache_ttl: int = 21600,  # 6 часов
    ) -> None:
        self._session = session
        self._usd_rate = usd_rate if usd_rate > 0 else 1.0
        self._cache_ttl = cache_ttl
        self._cache: dict[str, tuple[float, int]] = {}  # name -> (средняя, кол-во площадок)
        self._cache_ts: float = 0.0
        self._cooldown_until: float = 0.0

    def _valid(self) -> bool:
        return bool(self._cache) and (time.time() - self._cache_ts) < self._cache_ttl

    async def _refresh(self) -> None:
        try:
            async with self._session.get(
                PRICES_URL, headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=120)
            ) as resp:
                if resp.status != 200:
                    logger.warning("Агрегатор цен вернул %s", resp.status)
                    self._cooldown_until = time.time() + 300
                    return
                text = await resp.text()
            if not text.strip():
                logger.warning("Агрегатор: пустой ответ (0 байт) — CDN/сжатие")
                self._cooldown_until = time.time() + 300
                return
            payload = json.loads(text)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            logger.warning("Ошибка загрузки агрегатора цен: %s", exc)
            self._cooldown_until = time.time() + 300
            return

        if not isinstance(payload, dict):
            logger.warning("Неожиданный формат агрегатора цен")
            return

        # ДИАГНОСТИКА: покажем реальную структуру одного предмета
        sample = next(iter(payload.items()), None)
        if sample:
            name, srcs = sample
            if isinstance(srcs, dict):
                logger.info("АГРЕГАТОР [%s] источники: %s", name, list(srcs.keys()))
                one = next(iter(srcs.items()), None)
                if one:
                    logger.info("АГРЕГАТОР пример источника %s = %r", one[0], one[1])

        r = self._usd_rate
        cache: dict[str, tuple[float, int]] = {}
        counts: list[int] = []
        for name, sources in payload.items():
            prices = _item_prices(sources)
            if len(prices) >= 2:  # нужна хотя бы пара площадок для «средней»
                cache[name] = (round(statistics.median(prices) * r, 2), len(prices))
                counts.append(len(prices))
        if cache:
            self._cache = cache
            self._cache_ts = time.time()
            avg_c = sum(counts) / len(counts)
            logger.info(
                "Средние цены обновлены: %d предметов, в среднем %.1f площадок/предмет",
                len(cache), avg_c,
            )

    async def get_avg(self, market_hash_name: str) -> Optional[tuple[float, int]]:
        """Возвращает (средняя_цена, сколько_площадок) или None."""
        if not self._valid() and time.time() >= self._cooldown_until:
            await self._refresh()
        return self._cache.get(market_hash_name)

    async def ensure_loaded(self) -> int:
        if not self._valid() and time.time() >= self._cooldown_until:
            await self._refresh()
        return len(self._cache)
