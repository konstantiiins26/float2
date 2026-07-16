"""Конфигурация бота: читается из переменных окружения (.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _get_int(name: str, default: int) -> int:
    return int(_get_float(name, default))


@dataclass
class Config:
    # Telegram
    telegram_bot_token: str
    telegram_chat_id: str

    # API-ключи
    csfloat_api_key: str
    market_csgo_api_key: str

    # Сканирование
    scan_interval: int
    scan_limit: int
    scan_sort_by: str

    # Фильтры выгоды
    min_profit_percent: float
    min_profit_abs: float
    max_buy_price: float
    min_buy_price: float

    # Ликвидность / стабильность
    min_market_volume: float
    min_csfloat_quantity: float
    float_edge_margin: float

    # Комиссии
    csfloat_fee: float
    market_csgo_fee: float

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            csfloat_api_key=os.getenv("CSFLOAT_API_KEY", "").strip(),
            market_csgo_api_key=os.getenv("MARKET_CSGO_API_KEY", "").strip(),
            scan_interval=_get_int("SCAN_INTERVAL", 60),
            scan_limit=min(_get_int("SCAN_LIMIT", 50), 50),
            scan_sort_by=os.getenv("SCAN_SORT_BY", "most_recent").strip() or "most_recent",
            min_profit_percent=_get_float("MIN_PROFIT_PERCENT", 10.0),
            min_profit_abs=_get_float("MIN_PROFIT_ABS", 0.5),
            max_buy_price=_get_float("MAX_BUY_PRICE", 0.0),
            min_buy_price=_get_float("MIN_BUY_PRICE", 1.0),
            min_market_volume=_get_float("MIN_MARKET_VOLUME", 20.0),
            min_csfloat_quantity=_get_float("MIN_CSFLOAT_QUANTITY", 10.0),
            float_edge_margin=_get_float("FLOAT_EDGE_MARGIN", 0.01),
            csfloat_fee=_get_float("CSFLOAT_FEE", 0.02),
            market_csgo_fee=_get_float("MARKET_CSGO_FEE", 0.05),
        )

    def validate(self) -> list[str]:
        """Возвращает список проблем конфигурации (пустой = всё ок)."""
        problems: list[str] = []
        if not self.telegram_bot_token:
            problems.append("TELEGRAM_BOT_TOKEN не задан — бот не сможет запуститься.")
        if not self.telegram_chat_id:
            problems.append(
                "TELEGRAM_CHAT_ID не задан — авто-оповещения некуда слать "
                "(команды в личке будут работать после /start)."
            )
        return problems
