"""Telegram-бот: команды + фоновый авто-скан с пушами."""
from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

from bot.formatters import format_opportunity, format_summary
from config import Config
from scanner import Scanner

logger = logging.getLogger(__name__)

HELP_TEXT = (
    "🤖 <b>CSFloat арбитраж-бот</b>\n\n"
    "Ищу скины, которые можно дёшево купить на CSFloat и продать дороже "
    "(на CSFloat по средней цене или на market.csgo), с учётом комиссий, "
    "ликвидности и стабильности флоата.\n\n"
    "<b>Команды:</b>\n"
    "/scan — показать лучшие сделки прямо сейчас\n"
    "/status — состояние бота и текущие фильтры\n"
    "/settings — показать все параметры фильтров\n"
    "/help — эта справка\n\n"
    "Авто-оповещения приходят сами, как только появляется новая выгодная сделка."
)


class ArbitrageBot:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._scanner: Scanner | None = None
        self._session = None  # aiohttp.ClientSession, создаётся в post_init
        self._scan_task: asyncio.Task | None = None
        self.app: Application = (
            ApplicationBuilder()
            .token(cfg.telegram_bot_token)
            .post_init(self._on_startup)
            .post_shutdown(self._on_shutdown)
            .build()
        )
        self._register_handlers()

    def _register_handlers(self) -> None:
        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CommandHandler("help", self.cmd_start))
        self.app.add_handler(CommandHandler("scan", self.cmd_scan))
        self.app.add_handler(CommandHandler("status", self.cmd_status))
        self.app.add_handler(CommandHandler("settings", self.cmd_settings))

    # ---- Жизненный цикл ----
    async def _on_startup(self, app: Application) -> None:
        import aiohttp

        self._session = aiohttp.ClientSession()
        self._scanner = Scanner(self.cfg, self._session)
        # Фоновый цикл авто-скана
        self._scan_task = asyncio.create_task(self._auto_scan_loop())
        logger.info("Бот запущен, авто-скан каждые %d сек.", self.cfg.scan_interval)

    async def _on_shutdown(self, app: Application) -> None:
        if self._scan_task:
            self._scan_task.cancel()
        if self._session:
            await self._session.close()

    # ---- Фоновый цикл ----
    async def _auto_scan_loop(self) -> None:
        # Первый прогон помечает уже висящие сделки как «виденные», чтобы при
        # старте не спамить всей выдачей. Пуши идут только по новым листингам.
        assert self._scanner is not None
        try:
            await self._scanner.scan_new()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Ошибка первичного скана: %s", exc)

        while True:
            await asyncio.sleep(self.cfg.scan_interval)
            if not self.cfg.telegram_chat_id:
                continue
            try:
                fresh = await self._scanner.scan_new()
            except Exception as exc:  # noqa: BLE001
                logger.exception("Ошибка авто-скана: %s", exc)
                continue
            for opp in fresh:
                await self._send(self.cfg.telegram_chat_id, format_opportunity(opp))
                await asyncio.sleep(0.5)  # мягкий рейт-лимит Telegram

    async def _send(self, chat_id: str | int, text: str) -> None:
        try:
            await self.app.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except TelegramError as exc:
            logger.error("Не удалось отправить сообщение в %s: %s", chat_id, exc)

    # ---- Команды ----
    async def cmd_start(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_html(HELP_TEXT, disable_web_page_preview=True)

    async def cmd_scan(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._scanner:
            await update.message.reply_text("Сканер ещё инициализируется, попробуй через пару секунд.")
            return
        await update.message.reply_text("🔎 Сканирую CSFloat…")
        opportunities = await self._scanner.scan()
        if self._scanner.last_error:
            await update.message.reply_text(f"⚠️ {self._scanner.last_error}")
        # Сводка
        await update.message.reply_html(
            format_summary(opportunities), disable_web_page_preview=True
        )
        # Детально топ-5
        for opp in opportunities[:5]:
            await update.message.reply_html(
                format_opportunity(opp), disable_web_page_preview=True
            )

    async def cmd_status(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        market_size = 0
        if self._scanner:
            market_size = await self._scanner.market.ensure_loaded()
        chat = "задан" if self.cfg.telegram_chat_id else "НЕ задан (авто-пуши выключены)"
        text = (
            "📟 <b>Статус</b>\n"
            f"Авто-скан каждые: {self.cfg.scan_interval} сек\n"
            f"Прайс-лист market.csgo: {market_size} предметов в кэше\n"
            f"Чат для оповещений: {chat}\n"
            f"CSFloat ключ: {'есть' if self.cfg.csfloat_api_key else 'нет (публичный доступ)'}\n"
            f"market.csgo ключ: {'есть' if self.cfg.market_csgo_api_key else 'нет'}"
        )
        await update.message.reply_html(text)

    async def cmd_settings(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        c = self.cfg
        text = (
            "⚙️ <b>Фильтры</b>\n"
            f"Мин. прибыль: {c.min_profit_percent}% и ${c.min_profit_abs}\n"
            f"Цена покупки: ${c.min_buy_price} – "
            f"{('$' + str(c.max_buy_price)) if c.max_buy_price else '∞'}\n"
            f"Мин. объём market.csgo: {c.min_market_volume}\n"
            f"Мин. листингов CSFloat: {c.min_csfloat_quantity}\n"
            f"Отступ флоата от границы: {c.float_edge_margin}\n"
            f"Комиссии: CSFloat {c.csfloat_fee*100:.0f}%, "
            f"market.csgo {c.market_csgo_fee*100:.0f}%\n"
            f"Листингов за проход: {c.scan_limit}, сортировка: {c.scan_sort_by}\n\n"
            "Параметры меняются в файле .env (перезапусти бота после изменений)."
        )
        await update.message.reply_html(text)

    def run(self) -> None:
        self.app.run_polling(allowed_updates=Update.ALL_TYPES)
