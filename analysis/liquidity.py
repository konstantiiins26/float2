"""Оценка ликвидности и стабильности скина.

Стабильность мы понимаем в двух смыслах:
1. Ликвидность — насколько легко перепродать (объём на market.csgo и кол-во
   листингов на CSFloat). Неликвидный скин можно купить дёшево, но потом
   месяцами не продать.
2. Стабильность флоата — насколько флоат далёк от границы износа. У самой
   границы (например, 0.15 между MW и FT) цена резко скачет, и «средняя»
   оценка становится ненадёжной.
"""
from __future__ import annotations

# Границы износа CS2 (float)
WEAR_BOUNDARIES = (0.07, 0.15, 0.38, 0.45)


def distance_to_wear_edge(float_value: float | None) -> float:
    """Расстояние до ближайшей границы износа. Если флоат неизвестен — 1.0
    (считаем максимально стабильным, чтобы не отсекать предметы без флоата,
    например ножи в некоторых листингах)."""
    if float_value is None:
        return 1.0
    return min(abs(float_value - edge) for edge in WEAR_BOUNDARIES)


def is_float_stable(float_value: float | None, edge_margin: float) -> bool:
    """True, если флоат достаточно далёк от границы износа."""
    if edge_margin <= 0:
        return True
    return distance_to_wear_edge(float_value) >= edge_margin


def liquidity_score(market_volume: int, csfloat_quantity: int) -> float:
    """Простой скор ликвидности 0..100 для сортировки/показа.

    Логарифмически растёт с объёмом на обеих площадках, чтобы крупные объёмы
    не забивали шкалу.
    """
    import math

    vol = math.log1p(max(market_volume, 0)) * 15
    qty = math.log1p(max(csfloat_quantity, 0)) * 10
    return round(min(vol + qty, 100.0), 1)


def is_liquid(
    market_volume: int,
    csfloat_quantity: int,
    min_market_volume: float,
    min_csfloat_quantity: float,
) -> bool:
    return (
        market_volume >= min_market_volume
        and csfloat_quantity >= min_csfloat_quantity
    )
