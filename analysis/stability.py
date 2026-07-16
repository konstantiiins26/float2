"""Анализ стабильности цены по истории продаж.

По ряду цен (в хронологическом порядке) считаем:
- волатильность = коэффициент вариации (стандартное отклонение / среднее);
- тренд = сравнение средней первой и второй половины ряда.
И выдаём человекочитаемую оценку: стабильный / средний / нестабильный.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass

# Пороги коэффициента вариации (разброса цены)
STABLE_CV = 0.10     # < 10% разброса — стабильный
MEDIUM_CV = 0.20     # < 20% — средняя стабильность, выше — нестабильный
MIN_SAMPLE = 4       # меньше стольких продаж — данных мало


@dataclass
class StabilityReport:
    label: str        # "stable" | "medium" | "unstable" | "unknown"
    cv: float         # коэффициент вариации (доля), 0.05 = разброс 5%
    trend: str        # "up" | "down" | "flat" | "unknown"
    sample: int       # сколько точек в выборке

    @property
    def is_stable(self) -> bool:
        return self.label == "stable"


def analyze(prices: list[float]) -> StabilityReport:
    """prices — цены в хронологическом порядке (старые → новые)."""
    clean = [p for p in prices if p and p > 0]
    n = len(clean)
    if n < MIN_SAMPLE:
        return StabilityReport("unknown", 0.0, "unknown", n)

    mean = statistics.fmean(clean)
    if mean <= 0:
        return StabilityReport("unknown", 0.0, "unknown", n)
    std = statistics.pstdev(clean)
    cv = std / mean

    if cv < STABLE_CV:
        label = "stable"
    elif cv < MEDIUM_CV:
        label = "medium"
    else:
        label = "unstable"

    # Тренд: средняя второй половины против первой
    half = n // 2
    first = statistics.fmean(clean[:half]) if half else mean
    second = statistics.fmean(clean[half:])
    if second > first * 1.05:
        trend = "up"
    elif second < first * 0.95:
        trend = "down"
    else:
        trend = "flat"

    return StabilityReport(label, round(cv, 4), trend, n)
