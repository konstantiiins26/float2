"""Точка входа: запуск CSFloat арбитраж-бота."""
from __future__ import annotations

import logging
import sys

from bot.telegram_bot import ArbitrageBot
from config import Config


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # httpx у python-telegram-bot слишком болтливый
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main() -> int:
    setup_logging()
    cfg = Config.from_env()

    problems = cfg.validate()
    for p in problems:
        logging.warning("Конфигурация: %s", p)

    if not cfg.telegram_bot_token:
        logging.error(
            "TELEGRAM_BOT_TOKEN обязателен. Скопируй .env.example в .env и заполни."
        )
        return 1

    bot = ArbitrageBot(cfg)
    logging.info("Запускаю бота…")
    bot.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
