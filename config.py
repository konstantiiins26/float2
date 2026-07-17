"""Конфигурация бота: читается из переменных окружения (.env).

Часть параметров-фильтров можно менять на лету командой /set в Telegram —
такие изменения сохраняются в файл OVERRIDES_FILE и переживают перезапуск
(накладываются поверх значений из .env)."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

OVERRIDES_FILE = Path(os.getenv("OVERRIDES_FILE", "runtime_settings.json"))


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


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


# Параметры, которые можно менять из чата: имя -> (тип, минимум, максимум, подпись)
SETTABLE: dict[str, tuple[type, float, float | None, str]] = {
    "min_profit_percent": (float, 0, None, "мин. прибыль, %"),
    "min_profit_abs": (float, 0, None, "мин. прибыль, $"),
    "min_buy_price": (float, 0, None, "мин. цена покупки, $"),
    "max_buy_price": (float, 0, None, "макс. цена покупки, $ (0 = ∞)"),
    "min_market_volume": (float, 0, None, "мин. объём market.csgo"),
    "min_csfloat_quantity": (float, 0, None, "мин. листингов CSFloat"),
    "float_edge_margin": (float, 0, 0.5, "отступ флоата от границы износа"),
    "scan_interval": (int, 10, 86400, "интервал авто-скана, сек"),
    "csfloat_fee": (float, 0, 1, "комиссия CSFloat (доля)"),
    "market_csgo_fee": (float, 0, 1, "комиссия market.csgo (доля)"),
}


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

    # Валюта отображения и курс конвертации цен CSFloat (они приходят в USD)
    currency: str = "EUR"
    usd_rate: float = 0.92  # сколько единиц currency в 1 USD (для CSFloat)

    # Что исключать из выдачи
    exclude_stickers: bool = True    # стикеры (Sticker | ...)
    exclude_souvenir: bool = True    # сувенирное оружие

    # Проверять актуальность лота (куплен/нет) перед отправкой оффера
    check_live_status: bool = True

    # Анализировать стабильность цены по истории продаж
    analyze_stability: bool = True

    # Экспериментально: искать предметы ещё и на CS.MONEY (может блокироваться)
    csmoney_enabled: bool = False

    # Переопределения, заданные из чата (в память + файл)
    overrides: dict[str, float] = field(default_factory=dict)

    @property
    def currency_symbol(self) -> str:
        return {"EUR": "€", "USD": "$", "RUB": "₽"}.get(self.currency, self.currency + " ")

    @classmethod
    def from_env(cls) -> "Config":
        cfg = cls(
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
        cfg.currency = (os.getenv("CURRENCY", "EUR").strip().upper() or "EUR")
        cfg.usd_rate = _get_float("USD_RATE", 1.0 if cfg.currency == "USD" else 0.92)
        cfg.exclude_stickers = _get_bool("EXCLUDE_STICKERS", True)
        cfg.exclude_souvenir = _get_bool("EXCLUDE_SOUVENIR", True)
        cfg.check_live_status = _get_bool("CHECK_LIVE_STATUS", True)
        cfg.analyze_stability = _get_bool("ANALYZE_STABILITY", True)
        cfg.csmoney_enabled = _get_bool("CSMONEY_ENABLED", False)
        cfg._load_overrides()
        return cfg

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

    # ---- Runtime-настройки из чата ----
    def set_param(self, key: str, raw_value: str) -> tuple[bool, str]:
        """Меняет параметр на лету. Возвращает (успех, сообщение для пользователя)."""
        key = key.strip().lower()
        if key not in SETTABLE:
            return False, "Неизвестный параметр. Список: /set без аргументов."
        typ, lo, hi, label = SETTABLE[key]
        try:
            value = typ(float(raw_value.replace(",", ".")))
        except (TypeError, ValueError):
            return False, f"«{raw_value}» — не число."
        if value < lo:
            return False, f"{key} не может быть меньше {lo}."
        if hi is not None and value > hi:
            return False, f"{key} не может быть больше {hi}."
        setattr(self, key, value)
        self.overrides[key] = value
        self._save_overrides()
        return True, f"✅ {key} ({label}) = {value}"

    def settable_help(self) -> str:
        lines = ["Меняемые параметры (<code>/set имя значение</code>):"]
        for name, (_typ, lo, hi, label) in SETTABLE.items():
            cur = getattr(self, name)
            rng = f"≥{lo}" + (f", ≤{hi}" if hi is not None else "")
            lines.append(f"• <code>{name}</code> = {cur} — {label} ({rng})")
        return "\n".join(lines)

    def _load_overrides(self) -> None:
        try:
            if OVERRIDES_FILE.exists():
                data = json.loads(OVERRIDES_FILE.read_text())
                for key, value in data.items():
                    if key in SETTABLE:
                        typ = SETTABLE[key][0]
                        setattr(self, key, typ(value))
                        self.overrides[key] = typ(value)
                if self.overrides:
                    logger.info("Загружены runtime-настройки: %s", self.overrides)
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("Не удалось прочитать %s: %s", OVERRIDES_FILE, exc)

    def _save_overrides(self) -> None:
        try:
            OVERRIDES_FILE.write_text(json.dumps(self.overrides, ensure_ascii=False))
        except OSError as exc:
            logger.warning("Не удалось записать %s: %s", OVERRIDES_FILE, exc)
