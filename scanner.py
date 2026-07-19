"""Сканер: собирает листинги CSFloat, сопоставляет с ценами market.csgo,
прогоняет через анализ и возвращает список выгодных сделок."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import aiohttp

from analysis.arbitrage import Opportunity, evaluate
from analysis.stability import analyze as analyze_stability
from config import Config
from sources.aggregate import AggregateClient
from sources.buff163 import BuffClient
from sources.csfloat import CsFloatClient
from sources.market_csgo import MarketCsgoClient
from sources.pricempire import PricempireClient
from sources.skinport import SkinportClient

logger = logging.getLogger(__name__)

SEEN_FILE = Path(os.getenv("SEEN_FILE", "seen_listings.json"))
SEEN_MAX = 5000


class Scanner:
    def __init__(self, cfg: Config, session: aiohttp.ClientSession) -> None:
        self.cfg = cfg
        self.csfloat = CsFloatClient(session, cfg.csfloat_api_key, usd_rate=cfg.usd_rate)
        self.market = MarketCsgoClient(
            session, cfg.market_csgo_api_key, currency=cfg.currency
        )
        self.skinport = SkinportClient(
            session,
            currency=cfg.currency,
            api_key=cfg.skinport_api_key,
            insecure=cfg.skinport_insecure,
        )
        self.buff = BuffClient(session, usd_rate=cfg.usd_rate)
        self.aggregate = AggregateClient(session, usd_rate=cfg.usd_rate)
        self.pricempire = PricempireClient(
            session, api_key=cfg.pricempire_api_key, currency=cfg.currency
        )
        self._seen: set[str] = self._load_seen()
        self.last_error: str | None = None

    # ---- Персистентность «уже показанных» листингов ----
    def _load_seen(self) -> set[str]:
        try:
            if SEEN_FILE.exists():
                return set(json.loads(SEEN_FILE.read_text()))
        except (OSError, ValueError) as exc:
            logger.warning("Не удалось прочитать %s: %s", SEEN_FILE, exc)
        return set()

    def _save_seen(self) -> None:
        try:
            # Обрезаем, чтобы файл не рос бесконечно
            data = list(self._seen)[-SEEN_MAX:]
            SEEN_FILE.write_text(json.dumps(data))
        except OSError as exc:
            logger.warning("Не удалось записать %s: %s", SEEN_FILE, exc)

    # ---- Основной проход ----
    async def scan(self) -> list[Opportunity]:
        """Полный проход: возвращает ВСЕ подходящие сделки (для команды /scan)."""
        self.last_error = None
        await self.market.ensure_loaded()

        listings: list = []
        if self.cfg.csfloat_enabled:
            listings = await self.csfloat.get_listings(
                limit=self.cfg.scan_limit,
                sort_by=self.cfg.scan_sort_by,
                min_price=self.cfg.min_buy_price or None,
                max_price=self.cfg.max_buy_price or None,
            )
            if not listings:
                self.last_error = (
                    self.csfloat.last_error
                    or "CSFloat не вернул листингов (проверь сеть/ключ)."
                )
            # Второй проход — по «скидке к рынку»: CSFloat сам сортирует лоты,
            # которые дешевле его оценки. Именно там прячутся лучшие сделки.
            if self.cfg.scan_deals:
                deals = await self.csfloat.get_listings(
                    limit=self.cfg.scan_limit,
                    sort_by="highest_discount",
                    min_price=self.cfg.min_buy_price or None,
                    max_price=self.cfg.max_buy_price or None,
                )
                seen_ids = {l.listing_id for l in listings}
                listings += [l for l in deals if l.listing_id not in seen_ids]

        # Добавляем предметы со Skinport (публичный API)
        if self.cfg.skinport_enabled:
            skinport_listings = await self.skinport.get_listings()
            if skinport_listings:
                listings = listings + skinport_listings

        if self.cfg.buff_enabled:
            await self.buff.ensure_loaded()
        if self.cfg.avg_enabled:
            await self.aggregate.ensure_loaded()
        if self.cfg.pricempire_enabled and self.cfg.pricempire_api_key:
            await self.pricempire.ensure_loaded()

        opportunities: list[Opportunity] = []
        for listing in listings:
            market_price = await self.market.get_price(listing.market_hash_name)
            # Цена Buff163 (из кэшированного фида, без доп. запросов) — до оценки,
            # чтобы учесть путь «продать на CSFloat по цене Buff».
            buff_base = buff_order = None
            if self.cfg.buff_enabled:
                buff = await self.buff.get_price(listing.market_hash_name)
                if buff:
                    buff_base = buff.starting_at
                    buff_order = buff.highest_order
            # Средняя цена по всем площадкам — считаем ДО оценки, чтобы учесть
            # главный путь «продать по средней рынка».
            avg_price: float | None = None
            avg_count = 0
            pricempire_avg = None
            if self.cfg.pricempire_enabled and self.cfg.pricempire_api_key:
                pricempire_avg = await self.pricempire.get_avg(listing.market_hash_name)
            if pricempire_avg:
                avg_price, avg_count = pricempire_avg, 30
            elif self.cfg.avg_enabled:
                agg = await self.aggregate.get_avg(listing.market_hash_name)
                if agg:
                    avg_price, avg_count = agg
            # Запас: если предмета нет в агрегаторе — по тому, что видим
            if avg_price is None:
                src_prices = [p for p in (
                    buff_base,
                    market_price.price if market_price else None,
                    listing.predicted_price if listing.predicted_price > 0 else None,
                ) if p]
                if src_prices:
                    avg_price = round(sum(src_prices) / len(src_prices), 2)
                    avg_count = len(src_prices)

            opp = evaluate(
                listing, market_price, self.cfg, buff_base, buff_order, avg_price, avg_count
            )
            if not opp:
                continue
            opp.pricempire_avg = pricempire_avg
            opportunities.append(opp)

        opportunities.sort(key=lambda o: o.best.profit_abs, reverse=True)
        return opportunities

    async def enrich_stability(self, opp: Opportunity) -> None:
        """Дополняет оффер анализом стабильности цены по истории продаж.
        Ошибки/отсутствие данных не мешают отправке оффера."""
        if not self.cfg.analyze_stability:
            return
        prices = await self.csfloat.get_price_history(opp.listing.market_hash_name)
        if prices:
            opp.stability = analyze_stability(prices)

    async def scan_new(self) -> list[Opportunity]:
        """Проход для авто-оповещений: только те сделки, что ещё не показывали."""
        opportunities = await self.scan()
        fresh = [o for o in opportunities if o.listing.listing_id not in self._seen]
        for o in fresh:
            self._seen.add(o.listing.listing_id)
        if fresh:
            self._save_seen()
        return fresh
