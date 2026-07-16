"""Ядро анализа: из листинга CSFloat + цены market.csgo считает выгоду.

Схема сделки:
  - Покупаем на CSFloat по `buy_price`.
  - Продаём одним из двух путей:
      а) обратно на CSFloat по средней (predicted) цене за вычетом комиссии CSFloat;
      б) на market.csgo по их цене за вычетом комиссии market.csgo.
  - Берём лучший из путей как основную рекомендацию.

Опция проходит фильтр, только если она ликвидна, стабильна по флоату и
прибыль превышает пороги (в % и в абсолюте).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from analysis.liquidity import (
    distance_to_wear_edge,
    is_float_stable,
    is_liquid,
    liquidity_score,
)
from config import Config
from sources.csfloat import CsFloatListing
from sources.market_csgo import MarketPrice


@dataclass
class ResaleRoute:
    venue: str            # "CSFloat" | "market.csgo"
    gross_price: float    # цена продажи до комиссии, USD
    net_price: float      # то, что получаем на руки после комиссии, USD
    profit_abs: float     # чистая прибыль, USD
    profit_pct: float     # чистая прибыль в % от цены покупки


@dataclass
class Opportunity:
    listing: CsFloatListing
    market: Optional[MarketPrice]
    buy_price: float
    routes: list[ResaleRoute]        # отсортированы по убыванию прибыли
    liquidity: float
    float_edge_distance: float
    currency_symbol: str = "$"

    @property
    def best(self) -> ResaleRoute:
        return self.routes[0]

    @property
    def csfloat_route(self) -> Optional[ResaleRoute]:
        return next((r for r in self.routes if r.venue == "CSFloat"), None)

    @property
    def market_route(self) -> Optional[ResaleRoute]:
        return next((r for r in self.routes if r.venue == "market.csgo"), None)


def _route(venue: str, buy_price: float, gross: float, fee: float) -> Optional[ResaleRoute]:
    if gross <= 0:
        return None
    net = gross * (1.0 - fee)
    profit = net - buy_price
    pct = (profit / buy_price * 100.0) if buy_price > 0 else 0.0
    return ResaleRoute(
        venue=venue,
        gross_price=round(gross, 2),
        net_price=round(net, 2),
        profit_abs=round(profit, 2),
        profit_pct=round(pct, 1),
    )


def evaluate(
    listing: CsFloatListing,
    market: Optional[MarketPrice],
    cfg: Config,
) -> Optional[Opportunity]:
    """Оценивает один листинг. Возвращает Opportunity, если сделка проходит
    все фильтры, иначе None."""
    buy = listing.buy_price
    if buy <= 0:
        return None

    # Исключаем нежелательные категории
    if cfg.exclude_souvenir and listing.is_souvenir:
        return None
    if cfg.exclude_stickers and listing.is_sticker:
        return None

    # Ценовой диапазон покупки
    if cfg.min_buy_price and buy < cfg.min_buy_price:
        return None
    if cfg.max_buy_price and buy > cfg.max_buy_price:
        return None

    # Стабильность флоата
    if not is_float_stable(listing.float_value, cfg.float_edge_margin):
        return None

    # Ликвидность (объём market.csgo + кол-во листингов CSFloat)
    market_volume = market.volume if market else 0
    if not is_liquid(
        market_volume,
        listing.reference_quantity,
        cfg.min_market_volume,
        cfg.min_csfloat_quantity,
    ):
        return None

    # Считаем оба пути перепродажи
    routes: list[ResaleRoute] = []
    r_csfloat = _route(
        "CSFloat", buy, listing.predicted_price, cfg.csfloat_fee
    )
    if r_csfloat:
        routes.append(r_csfloat)
    if market:
        r_market = _route(
            "market.csgo", buy, market.price, cfg.market_csgo_fee
        )
        if r_market:
            routes.append(r_market)

    if not routes:
        return None

    routes.sort(key=lambda r: r.profit_abs, reverse=True)
    best = routes[0]

    # Пороги прибыли
    if best.profit_abs < cfg.min_profit_abs:
        return None
    if best.profit_pct < cfg.min_profit_percent:
        return None

    return Opportunity(
        listing=listing,
        market=market,
        buy_price=buy,
        routes=routes,
        liquidity=liquidity_score(market_volume, listing.reference_quantity),
        float_edge_distance=round(distance_to_wear_edge(listing.float_value), 4),
        currency_symbol=cfg.currency_symbol,
    )
