"""Клиент CSFloat: получение активных buy_now листингов.

Документация публичного API: https://docs.csfloat.com/
Основной эндпоинт — GET /api/v1/listings, отдаёт активные лоты вместе с
`reference.predicted_price` (оценка справедливой/средней цены самим CSFloat)
и `reference.quantity` (кол-во листингов данного предмета = индикатор ликвидности).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional
from urllib.parse import quote

import aiohttp

logger = logging.getLogger(__name__)

BASE_URL = "https://csfloat.com/api/v1"

HISTORY_SAMPLE = 30       # сколько последних продаж берём для анализа
HISTORY_CACHE_TTL = 3600  # кэш истории цен, сек


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
    created_at: str = ""      # когда листинг выставлен (ISO8601)
    watchers: int = 0         # сколько человек «наблюдают» за лотом
    item_type: str = ""       # тип предмета (skin, sticker, ...) если есть
    source: str = "csfloat"   # площадка покупки: csfloat | csmoney
    page_url: str = ""        # прямая ссылка на лот (если не csfloat)
    paint_seed: Optional[int] = None   # сид/паттерн
    low_rank: Optional[int] = None     # место по самому низкому флоату (1 = топ)
    high_rank: Optional[int] = None    # место по самому высокому флоату (1 = топ)

    @property
    def url(self) -> str:
        return self.page_url or f"https://csfloat.com/item/{self.listing_id}"

    @property
    def float_rank(self) -> Optional[int]:
        """Лучшее (наименьшее) место по флоату среди low/high, если есть."""
        ranks = [r for r in (self.low_rank, self.high_rank) if r]
        return min(ranks) if ranks else None

    @property
    def rank_kind(self) -> str:
        """Какой это ранг (в дательном падеже): низкому или высокому флоату."""
        lr, hr = self.low_rank or 10**9, self.high_rank or 10**9
        return "низкому" if lr <= hr else "высокому"

    @property
    def source_name(self) -> str:
        return {
            "csfloat": "CSFloat",
            "csmoney": "CS.MONEY",
            "skinport": "Skinport",
        }.get(self.source, self.source)

    @property
    def is_sticker(self) -> bool:
        return (
            self.item_type == "sticker"
            or self.market_hash_name.startswith("Sticker |")
        )


def _cents_to_usd(value: Any) -> float:
    try:
        return round(float(value) / 100.0, 2)
    except (TypeError, ValueError):
        return 0.0


def _row_time(row: dict[str, Any], index: int) -> float:
    """Время продажи как unix-timestamp для сортировки. Если распарсить не
    вышло — используем обратный индекс (CSFloat отдаёт новые продажи первыми,
    поэтому больший индекс = более старая продажа)."""
    raw = row.get("sold_at") or row.get("created_at") or row.get("date") or row.get("timestamp")
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str) and raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return float(-index)


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
            created_at=str(raw.get("created_at") or ""),
            watchers=int(raw.get("watchers") or 0),
            item_type=str(item.get("type") or ""),
            paint_seed=(int(item["paint_seed"]) if item.get("paint_seed") is not None else None),
            low_rank=(int(item["low_rank"]) if item.get("low_rank") is not None else None),
            high_rank=(int(item["high_rank"]) if item.get("high_rank") is not None else None),
        )
    except (TypeError, ValueError) as exc:
        logger.warning("Не удалось разобрать листинг CSFloat: %s", exc)
        return None


class CsFloatClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_key: str = "",
        usd_rate: float = 1.0,
    ) -> None:
        self._session = session
        self._api_key = api_key
        # Курс: сколько единиц целевой валюты в 1 USD (цены CSFloat приходят в USD)
        self._usd_rate = usd_rate if usd_rate > 0 else 1.0
        # Кэш истории цен: name -> (время, список цен в целевой валюте)
        self._history_cache: dict[str, tuple[float, list[float]]] = {}
        self.last_error: str = ""  # понятная причина последнего пустого ответа

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
        # Пороги приходят в целевой валюте — конвертируем в USD-центы для API
        if min_price:
            params["min_price"] = int(min_price / self._usd_rate * 100)
        if max_price:
            params["max_price"] = int(max_price / self._usd_rate * 100)

        self.last_error = ""
        try:
            async with self._session.get(
                f"{BASE_URL}/listings",
                params=params,
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error("CSFloat вернул %s: %s", resp.status, body[:200])
                    hint = {
                        429: "слишком много запросов (лимит). Увеличь интервал скана.",
                        401: "неверный ключ CSFloat.",
                        403: "доступ запрещён (ключ/блокировка).",
                    }.get(resp.status, body[:120])
                    self.last_error = f"CSFloat вернул HTTP {resp.status}: {hint}"
                    return []
                payload = await resp.json()
        except (aiohttp.ClientError, TimeoutError) as exc:
            logger.error("Ошибка запроса к CSFloat: %s", exc)
            self.last_error = f"CSFloat: ошибка сети — {exc}"
            return []

        # Ответ может быть {"data": [...]} или просто [...]
        rows = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            logger.error("Неожиданный формат ответа CSFloat: %r", type(payload))
            self.last_error = "CSFloat: неожиданный формат ответа."
            return []

        listings = [l for l in (_parse_listing(r) for r in rows) if l is not None]
        if rows and not listings:
            self.last_error = (
                f"CSFloat отдал {len(rows)} предметов, но ни один не распознан "
                "(изменился формат ответа)."
            )
        elif not rows:
            self.last_error = "CSFloat отдал пустой список (0 предметов)."

        # Конвертируем цены USD -> целевая валюта
        if self._usd_rate != 1.0:
            for l in listings:
                l.buy_price = round(l.buy_price * self._usd_rate, 2)
                l.predicted_price = round(l.predicted_price * self._usd_rate, 2)

        return listings

    async def get_listing_status(self, listing_id: str) -> str:
        """Проверяет актуальность лота в реальном времени.

        Возвращает: "active" (ещё доступен), "sold" (куплен/снят),
        "unknown" (не удалось проверить — сеть/лимит; тогда оффер всё равно
        показываем, просто без гарантии)."""
        if not listing_id:
            return "unknown"
        try:
            async with self._session.get(
                f"{BASE_URL}/listings/{listing_id}",
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status == 404:
                    return "sold"
                if resp.status != 200:
                    return "unknown"
                payload = await resp.json()
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            logger.debug("Не удалось проверить статус %s: %s", listing_id, exc)
            return "unknown"

        # Ответ может быть {"data": {...}} или сам объект лота {...}
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            data = payload["data"]
        elif isinstance(payload, dict):
            data = payload
        else:
            return "unknown"
        state = str(data.get("state") or "").lower()
        if state == "listed":
            return "active"
        if state:
            return "sold"
        return "unknown"

    async def get_price_history(self, market_hash_name: str) -> list[float]:
        """Цены последних продаж предмета (в целевой валюте, хронологически:
        старые → новые). Пустой список = данных нет / ошибка. Кэшируется."""
        now = time.time()
        cached = self._history_cache.get(market_hash_name)
        if cached and (now - cached[0]) < HISTORY_CACHE_TTL:
            return cached[1]

        prices = await self._fetch_price_history(market_hash_name)
        self._history_cache[market_hash_name] = (now, prices)
        return prices

    async def _fetch_price_history(self, market_hash_name: str) -> list[float]:
        url = f"{BASE_URL}/history/{quote(market_hash_name, safe='')}/sales"
        try:
            async with self._session.get(
                url,
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status != 200:
                    return []
                payload = await resp.json()
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            logger.debug("История цен CSFloat недоступна для %s: %s",
                         market_hash_name, exc)
            return []

        rows = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(rows, list) or not rows:
            return []

        points: list[tuple[float, float]] = []
        for i, row in enumerate(rows[:HISTORY_SAMPLE]):
            if not isinstance(row, dict):
                continue
            raw_price = row.get("price") or row.get("total_price") or row.get("sold_price")
            if raw_price is None:
                continue
            try:
                price = float(raw_price) / 100.0 * self._usd_rate
            except (TypeError, ValueError):
                continue
            points.append((_row_time(row, i), price))

        if not points:
            return []
        points.sort(key=lambda p: p[0])  # хронологически: старые → новые
        return [p for _, p in points]
