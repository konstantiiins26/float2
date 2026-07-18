"""Цены Buff163 — главный ценовой ориентир в трейде скинов.

Напрямую с buff.163.com брать тяжело (китайский аккаунт, куки, блокировки),
поэтому берём готовый публичный фид цен csgotrader.app (тот же источник, что
используют расширения вроде BetterFloat). Ключ не нужен. Цены в USD —
конвертируем в целевую валюту. Файл большой, поэтому кэшируем надолго.

По каждому предмету Buff даёт два ключевых числа:
- starting_at (лоты)   — минимальная цена продажи, т.е. рыночная стоимость;
- highest_order (ордер) — макс. цена скупки, т.е. за сколько можно продать сразу.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

import aiohttp

logger = logging.getLogger(__name__)

BUFF_URL = "https://prices.csgotrader.app/latest/buff163.json"


@dataclass
class BuffPrice:
    starting_at: Optional[float]   # лоты (рыночная цена), в целевой валюте
    highest_order: Optional[float]  # ордер (быстрая продажа), в целевой валюте


def _price(node: Any) -> Optional[float]:
    if isinstance(node, dict):
        node = node.get("price")
    try:
        val = float(node)
        return val if val > 0 else None
    except (TypeError, ValueError):
        return None


class BuffClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        usd_rate: float = 1.0,
        cache_ttl: int = 21600,  # 6 часов
    ) -> None:
        self._session = session
        self._usd_rate = usd_rate if usd_rate > 0 else 1.0
        self._cache_ttl = cache_ttl
        self._cache: dict[str, BuffPrice] = {}
        self._cache_ts: float = 0.0

    def _valid(self) -> bool:
        return bool(self._cache) and (time.time() - self._cache_ts) < self._cache_ttl

    async def _refresh(self) -> None:
        try:
            async with self._session.get(
                BUFF_URL, timeout=aiohttp.ClientTimeout(total=40)
            ) as resp:
                if resp.status != 200:
                    logger.warning("Buff163-фид вернул %s", resp.status)
                    return
                payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            logger.warning("Ошибка загрузки цен Buff163: %s", exc)
            return

        if not isinstance(payload, dict):
            logger.warning("Неожиданный формат фида Buff163")
            return

        cache: dict[str, BuffPrice] = {}
        r = self._usd_rate
        for name, node in payload.items():
            if not isinstance(node, dict):
                continue
            start = _price(node.get("starting_at"))
            order = _price(node.get("highest_order"))
            if start is None and order is None:
                continue
            cache[name] = BuffPrice(
                starting_at=round(start * r, 2) if start else None,
                highest_order=round(order * r, 2) if order else None,
            )
        if cache:
            self._cache = cache
            self._cache_ts = time.time()
            logger.info("Цены Buff163 обновлены: %d предметов", len(cache))

    async def get_price(self, market_hash_name: str) -> Optional[BuffPrice]:
        if not self._valid():
            await self._refresh()
        return self._cache.get(market_hash_name)

    async def ensure_loaded(self) -> int:
        if not self._valid():
            await self._refresh()
        return len(self._cache)
