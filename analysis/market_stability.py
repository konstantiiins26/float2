"""Стабильность рынка на market.csgo (куда продаём).

У market.csgo нет открытого графика-истории, поэтому стабильность оцениваем по
тем данным, что у них точно есть и которые напрямую влияют на продажу:
- ОБЪЁМ (volume) — сколько лотов торгуется. Много = ликвидный, стабильный рынок,
  продать легко и цена предсказуема.
- СПРЕД — насколько текущая цена market.csgo отклоняется от средней/справедливой
  цены. Большое отклонение = цена-выброс, нестабильно.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

VOL_STABLE = 30      # объём для «стабильного» рынка
VOL_MEDIUM = 10      # объём для «среднего»
SPREAD_STABLE = 0.06  # ≤6% отклонения цены — стабильно
SPREAD_MEDIUM = 0.15  # ≤15% — средне, выше — нестабильно


@dataclass
class MarketStability:
    label: str               # "stable" | "medium" | "unstable" | "unknown"
    volume: int
    spread: Optional[float]  # доля отклонения цены от средней (0.05 = 5%)


def analyze_market(
    volume: int,
    market_price: float,
    reference_avg: Optional[float],
) -> MarketStability:
    if not market_price or market_price <= 0:
        return MarketStability("unknown", int(volume or 0), None)

    spread: Optional[float] = None
    if reference_avg and reference_avg > 0:
        spread = abs(market_price - reference_avg) / reference_avg

    # сигнал по объёму
    if volume >= VOL_STABLE:
        vscore = 2
    elif volume >= VOL_MEDIUM:
        vscore = 1
    else:
        vscore = 0

    # сигнал по спреду (если средней нет — не штрафуем)
    if spread is None or spread <= SPREAD_STABLE:
        sscore = 2
    elif spread <= SPREAD_MEDIUM:
        sscore = 1
    else:
        sscore = 0

    label = {2: "stable", 1: "medium", 0: "unstable"}[min(vscore, sscore)]
    return MarketStability(label, int(volume), spread)
