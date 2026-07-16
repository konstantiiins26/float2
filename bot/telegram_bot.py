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
    "/set имя значение — изменить фильтр на лету (напр. <code>/set min_profit_percent 15</code>)\n"
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
        self.app.add_handler(CommandHandler("set", self.cmd_set))

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
                # Перед отправкой проверяем, не купили ли лот. Если куплен —
                # не шлём (мёртвый оффер). Если проверить не удалось — шлём.
                if self.cfg.check_live_status:
                    opp.live_status = await self._scanner.csfloat.get_listing_status(
                        opp.listing.listing_id
                    )
                    if opp.live_status == "sold":
                        continue
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
        # Детально топ-5 (со свежей проверкой актуальности)
        for opp in opportunities[:5]:
            if self.cfg.check_live_status:
                opp.live_status = await self._scanner.csfloat.get_listing_status(
                    opp.listing.listing_id
                )
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
            "Многие фильтры можно менять прямо в чате: /set имя значение "
            "(список — просто /set)."
        )
        await update.message.reply_html(text)

    async def cmd_set(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        args = ctx.args or []
        if len(args) < 2:
            await update.message.reply_html(
                self.cfg.settable_help()
                + "\n\nПример: <code>/set min_profit_percent 15</code>"
            )
            return
        key, raw = args[0], args[1]
        ok, msg = self.cfg.set_param(key, raw)
        await update.message.reply_text(msg)

    def run(self) -> None:
        # Python 3.14 больше не создаёт event loop автоматически — создаём его
        # явно, иначе run_polling падает с "no current event loop in thread".
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.set_event_loop(asyncio.new_event_loop())
        self.app.run_polling(allowed_updates=Update.ALL_TYPES)
