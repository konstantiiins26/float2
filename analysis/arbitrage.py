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

from dataclasses import dataclass, field
from typing import Optional

from analysis.liquidity import (
    distance_to_wear_edge,
    is_float_stable,
    is_liquid,
    liquidity_score,
)
from analysis.categories import is_wanted
from analysis.float_price import estimate_for_float
from analysis.market_stability import MarketStability, analyze_market
from analysis.stability import StabilityReport
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
    live_status: str = ""  # "active" | "sold" | "unknown" (проверка перед отправкой)
    market_stability: Optional["MarketStability"] = None  # стабильность рынка market.csgo
    stability: Optional["StabilityReport"] = None  # история цены CSFloat (второстепенно)
    is_rank_find: bool = False  # найден из-за топового ранга по флоату
    float_rank: Optional[int] = None
    rank_kind: str = ""
    buff_start: Optional[float] = None   # Buff163: цена лотов (рыночная), валюта
    buff_order: Optional[float] = None   # Buff163: цена ордера (быстрая продажа)
    buff_fee: float = 0.025              # комиссия Buff163
    buff_float_estimate: Optional[float] = None  # оценка цены Buff за твой флоат
    pricempire_avg: Optional[float] = None       # средняя цена скина (Pricempire, если есть ключ)
    market_avg: Optional[float] = None           # средняя по площадкам, что видит бот
    market_avg_count: int = 0                    # из скольких площадок посчитана

    @property
    def best(self) -> ResaleRoute:
        return self.routes[0]

    @property
    def buff_pct(self) -> Optional[float]:
        """За сколько % от цены Buff163 куплено (меньше = лучше). Если есть
        оценка по флоату — считаем от неё, иначе от рыночной цены лотов."""
        ref = self.buff_float_estimate or self.buff_start
        if ref and ref > 0:
            return round(self.buy_price / ref * 100.0, 1)
        return None

    @property
    def buff_float_net(self) -> Optional[float]:
        """Сколько на руки, если продать на Buff по оценке за твой флоат."""
        if self.buff_float_estimate and self.buff_float_estimate > 0:
            return round(self.buff_float_estimate * (1.0 - self.buff_fee), 2)
        return None

    appraiser_route: Optional[ResaleRoute] = field(default=None)

    @property
    def csfloat_route(self) -> Optional[ResaleRoute]:
        return self.appraiser_route

    @property
    def market_route(self) -> Optional[ResaleRoute]:
        return next((r for r in self.routes if r.venue == "market.csgo"), None)

    @property
    def buff_route(self) -> Optional[ResaleRoute]:
        return next((r for r in self.routes if r.venue == "CSFloat по Buff"), None)

    @property
    def avg_route(self) -> Optional[ResaleRoute]:
        return next((r for r in self.routes if r.venue == "средней рынка"), None)


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
    buff_base: Optional[float] = None,
    buff_order: Optional[float] = None,
    avg_price: Optional[float] = None,
    avg_count: int = 0,
) -> Optional[Opportunity]:
    """Оценивает один листинг. Возвращает Opportunity, если сделка проходит
    все фильтры, иначе None.

    buff_base — цена Buff163 за износ (лоты).
    avg_price — средняя цена по всем площадкам; из неё считаем главный путь
    «продать по средней рынка».
    """
    buy = listing.buy_price
    if buy <= 0:
        return None

    # Исключаем нежелательные категории
    if cfg.exclude_souvenir and listing.is_souvenir:
        return None
    if cfg.weapons_only and not is_wanted(listing.market_hash_name):
        return None

    # Топ по флоату из базы CSFloat: редкий предмет (например #1 по флоату).
    # Такие показываем даже без арбитражной выгоды и без порогов ликвидности.
    rank = listing.float_rank
    is_rank_find = bool(cfg.float_rank_alert and rank and rank <= cfg.float_rank_alert)

    # Ценовой диапазон покупки
    if cfg.min_buy_price and buy < cfg.min_buy_price:
        return None
    if cfg.max_buy_price and buy > cfg.max_buy_price:
        return None

    # Стабильность флоата
    if not is_float_stable(listing.float_value, cfg.float_edge_margin):
        return None

    # Учитываем, включён ли market.csgo как площадка продажи
    market_on = cfg.market_enabled and market is not None
    market_volume = market.volume if market_on else 0

    # Ликвидность
    if listing.source == "csfloat":
        if market_on:
            liquid = is_liquid(
                market_volume,
                listing.reference_quantity,
                cfg.min_market_volume,
                cfg.min_csfloat_quantity,
            )
        else:
            # market.csgo выключен — оцениваем по ликвидности самого CSFloat
            liquid = listing.reference_quantity >= cfg.min_csfloat_quantity
    else:
        # Skinport продаётся только на market.csgo — без него смысла нет
        if not market_on:
            return None
        liquid = market_volume >= cfg.min_market_volume
    if not liquid and not is_rank_find:
        return None

    # Оценка CSFloat Appraiser — ТОЛЬКО справочно, в лучший путь не идёт
    appraiser_route = _route(
        "CSFloat", buy, listing.predicted_price, cfg.csfloat_fee
    )

    # Реальные пути перепродажи (по ним считается лучший путь и пороги)
    routes: list[ResaleRoute] = []
    if market_on:
        r_market = _route(
            "market.csgo", buy, market.price, cfg.market_csgo_fee
        )
        if r_market:
            routes.append(r_market)

    # Путь «продать на CSFloat по цене Buff» (оценка Buff за конкретный флоат)
    buff_float_estimate = estimate_for_float(
        buff_base, listing.float_value, listing.wear_name, cfg.buff_float_sensitivity
    ) if buff_base else None
    if cfg.buff_resale and buff_float_estimate:
        r_buff = _route("CSFloat по Buff", buy, buff_float_estimate, cfg.csfloat_fee)
        if r_buff:
            routes.append(r_buff)

    # Главный путь — продать по средней цене всех площадок
    if avg_price and avg_price > 0:
        r_avg = _route("средней рынка", buy, avg_price, cfg.avg_fee)
        if r_avg:
            routes.append(r_avg)

    if not routes:
        if is_rank_find:
            routes.append(ResaleRoute("CSFloat", round(buy, 2), round(buy, 2), 0.0, 0.0))
        else:
            return None

    routes.sort(key=lambda r: r.profit_abs, reverse=True)
    best = routes[0]

    # Пороги прибыли (топовый ранг по флоату пропускаем мимо порогов)
    if not is_rank_find:
        if best.profit_abs < cfg.min_profit_abs:
            return None
        if best.profit_pct < cfg.min_profit_percent:
            return None

    reference_avg = listing.predicted_price if listing.predicted_price > 0 else None
    market_stability = (
        analyze_market(market_volume, market.price, reference_avg)
        if market_on
        else None
    )

    return Opportunity(
        listing=listing,
        market=market if market_on else None,
        buy_price=buy,
        routes=routes,
        liquidity=liquidity_score(market_volume, listing.reference_quantity),
        float_edge_distance=round(distance_to_wear_edge(listing.float_value), 4),
        currency_symbol=cfg.currency_symbol,
        market_stability=market_stability,
        is_rank_find=is_rank_find,
        float_rank=rank,
        rank_kind=listing.rank_kind if rank else "",
        buff_fee=cfg.buff_fee,
        buff_start=buff_base,
        buff_order=buff_order,
        buff_float_estimate=buff_float_estimate,
        market_avg=avg_price,
        market_avg_count=avg_count,
        appraiser_route=appraiser_route,
    )
