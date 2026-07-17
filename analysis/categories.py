"""Определение категории предмета по названию.

Нужны только: оружие, ножи, перчатки, агенты.
Не нужны: стикеры, брелки (charms), граффити, патчи, музыка, кейсы, капсулы,
значки, пропуска и прочие коллекционные предметы.

Логика: у оружия/ножей/перчаток/агентов в названии есть разделитель «|»
(например «AK-47 | Redline», «★ Karambit | Doppler», «Sir Bloody … | The
Professionals»). У ванильных ножей есть «★». Мусорные категории отсекаем по
явным маркерам в названии, а предметы без «|» и без «★» (кейсы, значки,
пропуска) не берём.
"""
from __future__ import annotations

# Явные маркеры нежелательных категорий
JUNK_MARKERS = (
    "Sticker |",
    "Sealed Graffiti |",
    "Graffiti |",
    "Charm |",          # брелки CS2
    "Patch |",
    "Music Kit |",
    "Autograph Capsule",
)


def is_wanted(market_hash_name: str) -> bool:
    """True, если это оружие / нож / перчатка / агент (а не стикер/брелок/и т.п.)."""
    name = market_hash_name.strip()
    if not name:
        return False
    for marker in JUNK_MARKERS:
        if marker in name:
            return False
    if "|" in name:          # оружие, нож/перчатка со скином, агент
        return True
    if name.startswith("★"):  # ванильный нож/перчатки без скина
        return True
    return False              # кейсы, капсулы, значки, пропуска и т.п.
