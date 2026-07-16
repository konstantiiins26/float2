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
from sources.csfloat import CsFloatClient
from sources.market_csgo import MarketCsgoClient

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

        listings = await self.csfloat.get_listings(
            limit=self.cfg.scan_limit,
            sort_by=self.cfg.scan_sort_by,
            min_price=self.cfg.min_buy_price or None,
            max_price=self.cfg.max_buy_price or None,
        )
        if not listings:
            self.last_error = "CSFloat не вернул листингов (проверь сеть/ключ)."

        opportunities: list[Opportunity] = []
        for listing in listings:
            market_price = await self.market.get_price(listing.market_hash_name)
            opp = evaluate(listing, market_price, self.cfg)
            if opp:
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
