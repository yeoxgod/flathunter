from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from telegram import InlineKeyboardButton as B
from telegram import InlineKeyboardMarkup, LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .analyzer import Analyzer
from .db import DB
from .links import apply_auto_links
from .llm import build_llm
from .pipeline import Pipeline, listing_from_url
from .sources.textparse import listings_from_text
from .render import render_card, wa_link
from .sender import CHANNEL_NAMES, BrowserSender, SendJob, SendManager, SendResult, choose_channel
from .settings import Settings, load_secrets
from .sources import olx as olx_mod
from .sources import otodom as otodom_mod
from .sources.base import Fetcher

log = logging.getLogger(__name__)
URL_RE = re.compile(r"https?://[^\s<>\"']+")

INSTR = {
    "short": "Make it noticeably shorter (about 45-60 words) and keep the key questions.",
    "formal": "Make the tone more formal and polite.",
    "casual": "Make the tone friendlier and more relaxed, still polite.",
}

HELP = (
    "<b>Как пользоваться</b>\n"
    "/criteria — что ищешь (пиши обычным текстом)\n"
    "/me — данные о себе для сообщений\n"
    "/links — ссылки на выдачу Otodom и OLX\n"
    "/settings — режим отправки, тест-режим, лимиты, источники\n"
    "/login — открыть окно браузера и войти в WhatsApp, OLX, Otodom\n"
    "/backfill — прислать подходящие за последние 2 суток (/backfill 24 — за сутки)\n"
    "/status — статистика, /poll — проверить сейчас, /pause и /resume\n"
    "/update — обновить бота до последней версии\n\n"
    "<b>Как получать объявления</b>\n"
    "• Перешли мне сообщения от @thewarsawrentbot (можно пачкой): разберу, отсею неподходящие, пришлю карточки.\n"
    "• Полный автомат: создай закрытый канал, добавь туда меня админом и настрой @thewarsawrentbot "
    "присылать объявления в этот канал (если он так умеет). Я спрошу подтверждение и буду читать канал сам.\n"
    "Можно прислать ссылку на любое объявление OLX/Otodom: разберу его сразу.\n"
    "Ответь (reply) на карточку текстом, например «добавь, что заедем с 1 декабря», и я перепишу черновик."
)


# --- форматирование -------------------------------------------------------------
def fmt_criteria(s: dict) -> str:
    out = []
    if s.get("max_price") or s.get("min_price"):
        lo, hi = s.get("min_price") or 0, s.get("max_price") or 0
        out.append(f"💰 {'от ' + str(lo) + ' ' if lo else ''}{'до ' + str(hi) if hi else ''} zł (с czynsz/media)")
    if s.get("min_area") or s.get("max_area"):
        lo, hi = s.get("min_area") or 0, s.get("max_area") or 0
        out.append(f"📐 {'от ' + str(lo) + ' ' if lo else ''}{'до ' + str(hi) if hi else ''} м²")
    if s.get("rooms"):
        out.append("🚪 комнат: " + ", ".join(map(str, s["rooms"])))
    if s.get("districts"):
        out.append("📍 " + ", ".join(s["districts"]))
    if s.get("exclude_keywords"):
        out.append("🚫 стоп-слова: " + ", ".join(s["exclude_keywords"]))
    if s.get("wishes"):
        out.append("💭 " + s["wishes"])
    return html.escape("\n".join(out)) or "<i>не заданы</i>"


PROFILE_LABELS = {
    "name": "Имя", "about": "О себе", "move_in": "Заезд", "lease": "Срок", "occupants": "Жильцы",
    "pets": "Животные", "extra": "Ещё", "email": "Email (для Otodom)", "phone": "Телефон (для Otodom)",
}


def fmt_profile(p: dict) -> str:
    lines = [f"{PROFILE_LABELS[k]}: {html.escape(v)}" for k, v in p.items() if v and k in PROFILE_LABELS]
    return "\n".join(lines) or "<i>пусто</i>"


def on_off(v) -> str:
    return "✅" if v else "❌"


def settings_view(s: dict) -> tuple[str, InlineKeyboardMarkup]:
    snd = s["send"]
    src = s["sources"]
    ch = snd["channels"]
    h0, h1 = snd["hours"][:2]
    text = (
        "<b>Настройки</b>\n"
        f"Режим отправки: <b>{'🤖 автомат' if snd['mode'] == 'auto' else '👆 по кнопке'}</b>\n"
        f"Тест-режим (вставляет текст, не отправляет): <b>{'ВКЛ' if snd['dry_run'] else 'выкл'}</b>\n"
        f"Карточки от оценки: <b>{s['min_score']}</b>\n"
        f"Автоотправка от оценки: <b>{snd['auto_min_score']}</b>, без красных флагов, "
        f"через {snd['auto_delay_minutes']} мин после карточки\n"
        f"Лимит отправок в день: <b>{snd['daily_limit']}</b>\n"
        f"Часы автоотправки: <b>{h0}–{h1}</b> (изменить: /hours 8 22)\n"
        f"Проверка каждые <b>{s['poll_interval_minutes']}</b> мин"
    )
    kb = [
        [B(f"Режим: {'автомат' if snd['mode'] == 'auto' else 'по кнопке'} ⇄", callback_data="s:mode")],
        [B(f"Тест-режим: {'ВКЛ' if snd['dry_run'] else 'выкл'} ⇄", callback_data="s:dry")],
        [B("Карточки −", callback_data="s:min:-1"), B(f"{s['min_score']}", callback_data="s:noop"), B("+", callback_data="s:min:1")],
        [B("Авто от −", callback_data="s:auto:-1"), B(f"{snd['auto_min_score']}", callback_data="s:noop"), B("+", callback_data="s:auto:1")],
        [B("Лимит −", callback_data="s:limit:-5"), B(f"{snd['daily_limit']}/день", callback_data="s:noop"), B("+", callback_data="s:limit:5")],
        [B("Интервал −", callback_data="s:int:-1"), B(f"{s['poll_interval_minutes']} мин", callback_data="s:noop"), B("+", callback_data="s:int:1")],
        [B(f"{on_off(ch['whatsapp'])} WhatsApp", callback_data="s:ch:whatsapp"),
         B(f"{on_off(ch['olx'])} чат OLX", callback_data="s:ch:olx"),
         B(f"{on_off(ch['otodom'])} форма Otodom", callback_data="s:ch:otodom")],
        [B(f"{on_off(src['otodom']['enabled'])} Otodom", callback_data="s:src:otodom"),
         B(f"{on_off(src['olx']['enabled'])} OLX", callback_data="s:src:olx"),
         B(f"{on_off(src['channel']['enabled'])} канал TG", callback_data="s:src:channel")],
        [B(f"{on_off(src['tgbot']['enabled'])} чат @{src['tgbot']['chat']} (нужен перезапуск)", callback_data="s:src:tgbot")],
    ]
    return text, InlineKeyboardMarkup(kb)


def card_keyboard(row_id: int, listing: dict, draft: str, status: str, channel: str | None, pending: bool) -> InlineKeyboardMarkup:
    top = [B("🔗 Объявление", url=listing["url"])]
    phones = listing.get("phones") or []
    if phones:
        top.append(B("💬 Открыть WhatsApp", url=wa_link(phones[0], draft)))
    rows = [top]
    if status == "sent":
        if pending:
            rows.append([B("⛔ Отменить автоотправку", callback_data=f"a:cancel:{row_id}")])
        elif channel:
            rows.append([B(f"📤 Отправить в {CHANNEL_NAMES[channel]}", callback_data=f"a:send:{row_id}")])
        rows.append([
            B("✂️ Короче", callback_data=f"a:short:{row_id}"),
            B("🎩 Формальнее", callback_data=f"a:formal:{row_id}"),
            B("😊 Проще", callback_data=f"a:casual:{row_id}"),
        ])
        rows.append([
            B("✅ Написал сам", callback_data=f"a:done:{row_id}"),
            B("🚫 Пропустить", callback_data=f"a:skip:{row_id}"),
        ])
    return InlineKeyboardMarkup(rows)


def preview(listing: dict) -> LinkPreviewOptions:
    return LinkPreviewOptions(url=listing["url"], prefer_large_media=True, show_above_text=True)


def classify_url(url: str) -> str | None:
    p = urlparse(url)
    host, path = p.netloc.lower(), p.path
    if host.endswith("otodom.pl"):
        return "otodom_listing" if "/oferta/" in path else "otodom_search" if "/wyniki/" in path else None
    if host.endswith("olx.pl"):
        return "olx_listing" if "/d/oferta/" in path else "olx_search"
    return None


# --- приложение ------------------------------------------------------------------
class FlatBot:
    def __init__(self, data_dir: str | Path = "data"):
        self.data_dir = Path(data_dir)
        self.secrets = load_secrets(self.data_dir)
        if not self.secrets.get("telegram_token") or not self.secrets.get("chat_id"):
            raise SystemExit("Бот не настроен. Запусти setup.bat.")
        self.chat_id = int(self.secrets["chat_id"])
        self.db = DB(self.data_dir / "flathunter.db")
        self.settings = Settings(self.db)
        self.llm = build_llm(self.secrets, self.data_dir)
        self.analyzer = Analyzer(self.llm, self.settings.get)
        self.fetcher = Fetcher()
        self.sender = BrowserSender(self.data_dir / "browser-profile", self.data_dir / "shots")
        self.app = (
            Application.builder()
            .token(self.secrets["telegram_token"])
            .post_init(self._post_init)
            .post_shutdown(self._post_shutdown)
            .build()
        )
        self.pipeline = Pipeline(self.db, self.settings.get, self.fetcher, self.analyzer, self.notify, self.alert)
        self.sends = SendManager(self.sender, self.db, self.settings.get, self.on_send_result)
        self.restart_requested = False
        self.watcher = None
        self._fwd_buffer: list = []
        self.pipeline.on_source_blocked = self._source_blocked
        self._register()

    # --- жизненный цикл --------------------------------------------------------
    async def _post_init(self, app: Application) -> None:
        s = self.settings.get()
        if self._has_criteria(s):
            self.settings.save(apply_auto_links(s))
        self.sends.start()
        self._schedule_poll()
        now = time.time()
        for k, v in self.db.kv_prefix("auto_pending:"):
            if v:
                row_id = int(k.split(":")[1])
                self._schedule_auto(row_id, max(30.0, float(v) - now))
        await app.bot.set_my_commands([
            ("criteria", "что ищу"), ("me", "данные о себе"), ("links", "ссылки на выдачу"),
            ("settings", "настройки и режимы"), ("login", "войти в WhatsApp/OLX/Otodom"),
            ("backfill", "подходящие за последние 2 суток"),
            ("status", "статистика"), ("poll", "проверить сейчас"), ("update", "обновить бота"), ("help", "помощь"),
        ])
        await self.alert("🟢 Бот запущен. /help — что умею.")
        await self._start_tg_watcher()
        self.app.job_queue.run_repeating(self._update_check_job, interval=6 * 3600, first=60, name="update_check")

    async def _start_tg_watcher(self) -> None:
        s = self.settings.get()
        tg = s["sources"]["tgbot"]
        self.watcher = None
        if not tg.get("enabled"):
            return
        if not (self.secrets.get("tg_api_id") and (self.data_dir / "tg_user.session").exists()):
            await self.alert(f"ℹ️ Чтобы я читал объявления из @{tg['chat']}, запусти на компьютере tg-login.bat (один раз).")
            return
        from .tguser import TgUserWatcher

        w = TgUserWatcher(self.data_dir, self.secrets["tg_api_id"], self.secrets["tg_api_hash"], tg["chat"], self._on_tg_listings)
        try:
            ok = await w.start()
        except Exception as e:
            log.exception("tg watcher")
            await self.alert(f"⚠️ Не получилось подключиться к чату @{tg['chat']}: {e}")
            return
        if not ok:
            await self.alert("⚠️ Вход в Telegram-аккаунт слетел: запусти tg-login.bat ещё раз.")
            return
        self.watcher = w
        self.pipeline.extra_backfill = w.recent
        await self.alert(f"📥 Читаю объявления из @{tg['chat']} и разбираю каждое новое.")

    # --- объявления из пересланных сообщений и каналов ----------------------------
    @staticmethod
    def message_listings(msg) -> list:
        text = msg.text or msg.caption or ""
        urls = []
        try:
            ents = msg.parse_entities() if msg.text else msg.parse_caption_entities()
            for ent, t in ents.items():
                if ent.type == "text_link" and ent.url:
                    urls.append(ent.url)
                elif ent.type == "url":
                    urls.append(t)
        except Exception:
            pass
        kb = getattr(msg.reply_markup, "inline_keyboard", None) or []
        for row in kb:
            for b in row:
                if getattr(b, "url", None):
                    urls.append(b.url)
        origin = getattr(msg, "forward_origin", None)
        dt = getattr(origin, "date", None) or msg.date
        posted = dt.astimezone().replace(tzinfo=None).isoformat(timespec="minutes") if dt else ""
        return listings_from_text(text, urls, posted)

    async def on_forward(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.ok(update) or not update.message:
            return
        items = self.message_listings(update.message)
        if not items:
            if update.message.text:
                await self.on_text(update, context)
            return
        self._fwd_buffer.extend(items)
        # пачку пересланных сообщений собираем 4 секунды и разбираем разом
        for j in self.app.job_queue.get_jobs_by_name("fwd"):
            j.schedule_removal()
        self.app.job_queue.run_once(self._flush_forwards, when=4, name="fwd")

    async def _flush_forwards(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        items, self._fwd_buffer = self._fwd_buffer, []
        if not items:
            return
        msg = await self.app.bot.send_message(self.chat_id, f"Разбираю пересланное: {len(items)} объявл.…")
        try:
            st = await self.pipeline.ingest(items)
        except Exception as e:
            log.exception("forward ingest")
            await msg.edit_text(f"Не получилось: {e}")
            return
        parts = [f"карточек {st.get('sent', 0)}"]
        for k, name in (("filtered", "не прошли фильтры"), ("lowscore", "низкая оценка"), ("already", "уже было"),
                        ("errors", "ошибок")):
            if st.get(k):
                parts.append(f"{name} {st[k]}")
        await msg.edit_text(f"Из {len(items)}: " + ", ".join(parts) + ".")

    async def on_my_member(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        cm = update.my_chat_member
        if cm is None or cm.chat.type == "private":
            return
        status = cm.new_chat_member.status
        if status not in ("administrator", "member"):
            return
        if cm.chat.type != "channel":
            await self.alert(f"ℹ️ Меня добавили в группу «{cm.chat.title}». В группах боты не видят сообщения других "
                             "ботов, поэтому объявления от бота-агрегатора лучше пересылать в канал.")
            return
        if cm.chat.id in self.settings.get()["sources"]["channels"]:
            return
        await self.app.bot.send_message(
            self.chat_id, f"Меня добавили в канал «{cm.chat.title}». Читать из него объявления?",
            reply_markup=InlineKeyboardMarkup([[B("✅ Да", callback_data=f"ch:allow:{cm.chat.id}"),
                                                B("Нет", callback_data=f"ch:deny:{cm.chat.id}")]]),
        )

    async def on_channel_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        await q.answer()
        if not self.ok(update):
            return
        _, action, cid = q.data.split(":")
        s = self.settings.get()
        chans = [c for c in s["sources"]["channels"] if c != int(cid)]
        if action == "allow":
            chans.append(int(cid))
        s["sources"]["channels"] = chans
        self.settings.save(s)
        await q.edit_message_text("✅ Читаю этот канал: каждое новое объявление разберу сам." if action == "allow"
                                  else "Ок, этот канал не читаю.")

    async def on_channel_post(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.channel_post or update.edited_channel_post
        if msg is None or msg.chat.id not in self.settings.get()["sources"]["channels"]:
            return
        items = self.message_listings(msg)
        if items and self.db.kv_get("paused") != "1":
            try:
                await self.pipeline.ingest(items)
            except Exception:
                log.exception("channel ingest")

    async def _source_blocked(self, name: str) -> None:
        self.settings.update(f"sources.{name}.enabled", False)
        await self.alert(
            f"⛔ {name.upper()} блокирует прямые запросы бота (403), обходить защиту я не буду, поэтому опрос "
            f"{name.upper()} выключил. Объявления присылай пересылкой от @thewarsawrentbot или через канал (/help)."
        )

    async def _on_tg_listings(self, items: list) -> None:
        if self.db.kv_get("paused") == "1":
            return
        try:
            await self.pipeline.ingest(items)
        except Exception:
            log.exception("ingest")

    async def _post_shutdown(self, app: Application) -> None:
        if getattr(self, "watcher", None):
            await self.watcher.stop()
        await self.sends.stop()
        await self.sender.close()
        await self.fetcher.aclose()

    def _schedule_poll(self) -> None:
        jq = self.app.job_queue
        for j in jq.get_jobs_by_name("poll"):
            j.schedule_removal()
        minutes = max(5, int(self.settings.get().get("poll_interval_minutes", 7)))
        jq.run_repeating(self._poll_job, interval=minutes * 60, first=20, name="poll")

    async def _poll_job(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.db.kv_get("paused") == "1":
            return
        try:
            await self.pipeline.cycle()
        except Exception:
            log.exception("проход упал")

    def _register(self) -> None:
        a = self.app
        for name, fn in (
            ("start", self.cmd_start), ("help", self.cmd_help), ("criteria", self.cmd_criteria), ("me", self.cmd_me),
            ("links", self.cmd_links), ("settings", self.cmd_settings), ("hours", self.cmd_hours),
            ("status", self.cmd_status), ("poll", self.cmd_poll), ("pause", self.cmd_pause),
            ("resume", self.cmd_resume), ("login", self.cmd_login), ("backfill", self.cmd_backfill), ("update", self.cmd_update),
        ):
            a.add_handler(CommandHandler(name, fn))
        a.add_handler(CallbackQueryHandler(self.on_card, pattern=r"^a:"))
        a.add_handler(CallbackQueryHandler(self.on_settings, pattern=r"^s:"))
        a.add_handler(CallbackQueryHandler(self.on_confirm, pattern=r"^[cp]:"))
        a.add_handler(CallbackQueryHandler(self.on_backfill_button, pattern=r"^bf:"))
        a.add_handler(CallbackQueryHandler(self.on_links_auto, pattern=r"^lk:"))
        a.add_handler(CallbackQueryHandler(self.on_update_button, pattern=r"^up:"))
        a.add_handler(CallbackQueryHandler(self.on_channel_confirm, pattern=r"^ch:"))
        a.add_handler(ChatMemberHandler(self.on_my_member, ChatMemberHandler.MY_CHAT_MEMBER))
        a.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POSTS, self.on_channel_post))
        a.add_handler(MessageHandler(filters.FORWARDED & ~filters.COMMAND, self.on_forward))
        a.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))

    def ok(self, update: Update) -> bool:
        chat = update.effective_chat
        return bool(chat and chat.id == self.chat_id)

    async def alert(self, text: str) -> None:
        await self.app.bot.send_message(self.chat_id, text)

    # --- карточки --------------------------------------------------------------
    def _card_parts(self, row) -> tuple[str, InlineKeyboardMarkup, dict]:
        listing = json.loads(row["listing"])
        analysis = json.loads(row["analysis"])
        pending = bool(self.db.kv_get(f"auto_pending:{row['id']}"))
        channel = choose_channel(listing, self.settings.get())
        note = self.db.kv_get(f"note:{row['id']}") or ""
        if pending and not note:
            note = f"⏳ Отправлю автоматически в {CHANNEL_NAMES.get(channel, '')} через несколько минут."
        text = render_card(listing, analysis, row["draft"], note=note)
        return text, card_keyboard(row["id"], listing, row["draft"], row["status"], channel, pending), listing

    async def refresh_card(self, row_id: int, note: str | None = None) -> None:
        if note is not None:
            self.db.kv_set(f"note:{row_id}", note)
        row = self.db.get(row_id)
        if row is None or not row["tg_message_id"]:
            return
        text, kb, listing = self._card_parts(row)
        try:
            await self.app.bot.edit_message_text(
                chat_id=self.chat_id, message_id=row["tg_message_id"], text=text,
                parse_mode=ParseMode.HTML, reply_markup=kb, link_preview_options=preview(listing),
            )
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise

    async def notify(self, row_id: int, listing: dict, analysis: dict, allow_auto: bool = True) -> None:
        s = self.settings.get()
        snd = s["send"]
        channel = choose_channel(listing, s)
        auto = (
            allow_auto and snd["mode"] == "auto" and channel is not None
            and analysis["fit_score"] >= int(snd["auto_min_score"]) and not analysis.get("red_flags")
        )
        if auto:
            delay = max(1, int(snd.get("auto_delay_minutes", 3))) * 60
            self.db.kv_set(f"auto_pending:{row_id}", str(time.time() + delay))
        row = self.db.get(row_id)
        text, kb, _ = self._card_parts(row)
        msg = await self.app.bot.send_message(
            self.chat_id, text, parse_mode=ParseMode.HTML, reply_markup=kb, link_preview_options=preview(listing),
        )
        self.db.update(row_id, tg_message_id=msg.message_id)
        if auto:
            self._schedule_auto(row_id, delay)

    def _schedule_auto(self, row_id: int, delay: float) -> None:
        self.app.job_queue.run_once(self._auto_fire, when=delay, data=row_id, name=f"auto:{row_id}")

    async def _auto_fire(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        row_id = context.job.data
        if not self.db.kv_get(f"auto_pending:{row_id}"):
            return
        self.db.kv_set(f"auto_pending:{row_id}", "")
        row = self.db.get(row_id)
        if row is None or row["status"] != "sent":
            return
        await self._enqueue(row, auto=True)

    async def _enqueue(self, row, auto: bool) -> bool:
        listing = json.loads(row["listing"])
        channel = choose_channel(listing, self.settings.get())
        if not channel:
            await self.refresh_card(row["id"], "❌ Нет доступного канала: нет телефона, а чат сайта выключен в /settings.")
            return False
        self.sends.submit(SendJob(row["id"], channel, listing, row["draft"], auto=auto))
        await self.refresh_card(row["id"], f"📤 В очереди на отправку в {CHANNEL_NAMES[channel]}…")
        return True

    async def on_send_result(self, job: SendJob, res: SendResult) -> None:
        stamp = time.strftime("%H:%M")
        if res.ok and res.dry_run:
            note = f"🧪 {res.message} ({stamp}). Тест-режим выключается в /settings."
        elif res.ok:
            note = f"✅ {res.message} ({stamp})"
        elif res.kind == "unverified":
            note = f"⚠️ {res.message} ({stamp})"
        else:
            note = f"❌ {res.message} ({stamp})"
            if res.kind == "login":
                note += " Затем нажми «Отправить» ещё раз."
        await self.refresh_card(job.row_id, note)
        row = self.db.get(job.row_id)
        reply_to = row["tg_message_id"] if row else None
        if res.screenshot and Path(res.screenshot).exists():
            with open(res.screenshot, "rb") as f:
                await self.app.bot.send_photo(self.chat_id, f, caption=res.message[:1000], reply_to_message_id=reply_to)

    async def on_card(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if not self.ok(update):
            await q.answer()
            return
        try:
            _, action, rid = q.data.split(":")
            row = self.db.get(int(rid))
        except ValueError:
            await q.answer()
            return
        if row is None:
            await q.answer("Не нашёл это объявление", show_alert=True)
            return
        listing = json.loads(row["listing"])
        if action in INSTR:
            await q.answer("Переписываю…")
            try:
                draft = await self.analyzer.redraft(listing, row["draft"], INSTR[action])
                self.db.update(row["id"], draft=draft)
                await self.refresh_card(row["id"])
            except Exception as e:
                log.exception("redraft")
                await q.message.reply_text(f"Не получилось переписать: {e}")
        elif action == "send":
            if row["status"] != "sent":
                await q.answer("Уже обработано")
                return
            await q.answer("Отправляю…")
            await self._enqueue(row, auto=False)
        elif action == "cancel":
            self.db.kv_set(f"auto_pending:{row['id']}", "")
            for j in self.app.job_queue.get_jobs_by_name(f"auto:{row['id']}"):
                j.schedule_removal()
            await q.answer("Автоотправка отменена")
            await self.refresh_card(row["id"], "⛔ Автоотправка отменена.")
        elif action == "done":
            self.db.update(row["id"], status="contacted")
            await q.answer("Отмечено")
            await self.refresh_card(row["id"], "✅ Отмечено: написал(а) сам(а)")
        elif action == "skip":
            self.db.update(row["id"], status="skipped")
            self.db.kv_set(f"auto_pending:{row['id']}", "")
            await q.answer("Пропущено")
            await q.edit_message_text(f"🚫 Пропущено: {listing.get('title', '')[:100]}")
        else:
            await q.answer()

    # --- команды ---------------------------------------------------------------
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.ok(update):
            return
        s = self.settings.get()
        steps = [
            ("Критерии поиска", bool(s["search"].get("summary_ru") or s["search"].get("max_price")), "/criteria"),
            ("Данные о себе", bool(s["profile"].get("name") or s["profile"].get("about")), "/me"),
        ]
        tg = s["sources"]["tgbot"]
        src_line = (f"\n📥 Источник: чат @{tg['chat']} — {'подключён' if getattr(self, 'watcher', None) else 'не подключён, запусти tg-login.bat'}"
                    if tg.get("enabled") else
                    "\n📥 Объявления: пересылай мне сообщения от @thewarsawrentbot (можно пачкой) или подключи канал — см. /help")
        lines = [f"{'✅' if done else '⬜️'} {name} — {cmd}" for name, done, cmd in steps]
        await update.message.reply_text(
            "Привет! Я ищу квартиры, оцениваю и пишу владельцам.\n\n<b>Настройка</b>\n" + "\n".join(lines) + src_line
            + "\n\nСейчас включён <b>тест-режим</b>: при отправке я вставляю текст, но не жму «Отправить», "
            "и присылаю скрин. Когда убедишься, что всё работает, выключи его в /settings.",
            parse_mode=ParseMode.HTML,
        )
        for name, done, cmd in steps:
            if not done:
                if cmd == "/criteria":
                    await self._ask_criteria(update, context)
                else:
                    await self._ask_me(update, context)
                break

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.ok(update):
            await update.message.reply_text(HELP, parse_mode=ParseMode.HTML)

    async def _ask_criteria(self, update, context) -> None:
        context.user_data["await"] = "criteria"
        cur = self.settings.get()["search"]
        await update.effective_chat.send_message(
            "<b>Что ищешь?</b>\nТекущие критерии:\n" + fmt_criteria(cur)
            + "\n\nНапиши обычным текстом, например:\n<i>2-3 комнаты до 5500 со всеми оплатами, Мокотув или Воля, "
            "от 35 м², с мебелью, рядом метро, без агентств</i>\n\nМожно и правку: «подними бюджет до 6000».",
            parse_mode=ParseMode.HTML,
        )

    async def _ask_me(self, update, context) -> None:
        context.user_data["await"] = "me"
        cur = self.settings.get()["profile"]
        await update.effective_chat.send_message(
            "<b>Расскажи о себе</b> (это пойдёт в сообщения владельцам, ничего сверх сказанного я не добавлю).\n"
            "Сейчас:\n" + fmt_profile(cur)
            + "\n\nНапример: <i>Меня зовут Иван, работаю программистом в Варшаве по umowa o pracę, "
            "заезд с 1 декабря, минимум на год, живу один, без животных, не курю. "
            "Email ivan@mail.com, телефон +48 600 000 000</i>\n"
            "Email и телефон нужны только для формы Otodom.",
            parse_mode=ParseMode.HTML,
        )

    async def _ask_links(self, update, context) -> None:
        context.user_data["await"] = "links"
        src = self.settings.get()["sources"]
        await update.effective_chat.send_message(
            "<b>Ссылки на выдачу</b>\n"
            f"Otodom ({'авто' if src['otodom'].get('auto', True) else 'своя'}): {html.escape(src['otodom']['search_url'] or '—')}\n\n"
            f"OLX ({'авто' if src['olx'].get('auto', True) else 'своя'}): {html.escape(src['olx']['search_url'] or '—')}\n\n"
            "Обычно ничего делать не нужно: ссылки я собираю сам из /criteria. "
            "Если хочешь свою (например, с районами на карте), выставь фильтры на сайте и пришли адрес сюда.",
            parse_mode=ParseMode.HTML, link_preview_options=LinkPreviewOptions(is_disabled=True),
            reply_markup=InlineKeyboardMarkup([[B("🔄 Собирать из критериев", callback_data="lk:auto")]]),
        )

    async def cmd_criteria(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.ok(update):
            return
        text = " ".join(context.args or []).strip()
        if text:
            await self._handle_criteria(update, context, text)
        else:
            await self._ask_criteria(update, context)

    async def cmd_me(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.ok(update):
            return
        text = " ".join(context.args or []).strip()
        if text:
            await self._handle_me(update, context, text)
        else:
            await self._ask_me(update, context)

    async def cmd_links(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.ok(update):
            return
        urls = URL_RE.findall(" ".join(context.args or []))
        if urls:
            await self._handle_links(update, urls)
        else:
            await self._ask_links(update, context)

    async def _handle_criteria(self, update, context, text: str) -> None:
        msg = await update.effective_chat.send_message("Разбираю…")
        try:
            crit = await self.analyzer.parse_criteria(text, self.settings.get()["search"])
        except Exception as e:
            log.exception("criteria")
            await msg.edit_text(f"Не получилось разобрать: {e}")
            return
        context.user_data["pending_criteria"] = crit
        context.user_data["await"] = "criteria"
        await msg.edit_text(
            f"<b>Понял так:</b> {html.escape(crit.get('summary_ru', ''))}\n\n{fmt_criteria(crit)}\n\n"
            "Сохранить? Или напиши, что поправить.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[B("✅ Сохранить", callback_data="c:save"), B("Отмена", callback_data="c:cancel")]]),
        )

    async def _handle_me(self, update, context, text: str) -> None:
        msg = await update.effective_chat.send_message("Разбираю…")
        try:
            prof = await self.analyzer.parse_profile(text, self.settings.get()["profile"])
        except Exception as e:
            log.exception("profile")
            await msg.edit_text(f"Не получилось разобрать: {e}")
            return
        context.user_data["pending_profile"] = prof
        context.user_data["await"] = "me"
        await msg.edit_text(
            f"<b>Запомню так:</b>\n{fmt_profile(prof)}\n\nСохранить? Или напиши, что поправить.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[B("✅ Сохранить", callback_data="p:save"), B("Отмена", callback_data="p:cancel")]]),
        )

    async def _handle_links(self, update, urls: list[str]) -> None:
        report = []
        for url in urls:
            kind = classify_url(url)
            if kind not in ("otodom_search", "olx_search"):
                report.append(f"⚠️ Не похоже на выдачу Otodom/OLX: {html.escape(url[:80])}")
                continue
            name, mod = ("otodom", otodom_mod) if kind == "otodom_search" else ("olx", olx_mod)
            self.settings.update(f"sources.{name}.search_url", url)
            self.settings.update(f"sources.{name}.enabled", True)
            self.settings.update(f"sources.{name}.auto", False)
            try:
                n = len(mod.parse_search(await self.fetcher.get(url)))
                check = f"нашёл {n} объявлений" if n else "⚠️ объявлений не нашлось (проверь фильтры или пришли скрин мне)"
            except Exception as e:
                check = f"⚠️ не загрузилось: {html.escape(str(e)[:120])}"
            report.append(f"✅ {name.upper()} сохранён: {check}")
        await update.effective_chat.send_message("\n".join(report) or "Ссылок не нашёл.", parse_mode=ParseMode.HTML)
        s = self.settings.get()
        if any(u.startswith("✅") for u in report):
            await update.effective_chat.send_message(
                "Ссылки сохранены. Дальше буду присылать новые объявления сами.\n"
                "Прислать подходящие из того, что уже опубликовано?",
                reply_markup=InlineKeyboardMarkup([[B("За 2 суток", callback_data="bf:48"), B("За сутки", callback_data="bf:24"),
                                                    B("Не надо", callback_data="bf:0")]]),
            )

    @staticmethod
    def _has_criteria(s: dict) -> bool:
        c = s.get("search") or {}
        return any(c.get(k) for k in ("max_price", "min_area", "max_area", "rooms", "summary_ru"))

    async def _report_links(self, chat) -> None:
        s = self.settings.get()
        lines = []
        for name, mod in (("otodom", otodom_mod), ("olx", olx_mod)):
            src = s["sources"][name]
            if not (src.get("enabled") and src.get("search_url")):
                continue
            try:
                n = len(mod.parse_search(await self.fetcher.get(src["search_url"])))
                lines.append(f"{name.upper()}: {'нашёл ' + str(n) + ' объявлений на первой странице' if n else '⚠️ ничего не нашлось'}")
            except Exception as e:
                lines.append(f"{name.upper()}: ⚠️ не загрузилось ({str(e)[:100]})")
        if lines and not getattr(self, "watcher", None):
            await chat.send_message("🔎 Поиск настроен по критериям:\n" + "\n".join(lines)
                                    + "\nСсылки можно посмотреть или заменить своими: /links")

    async def _offer_backfill(self, chat) -> None:
        await chat.send_message(
            "Всё готово, новые объявления буду присылать сам. Прислать подходящие из уже опубликованных?",
            reply_markup=InlineKeyboardMarkup([[B("За 2 суток", callback_data="bf:48"), B("За сутки", callback_data="bf:24"),
                                                B("Не надо", callback_data="bf:0")]]),
        )

    async def on_links_auto(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        await q.answer()
        if not self.ok(update):
            return
        s = self.settings.get()
        for name in ("otodom", "olx"):
            s["sources"][name]["auto"] = True
        self.settings.save(apply_auto_links(s))
        context.user_data.pop("await", None)
        await q.edit_message_reply_markup(None)
        await self._report_links(update.effective_chat)

    async def on_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if not self.ok(update):
            await q.answer()
            return
        kind, action = q.data.split(":")
        key, path, label = (
            ("pending_criteria", "search", "Критерии") if kind == "c" else ("pending_profile", "profile", "Данные о себе")
        )
        pending = context.user_data.pop(key, None)
        context.user_data.pop("await", None)
        if action == "save" and pending:
            self.settings.update(path, pending)
            await q.answer("Сохранено")
            await q.edit_message_text(f"✅ {label} сохранены.\n\n" + (fmt_criteria(pending) if kind == "c" else fmt_profile(pending)),
                                      parse_mode=ParseMode.HTML)
            s = self.settings.get()
            if kind == "c":
                self.settings.save(apply_auto_links(s))
                await self._report_links(update.effective_chat)
                s = self.settings.get()
            if not self._has_criteria(s):
                await self._ask_criteria(update, context)
            elif not (s["profile"].get("name") or s["profile"].get("about")):
                await self._ask_me(update, context)
            else:
                await self._offer_backfill(update.effective_chat)
        else:
            await q.answer("Отменено")
            await q.edit_message_text("Отменено, ничего не менял.")

    async def cmd_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.ok(update):
            text, kb = settings_view(self.settings.get())
            await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

    async def on_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if not self.ok(update):
            await q.answer()
            return
        parts = q.data.split(":")
        s = self.settings.get()
        snd = s["send"]
        act = parts[1]
        clamp = lambda v, lo, hi: max(lo, min(hi, v))  # noqa: E731
        if act == "mode":
            snd["mode"] = "manual" if snd["mode"] == "auto" else "auto"
        elif act == "dry":
            snd["dry_run"] = not snd["dry_run"]
        elif act == "min":
            s["min_score"] = clamp(int(s["min_score"]) + int(parts[2]), 0, 10)
        elif act == "auto":
            snd["auto_min_score"] = clamp(int(snd["auto_min_score"]) + int(parts[2]), 0, 10)
        elif act == "limit":
            snd["daily_limit"] = clamp(int(snd["daily_limit"]) + int(parts[2]), 0, 100)
        elif act == "int":
            s["poll_interval_minutes"] = clamp(int(s["poll_interval_minutes"]) + int(parts[2]), 5, 120)
        elif act == "ch":
            snd["channels"][parts[2]] = not snd["channels"].get(parts[2], True)
        elif act == "src":
            src = s["sources"][parts[2]]
            src["enabled"] = not src.get("enabled")
        else:
            await q.answer()
            return
        self.settings.save(s)
        if act == "int":
            self._schedule_poll()
        warn = ""
        if act == "mode" and snd["mode"] == "auto":
            warn = "Автомат включён. Риск блокировки аккаунтов выше, чем при отправке по кнопке."
        elif act == "dry" and not snd["dry_run"]:
            warn = "Тест-режим выключен: сообщения будут реально отправляться."
        await q.answer(warn or "Готово", show_alert=bool(warn))
        text, kb = settings_view(s)
        try:
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except BadRequest:
            pass

    async def cmd_backfill(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.ok(update):
            return
        try:
            hours = int(context.args[0]) if context.args else 48
        except ValueError:
            hours = 48
        await self._run_backfill(update.effective_chat, max(1, min(hours, 168)))

    async def on_backfill_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        await q.answer()
        if not self.ok(update):
            return
        hours = int(q.data.split(":")[1])
        await q.edit_message_reply_markup(None)
        if hours:
            await self._run_backfill(update.effective_chat, hours)
        else:
            await update.effective_chat.send_message("Ок, буду присылать только новые.")

    async def _run_backfill(self, chat, hours: int) -> None:
        s = self.settings.get()
        if not (getattr(self, "watcher", None) or s["sources"]["otodom"]["search_url"] or s["sources"]["olx"]["search_url"]
                or s["sources"]["channel"]["enabled"]):
            await chat.send_message("Сначала пришли ссылки на выдачу: /links")
            return
        status = await chat.send_message(f"Собираю объявления за последние {hours} ч… Это займёт несколько минут.")
        last = [""]

        async def progress(text):
            if text != last[0]:
                last[0] = text
                await status.edit_text(text)

        try:
            st = await self.pipeline.backfill(hours=hours, progress=progress)
        except Exception as e:
            log.exception("backfill")
            await status.edit_text(f"Не получилось: {e}")
            return
        parts = [f"найдено {st.get('found', 0)}", f"карточек {st.get('sent', 0)}"]
        for k, name in (("filtered", "отсеяно фильтрами"), ("lowscore", "низкая оценка"), ("already", "уже присылал"),
                        ("over_limit", "не успел оценить (лимит 40 за раз)"), ("errors", "ошибок")):
            if st.get(k):
                parts.append(f"{name} {st[k]}")
        text = f"Готово за {hours} ч: " + ", ".join(parts) + "."
        if not st.get("found"):
            text += " Если это странно, проверь ссылки (/links): возможно, сайт поменял вёрстку."
        await status.edit_text(text)

    # --- обновления ---------------------------------------------------------------
    async def _update_check_job(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        from .updater import available

        try:
            new = await asyncio.to_thread(available)
        except Exception:
            return
        if new and self.db.kv_get("update_notified") != new["sha"]:
            self.db.kv_set("update_notified", new["sha"])
            await self.app.bot.send_message(
                self.chat_id, f"🆕 Есть обновление: {new['message']}",
                reply_markup=InlineKeyboardMarkup([[B("Обновить", callback_data="up:go")]]),
            )

    async def cmd_update(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.ok(update):
            await self._do_update(update.effective_chat)

    async def on_update_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        await q.answer()
        if self.ok(update):
            await q.edit_message_reply_markup(None)
            await self._do_update(update.effective_chat)

    async def _do_update(self, chat) -> None:
        from .updater import UpdateError, apply, available

        msg = await chat.send_message("Проверяю обновления…")
        try:
            new = await asyncio.to_thread(available)
            if not new:
                await msg.edit_text("✅ У тебя последняя версия.")
                return
            await msg.edit_text(f"Скачиваю: {new['message']}…")
            res = await asyncio.to_thread(apply, new)
        except UpdateError as e:
            await msg.edit_text(f"Не получилось обновить: {e}")
            return
        await msg.edit_text(f"✅ Обновлено ({len(res['changed'])} файлов): {res['message']}\nПерезапускаюсь, это ~20 секунд.")
        self.restart_requested = True
        self.app.stop_running()

    async def cmd_hours(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.ok(update):
            return
        try:
            a, b = (int(x) for x in context.args[:2])
            assert 0 <= a < b <= 24
        except Exception:
            await update.message.reply_text("Формат: /hours 8 22")
            return
        self.settings.update("send.hours", [a, b])
        await update.message.reply_text(f"Автоотправка с {a}:00 до {b}:00.")

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.ok(update):
            return
        counts = self.db.counts()
        last = self.db.kv_get("last_poll")
        ago = f"{int((time.time() - float(last)) / 60)} мин назад" if last else "ещё не было"
        s = self.settings.get()
        names = {
            "sent": "карточек", "contacted": "написано", "skipped": "пропущено", "lowscore": "низкая оценка",
            "filtered": "отсеяно фильтрами", "seed": "запомнено при старте", "error": "ошибки",
        }
        lines = [f"{names.get(k, k)}: {v}" for k, v in sorted(counts.items())]
        await update.message.reply_text(
            f"{'⏸ Пауза' if self.db.kv_get('paused') == '1' else '▶️ Работает'} · проверка: {ago}\n"
            f"Режим: {'автомат' if s['send']['mode'] == 'auto' else 'по кнопке'}"
            f"{' · ТЕСТ' if s['send']['dry_run'] else ''} · отправлено сегодня: {self.sends.sent_today()}/{s['send']['daily_limit']}\n"
            + ("\n".join(lines) or "пока пусто")
        )

    async def cmd_poll(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.ok(update):
            return
        await update.message.reply_text("Проверяю…")
        stats = await self.pipeline.cycle()
        if stats.get("no_sources"):
            await update.message.reply_text("Нет ссылок на выдачу: /links")
            return
        names = {"sent": "новых карточек", "filtered": "отсеяно", "lowscore": "низкая оценка", "seeded": "запомнено",
                 "errors": "ошибок", "retry": "отложено"}
        await update.message.reply_text(
            "Готово: " + (", ".join(f"{names.get(k, k)} {v}" for k, v in stats.items()) or "новых нет")
        )

    async def cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.ok(update):
            self.db.kv_set("paused", "1")
            await update.message.reply_text("Пауза. /resume — продолжить.")

    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.ok(update):
            self.db.kv_set("paused", "0")
            await update.message.reply_text("Продолжаю.")

    async def cmd_login(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.ok(update):
            return
        try:
            await self.sender.open_login_pages()
        except Exception as e:
            await update.message.reply_text(f"Не открылся браузер: {e}")
            return
        await update.message.reply_text(
            "На компьютере открылось окно браузера с тремя вкладками. Войди:\n"
            "1) WhatsApp Web: телефон → Связанные устройства → отсканируй QR;\n"
            "2) OLX: войди в аккаунт;\n3) Otodom: войди (не обязательно, но форма заполнится сама).\n"
            "Окно можно не закрывать, сессии сохранятся."
        )

    # --- свободный текст -------------------------------------------------------
    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.ok(update) or not update.message or not update.message.text:
            return
        text = update.message.text.strip()
        reply = update.message.reply_to_message
        if reply is not None:
            row = self.db.by_message(reply.message_id)
            if row is not None:
                await self._redraft_reply(update, row, text)
                return
        urls = URL_RE.findall(text)
        if urls:
            search_urls = [u for u in urls if classify_url(u) in ("otodom_search", "olx_search")]
            listing_urls = [u for u in urls if listing_from_url(u)]
            if search_urls:
                context.user_data.pop("await", None)
                await self._handle_links(update, search_urls)
            for u in listing_urls[:3]:
                await self._analyze_one(update, u)
            if search_urls or listing_urls:
                return
        waiting = context.user_data.get("await")
        if waiting == "criteria":
            await self._handle_criteria(update, context, text)
        elif waiting == "me":
            await self._handle_me(update, context, text)
        elif waiting == "links":
            await update.message.reply_text("Пришли ссылку на страницу выдачи Otodom или OLX.")
        else:
            await update.message.reply_text("Не понял. /help — что умею.")

    async def _analyze_one(self, update, url: str) -> None:
        msg = await update.message.reply_text("Открываю и разбираю объявление…")
        try:
            row_id = await self.pipeline.process_url(url)
            await msg.delete()
            if row_id is None:
                await update.message.reply_text("Не похоже на объявление OLX/Otodom.")
        except Exception as e:
            log.exception("analyze one")
            await msg.edit_text(f"Не получилось: {e}")

    async def _redraft_reply(self, update, row, instruction: str) -> None:
        try:
            listing = json.loads(row["listing"])
            draft = await self.analyzer.redraft(listing, row["draft"], instruction)
            self.db.update(row["id"], draft=draft)
            await self.refresh_card(row["id"])
            await update.message.reply_text("Черновик обновил ↑")
        except Exception as e:
            log.exception("redraft by reply")
            await update.message.reply_text(f"Не получилось переписать: {e}")

    def run(self) -> None:
        self.app.run_polling(allowed_updates=Update.ALL_TYPES)
