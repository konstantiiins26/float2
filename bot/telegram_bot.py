"""Telegram-бот: команды, кнопки управления и фоновый авто-скан с пушами."""
from __future__ import annotations

import asyncio
import logging

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from analysis.arbitrage import Opportunity
from bot.formatters import format_opportunity, format_summary
from config import Config
from scanner import Scanner
from sources.market_csgo import market_url

logger = logging.getLogger(__name__)

# Подписи кнопок нижней клавиатуры
BTN_SCAN = "🔎 Сканировать"
BTN_STATUS = "📟 Статус"
BTN_SETTINGS = "⚙️ Фильтры"
BTN_PAUSE = "⏯️ Авто-скан вкл/выкл"
BTN_PRESETS = "🎚️ Пресеты"

# Готовые наборы фильтров
PRESETS: dict[str, dict[str, float]] = {
    "liquid": {"min_market_volume": 40, "min_csfloat_quantity": 20, "min_profit_percent": 10},
    "risk": {"min_market_volume": 8, "min_csfloat_quantity": 4, "min_profit_percent": 25},
    "test": {"min_market_volume": 3, "min_csfloat_quantity": 2, "min_profit_percent": 5},
}

HELP_TEXT = (
    "🤖 <b>CSFloat арбитраж-бот</b>\n\n"
    "Ищу оружие, ножи, перчатки и агентов, которые можно дёшево купить и "
    "продать дороже на market.csgo, с учётом комиссий, ликвидности и "
    "стабильности цены.\n\n"
    "<b>Управляй кнопками снизу</b> 👇 или командами:\n"
    "/scan — лучшие сделки сейчас\n"
    "/status — состояние бота\n"
    "/settings — текущие фильтры\n"
    "/set имя значение — изменить фильтр (напр. <code>/set min_profit_percent 15</code>)\n"
    "/pause — пауза/возобновление авто-скана\n\n"
    "Новые сделки приходят сами. Под каждой — кнопки «Купить» и «market.csgo»."
)


def _main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(BTN_SCAN), KeyboardButton(BTN_STATUS)],
            [KeyboardButton(BTN_SETTINGS), KeyboardButton(BTN_PAUSE)],
            [KeyboardButton(BTN_PRESETS)],
        ],
        resize_keyboard=True,
    )


def _presets_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🟢 Ликвид (безопасно)", callback_data="preset:liquid")],
            [InlineKeyboardButton("🟡 Риск (больше сделок)", callback_data="preset:risk")],
            [InlineKeyboardButton("🔵 Тест (максимум)", callback_data="preset:test")],
        ]
    )


def _offer_keyboard(opp: Opportunity) -> InlineKeyboardMarkup:
    l = opp.listing
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(f"🛒 Купить ({l.source_name})", url=l.url),
                InlineKeyboardButton("📊 market.csgo", url=market_url(l.market_hash_name)),
            ],
            [InlineKeyboardButton("🙈 Скрыть", callback_data="hide")],
        ]
    )


class ArbitrageBot:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._scanner: Scanner | None = None
        self._session = None  # aiohttp.ClientSession, создаётся в post_init
        self._scan_task: asyncio.Task | None = None
        self._paused = False
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
        self.app.add_handler(CommandHandler("pause", self.cmd_pause))
        self.app.add_handler(CallbackQueryHandler(self.on_callback))
        self.app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text)
        )

    # ---- Жизненный цикл ----
    async def _on_startup(self, app: Application) -> None:
        import ssl

        import aiohttp
        import certifi

        # Используем свежий пакет корневых сертификатов certifi для всех
        # HTTPS-запросов — иначе на Windows берётся системное хранилище, где
        # может быть просроченный корень (ошибка "certificate has expired").
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        connector = aiohttp.TCPConnector(ssl=ssl_ctx)
        self._session = aiohttp.ClientSession(connector=connector)
        self._scanner = Scanner(self.cfg, self._session)
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
            if self._paused or not self.cfg.telegram_chat_id:
                continue
            try:
                fresh = await self._scanner.scan_new()
            except Exception as exc:  # noqa: BLE001
                logger.exception("Ошибка авто-скана: %s", exc)
                continue
            for opp in fresh:
                # Перед отправкой проверяем, не купили ли лот (только CSFloat).
                if self.cfg.check_live_status and opp.listing.source == "csfloat":
                    opp.live_status = await self._scanner.csfloat.get_listing_status(
                        opp.listing.listing_id
                    )
                    if opp.live_status == "sold":
                        continue
                await self._scanner.enrich_stability(opp)
                await self._send(
                    self.cfg.telegram_chat_id,
                    format_opportunity(opp),
                    reply_markup=_offer_keyboard(opp),
                )
                await asyncio.sleep(0.5)  # мягкий рейт-лимит Telegram

    async def _send(self, chat_id, text, reply_markup=None) -> None:
        try:
            await self.app.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=reply_markup,
            )
        except TelegramError as exc:
            logger.error("Не удалось отправить сообщение в %s: %s", chat_id, exc)

    # ---- Команды ----
    async def cmd_start(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_html(
            HELP_TEXT, disable_web_page_preview=True, reply_markup=_main_keyboard()
        )

    async def cmd_scan(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._scanner:
            await update.message.reply_text("Сканер ещё инициализируется, подожди пару секунд.")
            return
        await update.message.reply_text("🔎 Сканирую…")
        opportunities = await self._scanner.scan()
        if self._scanner.last_error:
            await update.message.reply_text(f"⚠️ {self._scanner.last_error}")
        await update.message.reply_html(
            format_summary(opportunities), disable_web_page_preview=True
        )
        for opp in opportunities[:5]:
            if self.cfg.check_live_status and opp.listing.source == "csfloat":
                opp.live_status = await self._scanner.csfloat.get_listing_status(
                    opp.listing.listing_id
                )
            await self._scanner.enrich_stability(opp)
            await update.message.reply_html(
                format_opportunity(opp),
                disable_web_page_preview=True,
                reply_markup=_offer_keyboard(opp),
            )

    async def cmd_status(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        market_size = 0
        if self._scanner:
            market_size = await self._scanner.market.ensure_loaded()
        chat = "задан" if self.cfg.telegram_chat_id else "НЕ задан (авто-пуши выключены)"
        auto = "⏸️ на паузе" if self._paused else "▶️ работает"
        text = (
            "📟 <b>Статус</b>\n"
            f"Авто-скан: {auto}, каждые {self.cfg.scan_interval} сек\n"
            f"Прайс-лист market.csgo: {market_size} предметов в кэше\n"
            f"Чат для оповещений: {chat}\n"
            f"CSFloat ключ: {'есть' if self.cfg.csfloat_api_key else 'нет'}\n"
            f"market.csgo ключ: {'есть' if self.cfg.market_csgo_api_key else 'нет'}\n"
            f"Skinport: {'вкл' if self.cfg.skinport_enabled else 'выкл'}\n"
            f"CS.MONEY: {'вкл' if self.cfg.csmoney_enabled else 'выкл'}\n"
            f"Только оружие/ножи/перчатки/агенты: {'да' if self.cfg.weapons_only else 'нет'}"
        )
        await update.message.reply_html(text)

    async def cmd_settings(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        c = self.cfg
        s = c.currency_symbol
        text = (
            "⚙️ <b>Фильтры</b>\n"
            f"Мин. прибыль: {c.min_profit_percent}% и {s}{c.min_profit_abs}\n"
            f"Цена покупки: {s}{c.min_buy_price} – "
            f"{(s + str(c.max_buy_price)) if c.max_buy_price else '∞'}\n"
            f"Мин. объём market.csgo: {c.min_market_volume}\n"
            f"Мин. листингов CSFloat: {c.min_csfloat_quantity}\n"
            f"Отступ флоата от границы: {c.float_edge_margin}\n"
            f"Комиссии: CSFloat {c.csfloat_fee*100:.0f}%, "
            f"market.csgo {c.market_csgo_fee*100:.0f}%\n"
            f"Листингов за проход: {c.scan_limit}, сортировка: {c.scan_sort_by}\n\n"
            "Меняй фильтры: /set имя значение (список — просто /set), "
            "или жми «🎚️ Пресеты»."
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
        ok, msg = self.cfg.set_param(args[0], args[1])
        await update.message.reply_text(msg)

    async def cmd_pause(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        self._paused = not self._paused
        state = "⏸️ Авто-скан на паузе" if self._paused else "▶️ Авто-скан возобновлён"
        await update.message.reply_text(state)

    # ---- Кнопки ----
    async def on_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        text = (update.message.text or "").strip()
        if text == BTN_SCAN:
            await self.cmd_scan(update, ctx)
        elif text == BTN_STATUS:
            await self.cmd_status(update, ctx)
        elif text == BTN_SETTINGS:
            await self.cmd_settings(update, ctx)
        elif text == BTN_PAUSE:
            await self.cmd_pause(update, ctx)
        elif text == BTN_PRESETS:
            await update.message.reply_text(
                "Выбери набор фильтров:", reply_markup=_presets_keyboard()
            )
        # прочий текст игнорируем

    async def on_callback(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        data = query.data or ""
        if data == "hide":
            await query.answer("Скрыто")
            try:
                await query.message.delete()
            except TelegramError:
                pass
            return
        if data.startswith("preset:"):
            name = data.split(":", 1)[1]
            preset = PRESETS.get(name)
            if not preset:
                await query.answer("Неизвестный пресет")
                return
            for key, value in preset.items():
                self.cfg.set_param(key, str(value))
            await query.answer("Пресет применён ✅")
            applied = ", ".join(f"{k}={v}" for k, v in preset.items())
            await query.message.reply_text(f"✅ Пресет «{name}»: {applied}")
            return
        await query.answer()

    def run(self) -> None:
        # Python 3.14 больше не создаёт event loop автоматически — создаём его
        # явно, иначе run_polling падает с "no current event loop in thread".
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.set_event_loop(asyncio.new_event_loop())
        self.app.run_polling(allowed_updates=Update.ALL_TYPES)
