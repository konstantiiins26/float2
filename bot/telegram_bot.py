"""Telegram-бот: команды, меню-кнопки и фоновый авто-скан с пушами."""
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

# Подписи нижней клавиатуры
BTN_SCAN = "🔎 Сканировать"
BTN_MENU = "📋 Меню"
BTN_STATUS = "📟 Статус"
BTN_PAUSE = "⏯️ Авто-скан"

# Готовые наборы фильтров
PRESETS: dict[str, dict[str, float]] = {
    "liquid": {"min_market_volume": 40, "min_csfloat_quantity": 20, "min_profit_percent": 10},
    "risk": {"min_market_volume": 8, "min_csfloat_quantity": 4, "min_profit_percent": 25},
    "test": {"min_market_volume": 3, "min_csfloat_quantity": 2, "min_profit_percent": 5},
}

# Фильтры, которые крутятся кнопками ➖/➕: (ключ, подпись, шаг)
FILTER_ADJUST = [
    ("min_profit_percent", "Прибыль %", 1),
    ("min_profit_abs", "Прибыль €", 0.5),
    ("min_market_volume", "Объём market.csgo", 5),
    ("min_csfloat_quantity", "Лоты CSFloat", 5),
    ("min_buy_price", "Мин. цена €", 1),
    ("max_buy_price", "Макс. цена € (0=∞)", 5),
]

HELP_TEXT = (
    "🤖 <b>CSFloat арбитраж-бот</b>\n\n"
    "Ищу оружие, ножи, перчатки и агентов, которые можно дёшево купить "
    "(CSFloat / Skinport) и продать дороже на market.csgo — с учётом комиссий, "
    "ликвидности и стабильности цены.\n\n"
    "Управляй кнопками снизу 👇 или командой /menu.\n"
    "Под каждой сделкой — кнопки «Купить», «market.csgo», «Скрыть»."
)


# ---- Клавиатуры ----
def _main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(BTN_SCAN), KeyboardButton(BTN_MENU)],
            [KeyboardButton(BTN_STATUS), KeyboardButton(BTN_PAUSE)],
        ],
        resize_keyboard=True,
    )


def _menu_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔎 Сканировать", callback_data="act:scan")],
            [
                InlineKeyboardButton("📟 Статус", callback_data="act:status"),
                InlineKeyboardButton("⚙️ Фильтры", callback_data="act:settings"),
            ],
            [
                InlineKeyboardButton("🎚️ Пресеты", callback_data="menu:presets"),
                InlineKeyboardButton("🎛️ Настроить", callback_data="menu:filters"),
            ],
            [
                InlineKeyboardButton("🔌 Источники", callback_data="menu:sources"),
                InlineKeyboardButton("⏯️ Авто-скан", callback_data="act:pause"),
            ],
        ]
    )


def _menu_presets_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🟢 Ликвид (безопасно)", callback_data="preset:liquid")],
            [InlineKeyboardButton("🟡 Риск (больше сделок)", callback_data="preset:risk")],
            [InlineKeyboardButton("🔵 Тест (максимум)", callback_data="preset:test")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="menu:main")],
        ]
    )


def _menu_filters_kb(cfg: Config) -> InlineKeyboardMarkup:
    rows = []
    for key, label, step in FILTER_ADJUST:
        val = getattr(cfg, key)
        val_str = f"{val:g}"
        rows.append(
            [
                InlineKeyboardButton("➖", callback_data=f"set:{key}:-{step}"),
                InlineKeyboardButton(f"{label}: {val_str}", callback_data="noop"),
                InlineKeyboardButton("➕", callback_data=f"set:{key}:{step}"),
            ]
        )
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="menu:main")])
    return InlineKeyboardMarkup(rows)


def _menu_sources_kb(cfg: Config) -> InlineKeyboardMarkup:
    def s(b: bool) -> str:
        return "🟢 вкл" if b else "🔴 выкл"

    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"🟦 CSFloat: {s(cfg.csfloat_enabled)}", callback_data="toggle:csfloat")],
            [InlineKeyboardButton(f"🟧 Skinport: {s(cfg.skinport_enabled)}", callback_data="toggle:skinport")],
            [InlineKeyboardButton(f"🟥 CS.MONEY: {s(cfg.csmoney_enabled)}", callback_data="toggle:csmoney")],
            [InlineKeyboardButton(f"Только оружие/ножи/агенты: {s(cfg.weapons_only)}", callback_data="toggle:weapons")],
            [InlineKeyboardButton(f"Проверка «куплен?»: {s(cfg.check_live_status)}", callback_data="toggle:status")],
            [InlineKeyboardButton(f"Анализ стабильности: {s(cfg.analyze_stability)}", callback_data="toggle:stability")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="menu:main")],
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


MENU_TEXTS = {
    "main": "📋 <b>Меню</b>\nВыбери действие:",
    "presets": "🎚️ <b>Пресеты фильтров</b>\nОдин тап — готовый набор настроек:",
    "filters": "🎛️ <b>Настройка фильтров</b>\nЖми ➖/➕, чтобы менять значения:",
    "sources": "🔌 <b>Источники и функции</b>\nТап по строке — включить/выключить:",
}


class ArbitrageBot:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._scanner: Scanner | None = None
        self._session = None
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
        self.app.add_handler(CommandHandler("menu", self.cmd_menu))
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
                await asyncio.sleep(0.5)

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

    # ---- Тексты для команд/кнопок ----
    async def _status_text(self) -> str:
        market_size = 0
        if self._scanner:
            market_size = await self._scanner.market.ensure_loaded()
        chat = "задан" if self.cfg.telegram_chat_id else "НЕ задан"
        auto = "⏸️ на паузе" if self._paused else "▶️ работает"
        return (
            "📟 <b>Статус</b>\n"
            f"Авто-скан: {auto}, каждые {self.cfg.scan_interval} сек\n"
            f"Прайс-лист market.csgo: {market_size} предметов\n"
            f"Чат для оповещений: {chat}\n"
            f"Покупка: CSFloat {'вкл' if self.cfg.csfloat_enabled else 'выкл'} | "
            f"Skinport {'вкл' if self.cfg.skinport_enabled else 'выкл'} | "
            f"CS.MONEY {'вкл' if self.cfg.csmoney_enabled else 'выкл'}\n"
            f"Только оружие/ножи/перчатки/агенты: {'да' if self.cfg.weapons_only else 'нет'}"
        )

    def _settings_text(self) -> str:
        c = self.cfg
        cur = c.currency_symbol
        return (
            "⚙️ <b>Фильтры</b>\n"
            f"Мин. прибыль: {c.min_profit_percent}% и {cur}{c.min_profit_abs}\n"
            f"Цена покупки: {cur}{c.min_buy_price} – "
            f"{(cur + str(c.max_buy_price)) if c.max_buy_price else '∞'}\n"
            f"Мин. объём market.csgo: {c.min_market_volume}\n"
            f"Мин. листингов CSFloat: {c.min_csfloat_quantity}\n"
            f"Отступ флоата от границы: {c.float_edge_margin}\n"
            f"Комиссии: CSFloat {c.csfloat_fee*100:.0f}%, market.csgo {c.market_csgo_fee*100:.0f}%\n"
            f"Валюта: {c.currency}\n\n"
            "Меняй кнопками в /menu → 🎛️ Настроить, пресетами, или /set имя значение."
        )

    async def _scan_to(self, message) -> None:
        if not self._scanner:
            await message.reply_text("Сканер ещё инициализируется, подожди пару секунд.")
            return
        await message.reply_text("🔎 Сканирую…")
        opportunities = await self._scanner.scan()
        if self._scanner.last_error:
            await message.reply_text(f"⚠️ {self._scanner.last_error}")
        await message.reply_html(
            format_summary(opportunities), disable_web_page_preview=True
        )
        for opp in opportunities[:5]:
            if self.cfg.check_live_status and opp.listing.source == "csfloat":
                opp.live_status = await self._scanner.csfloat.get_listing_status(
                    opp.listing.listing_id
                )
            await self._scanner.enrich_stability(opp)
            await message.reply_html(
                format_opportunity(opp),
                disable_web_page_preview=True,
                reply_markup=_offer_keyboard(opp),
            )

    # ---- Команды ----
    async def cmd_start(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_html(
            HELP_TEXT, disable_web_page_preview=True, reply_markup=_main_keyboard()
        )
        await update.message.reply_html(MENU_TEXTS["main"], reply_markup=_menu_main_kb())

    async def cmd_menu(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_html(MENU_TEXTS["main"], reply_markup=_menu_main_kb())

    async def cmd_scan(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._scan_to(update.message)

    async def cmd_status(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_html(await self._status_text())

    async def cmd_settings(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_html(self._settings_text())

    async def cmd_set(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        args = ctx.args or []
        if len(args) < 2:
            await update.message.reply_html(
                self.cfg.settable_help()
                + "\n\nПример: <code>/set min_profit_percent 15</code>"
            )
            return
        _ok, msg = self.cfg.set_param(args[0], args[1])
        await update.message.reply_text(msg)

    async def cmd_pause(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        self._paused = not self._paused
        state = "⏸️ Авто-скан на паузе" if self._paused else "▶️ Авто-скан возобновлён"
        await update.message.reply_text(state)

    # ---- Нижняя клавиатура (текстовые кнопки) ----
    async def on_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        text = (update.message.text or "").strip()
        if text == BTN_SCAN:
            await self._scan_to(update.message)
        elif text == BTN_MENU:
            await update.message.reply_html(MENU_TEXTS["main"], reply_markup=_menu_main_kb())
        elif text == BTN_STATUS:
            await update.message.reply_html(await self._status_text())
        elif text == BTN_PAUSE:
            await self.cmd_pause(update, ctx)

    # ---- Инлайн-меню (навигация кнопками) ----
    async def _edit_menu(self, query, name: str) -> None:
        kb = {
            "main": _menu_main_kb(),
            "presets": _menu_presets_kb(),
            "filters": _menu_filters_kb(self.cfg),
            "sources": _menu_sources_kb(self.cfg),
        }[name]
        try:
            await query.edit_message_text(
                MENU_TEXTS[name], parse_mode=ParseMode.HTML, reply_markup=kb
            )
        except TelegramError:
            pass  # напр. "message is not modified" — игнорируем

    async def on_callback(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        data = query.data or ""

        if data == "noop":
            await query.answer()
            return

        if data == "hide":
            await query.answer("Скрыто")
            try:
                await query.message.delete()
            except TelegramError:
                pass
            return

        if data.startswith("menu:"):
            await query.answer()
            await self._edit_menu(query, data.split(":", 1)[1])
            return

        if data == "act:scan":
            await query.answer("Сканирую…")
            await self._scan_to(query.message)
            return

        if data == "act:status":
            await query.answer()
            await query.message.reply_html(await self._status_text())
            return

        if data == "act:settings":
            await query.answer()
            await query.message.reply_html(self._settings_text())
            return

        if data == "act:pause":
            self._paused = not self._paused
            await query.answer("⏸️ Пауза" if self._paused else "▶️ Работает")
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

        if data.startswith("set:"):
            _, key, delta = data.split(":", 2)
            try:
                new_val = getattr(self.cfg, key) + float(delta)
            except (AttributeError, ValueError):
                await query.answer("Ошибка")
                return
            self.cfg.set_param(key, str(round(max(new_val, 0), 2)))
            await query.answer(f"{key} = {getattr(self.cfg, key):g}")
            await self._edit_menu(query, "filters")
            return

        if data.startswith("toggle:"):
            what = data.split(":", 1)[1]
            attr = {
                "csfloat": "csfloat_enabled",
                "skinport": "skinport_enabled",
                "csmoney": "csmoney_enabled",
                "weapons": "weapons_only",
                "status": "check_live_status",
                "stability": "analyze_stability",
            }.get(what)
            if attr:
                setattr(self.cfg, attr, not getattr(self.cfg, attr))
                await query.answer("Переключено")
                await self._edit_menu(query, "sources")
            else:
                await query.answer()
            return

        await query.answer()

    def run(self) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.set_event_loop(asyncio.new_event_loop())
        self.app.run_polling(allowed_updates=Update.ALL_TYPES)
