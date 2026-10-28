"""Чтение объявлений из вашего чата с ботом-агрегатором (по умолчанию @thewarsawrentbot)
от вашего Telegram-аккаунта через официальный API Telegram (Telethon).

Бот только читает сообщения из этого одного чата: ничего не отправляет, ничего не удаляет.
Сессия хранится в data/tg_user.session — это доступ к вашему аккаунту, никому не передавайте папку data."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .sources.textparse import listings_from_text

log = logging.getLogger(__name__)


def message_urls(msg) -> list[str]:
    """Все ссылки сообщения: скрытые в тексте (text_url), обычные и в кнопках."""
    urls = []
    try:
        from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl

        text = msg.raw_text or ""
        for ent in msg.entities or []:
            if isinstance(ent, MessageEntityTextUrl):
                urls.append(ent.url)
            elif isinstance(ent, MessageEntityUrl):
                urls.append(text[ent.offset: ent.offset + ent.length])
    except Exception:
        pass
    for row in (getattr(msg, "buttons", None) or []):
        for b in row:
            u = getattr(b, "url", None)
            if u:
                urls.append(u)
    return urls


def listings_from_message(msg) -> list:
    posted = ""
    if getattr(msg, "date", None):
        posted = msg.date.astimezone().replace(tzinfo=None).isoformat(timespec="minutes")
    return listings_from_text(msg.raw_text or msg.message or "", message_urls(msg), posted)


class TgUserWatcher:
    def __init__(self, data_dir, api_id: int, api_hash: str, chat: str, on_listings):
        self.session = str(Path(data_dir) / "tg_user")
        self.api_id = int(api_id)
        self.api_hash = api_hash
        self.chat = chat.lstrip("@")
        self.on_listings = on_listings  # async (list[Listing]) -> None
        self.client = None
        self.entity = None

    async def start(self) -> bool:
        from telethon import TelegramClient, events

        self.client = TelegramClient(self.session, self.api_id, self.api_hash)
        await self.client.connect()
        if not await self.client.is_user_authorized():
            log.warning("Telegram-аккаунт не авторизован: запусти tg-login.bat")
            await self.client.disconnect()
            self.client = None
            return False
        self.entity = await self.client.get_entity(self.chat)

        @self.client.on(events.NewMessage(chats=self.entity))
        async def handler(event):  # noqa: ANN001
            try:
                items = listings_from_message(event.message)
                if items:
                    await self.on_listings(items)
            except Exception:
                log.exception("разбор сообщения от @%s", self.chat)

        log.info("Слушаю чат с @%s", self.chat)
        return True

    async def recent(self, hours: int, limit: int = 400) -> list:
        """Объявления из сообщений за последние `hours` часов (для /backfill)."""
        if self.client is None:
            return []
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        out = []
        async for msg in self.client.iter_messages(self.entity, limit=limit):
            if msg.date < since:
                break
            out.extend(listings_from_message(msg))
        return out

    async def stop(self) -> None:
        if self.client is not None:
            await self.client.disconnect()


def interactive_login(data_dir, api_id: int, api_hash: str, chat: str) -> str:
    """Вход в Telegram-аккаунт в консоли (номер, код из Telegram, пароль 2FA вводит сам пользователь)."""
    import asyncio
    import getpass

    from telethon import TelegramClient

    async def run() -> str:
        client = TelegramClient(str(Path(data_dir) / "tg_user"), int(api_id), api_hash)
        await client.start(
            phone=lambda: input("Номер телефона Telegram (в формате +48…): ").strip(),
            code_callback=lambda: input("Код, который пришёл в Telegram: ").strip(),
            password=lambda: getpass.getpass("Облачный пароль (2FA), если есть: "),
        )
        ent = await client.get_entity(chat.lstrip("@"))
        n = 0
        async for msg in client.iter_messages(ent, limit=30):
            n += len(listings_from_message(msg))
        me = await client.get_me()
        await client.disconnect()
        return f"Вошёл как {me.first_name}. В последних 30 сообщениях от @{chat.lstrip('@')} нашёл ссылок на объявления: {n}."

    return asyncio.run(run())
