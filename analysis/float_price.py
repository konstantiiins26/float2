"""Оценка цены с учётом конкретного флоата (как float appraiser у CSFloat).

Публичные цены (Buff163) даются на весь износ (напр. на все Field-Tested).
Но внутри износа цена зависит от флоата: чем ближе к «чистому» краю диапазона
(меньший флоат), тем дороже. Здесь корректируем базовую цену износа по позиции
флоата внутри его диапазона.

Это ПРИБЛИЖЕНИЕ: реальная надбавка за низкий флоат у разных скинов разная,
поэтому используем умеренный коэффициент чувствительности.
"""
from __future__ import annotations

from typing import Optional

# Диапазоны износа CS2 по флоату
WEAR_RANGES = {
    "Factory New": (0.00, 0.07),
    "Minimal Wear": (0.07, 0.15),
    "Field-Tested": (0.15, 0.38),
    "Well-Worn": (0.38, 0.45),
    "Battle-Scarred": (0.45, 1.00),
}
_BOUNDS = (0.0, 0.07, 0.15, 0.38, 0.45, 1.00)


def _range_from_float(f: float) -> tuple[float, float]:
    for i in range(len(_BOUNDS) - 1):
        if _BOUNDS[i] <= f < _BOUNDS[i + 1]:
            return _BOUNDS[i], _BOUNDS[i + 1]
    return 0.45, 1.00


def estimate_for_float(
    base_price: Optional[float],
    float_value: Optional[float],
    wear_name: str,
    sensitivity: float = 0.15,
) -> Optional[float]:
    """Оценка цены за конкретный флоат по базовой цене износа.

    sensitivity — насколько сильно флоат влияет на цену (0.15 = до ±7.5% на
    краях диапазона). Меньший флоат внутри износа → цена выше.
    """
    if not base_price or base_price <= 0:
        return None
    if float_value is None:
        return round(base_price, 2)

    rng = WEAR_RANGES.get(wear_name) or _range_from_float(float_value)
    lo, hi = rng
    if hi <= lo:
        return round(base_price, 2)

    pos = min(max((float_value - lo) / (hi - lo), 0.0), 1.0)
    factor = 1.0 + sensitivity * (0.5 - pos)  # низкий флоат (pos→0) дороже
    return round(base_price * factor, 2)
