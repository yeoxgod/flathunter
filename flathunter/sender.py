"""Отправка сообщений с ваших аккаунтов через отдельное окно браузера (Playwright).

Вход в WhatsApp Web, OLX и Otodom выполняется один раз вручную (setup или /login),
сессии хранятся в data/browser-profile.

Принципы:
- dry_run: текст вставляется, но кнопка «Отправить» не нажимается; бот присылает скриншот;
- капча, выход из аккаунта или незнакомая страница -> остановка и скриншот, без обходов;
- одно объявление и один номер получают сообщение только один раз;
- отправка по одной, с паузами; автомат соблюдает дневной лимит и часы.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse

log = logging.getLogger(__name__)

CHANNEL_NAMES = {"whatsapp": "WhatsApp", "olx": "чат OLX", "otodom": "форму Otodom"}

LOGIN_PAGES = [
    "https://web.whatsapp.com/",
    "https://www.olx.pl/konto/",
    "https://www.otodom.pl/pl/",
]

COOKIE_BUTTONS = [
    "#onetrust-accept-btn-handler",
    "button:has-text('Akceptuję')",
    "button:has-text('Zaakceptuj wszystkie')",
    "button:has-text('Accept all')",
]

CAPTCHA_FRAMES = ("captcha", "recaptcha", "hcaptcha", "challenges.cloudflare", "turnstile")
CAPTCHA_TEXT = re.compile(r"verify you are human|nie jesteś robotem|jesteś człowiekiem|potwierdź, że jesteś", re.I)

WA_COMPOSE = [
    "footer div[contenteditable='true']",
    "div[contenteditable='true'][data-tab='10']",
    "div[role='textbox'][contenteditable='true'][aria-label]",
]
WA_QR = ["canvas[aria-label]", "div[data-ref]", "[data-testid='qrcode']"]
WA_INVALID = re.compile(r"invalid|nieprawidłow|isn.t on whatsapp|nie (?:korzysta|ma) .*whatsapp", re.I)
WA_SEND = [
    "button[aria-label='Send']",
    "button[aria-label='Wyślij']",
    "span[data-icon='send']",
    "span[data-icon='wds-ic-send-filled']",
]

OLX_MSG_BUTTON = [
    "[data-testid='ad-contact-message-button']",
    "[data-cy='ad-contact-message-button']",
    "button:has-text('Napisz wiadomość')",
    "button:has-text('Wyślij wiadomość')",
    "a:has-text('Napisz wiadomość')",
]
OLX_TEXTAREA = [
    "textarea[name='message']",
    "[data-testid='chat-textarea'] textarea",
    "textarea[data-cy='chat-textarea']",
    "[data-testid='message-input'] textarea",
    "form textarea",
]
OLX_SUBMIT = [
    "[data-testid='chat-form-submit']",
    "[data-cy='chat-form-submit']",
    "form button[type='submit']",
    "button:has-text('Wyślij')",
]

OTODOM_OPEN = [
    "button:has-text('Wyślij wiadomość')",
    "button:has-text('Napisz wiadomość')",
    "button:has-text('Zapytaj o ofertę')",
    "button:has-text('Zapytaj')",
]
OTODOM_TEXTAREA = ["textarea[name='text']", "textarea[name='message']", "form textarea"]
OTODOM_FIELDS = {
    "name": ["input[name='name']"],
    "email": ["input[name='email']", "input[type='email']"],
    "phone": ["input[name='phone']", "input[type='tel']"],
}
OTODOM_SUBMIT = [
    "[data-cy='contact-form.submit-button']",
    "form button[type='submit']",
    "button:has-text('Wyślij wiadomość')",
    "button:has-text('Wyślij')",
]
OTODOM_OK = re.compile(r"wiadomość (?:została )?wysłan|dziękujemy|message (?:was )?sent", re.I)


@dataclass
class SendResult:
    ok: bool
    channel: str
    dry_run: bool
    message: str
    screenshot: str | None = None
    kind: str = ""  # captcha | login | not_found | invalid_phone | unverified | error | duplicate | limit | hours


def choose_channel(listing: dict, s: dict) -> str | None:
    ch = (s.get("send") or {}).get("channels") or {}
    if listing.get("phones") and ch.get("whatsapp", True):
        return "whatsapp"
    host = urlparse(listing.get("url", "")).netloc
    if host.endswith("olx.pl") and ch.get("olx", True):
        return "olx"
    if host.endswith("otodom.pl") and ch.get("otodom", True):
        return "otodom"
    return None


class BrowserSender:
    def __init__(self, profile_dir: str | Path, shots_dir: str | Path, base_urls: dict | None = None, headless: bool = False):
        self.profile_dir = Path(profile_dir)
        self.shots_dir = Path(shots_dir)
        self.headless = headless
        self.base = {"whatsapp": "https://web.whatsapp.com", **(base_urls or {})}
        self._pw = None
        self._ctx = None
        self._lock = asyncio.Lock()
        self.timeout_ms = 45000

    async def context(self):
        if self._ctx is not None:
            try:
                _ = self._ctx.pages
                return self._ctx
            except Exception:
                self._ctx = None
        from playwright.async_api import async_playwright

        if self._pw is None:
            self._pw = await async_playwright().start()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._ctx = await self._pw.chromium.launch_persistent_context(
            str(self.profile_dir),
            headless=self.headless,
            locale="pl-PL",
            viewport={"width": 1280, "height": 860},
        )
        self._ctx.on("close", lambda *_: setattr(self, "_ctx", None))
        return self._ctx

    async def close(self) -> None:
        try:
            if self._ctx is not None:
                await self._ctx.close()
        finally:
            self._ctx = None
            if self._pw is not None:
                await self._pw.stop()
                self._pw = None

    async def open_login_pages(self) -> None:
        ctx = await self.context()
        pages = list(ctx.pages)
        for i, url in enumerate(LOGIN_PAGES):
            page = pages[i] if i < len(pages) else await ctx.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded")
            except Exception as e:
                log.warning("не открылось %s: %s", url, e)

    # --- helpers ---------------------------------------------------------------
    async def _shot(self, page, tag: str) -> str | None:
        try:
            self.shots_dir.mkdir(parents=True, exist_ok=True)
            p = self.shots_dir / f"{datetime.now():%Y%m%d-%H%M%S}-{tag}.png"
            await page.screenshot(path=str(p))
            return str(p)
        except Exception:
            return None

    @staticmethod
    async def _visible(page, selectors: list[str]):
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    return loc
            except Exception:
                continue
        return None

    async def _wait_any(self, page, groups: dict[str, list[str]], timeout_ms: int):
        """Ждёт, пока появится любой из наборов селекторов. Возвращает (имя, локатор)."""
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            for name, sels in groups.items():
                loc = await self._visible(page, sels)
                if loc is not None:
                    return name, loc
            if await self._captcha(page):
                return "captcha", None
            await asyncio.sleep(0.5)
        return None, None

    @staticmethod
    async def _captcha(page) -> bool:
        for fr in page.frames:
            if any(k in (fr.url or "").lower() for k in CAPTCHA_FRAMES):
                return True
        try:
            body = await page.locator("body").inner_text(timeout=2000)
        except Exception:
            return False
        return bool(CAPTCHA_TEXT.search(body[:5000]))

    async def _cookies(self, page) -> None:
        loc = await self._visible(page, COOKIE_BUTTONS)
        if loc is not None:
            try:
                await loc.click(timeout=3000)
                await asyncio.sleep(0.5)
            except Exception:
                pass

    async def _fail(self, page, channel, dry, kind, msg) -> SendResult:
        return SendResult(False, channel, dry, msg, await self._shot(page, f"{channel}-{kind}"), kind)

    # --- send ------------------------------------------------------------------
    async def send(self, channel: str, listing: dict, text: str, profile: dict, dry_run: bool) -> SendResult:
        async with self._lock:
            ctx = await self.context()
            page = await ctx.new_page()
            try:
                if channel == "whatsapp":
                    return await self._whatsapp(page, listing["phones"][0], text, dry_run)
                if channel == "olx":
                    return await self._olx(page, listing["url"], text, dry_run)
                if channel == "otodom":
                    return await self._otodom(page, listing["url"], text, profile, dry_run)
                return SendResult(False, channel, dry_run, "неизвестный канал", None, "error")
            except Exception as e:
                log.exception("отправка %s", channel)
                return await self._fail(page, channel, dry_run, "error", f"ошибка браузера: {e}")
            finally:
                try:
                    await page.close()
                except Exception:
                    pass

    async def _whatsapp(self, page, phone: str, text: str, dry: bool) -> SendResult:
        await page.goto(f"{self.base['whatsapp']}/send?phone={phone}&text={quote(text)}", wait_until="domcontentloaded")
        deadline = time.monotonic() + self.timeout_ms / 1000
        compose = None
        while time.monotonic() < deadline:
            compose = await self._visible(page, WA_COMPOSE)
            if compose is not None:
                break
            if await self._visible(page, WA_QR) is not None:
                return await self._fail(page, "whatsapp", dry, "login",
                                        "WhatsApp Web не залогинен: открой /login и отсканируй QR.")
            try:
                body = await page.locator("body").inner_text(timeout=1000)
                if WA_INVALID.search(body[:3000]):
                    return await self._fail(page, "whatsapp", dry, "invalid_phone",
                                            f"Номера +{phone} нет в WhatsApp.")
            except Exception:
                pass
            await asyncio.sleep(0.7)
        if compose is None:
            return await self._fail(page, "whatsapp", dry, "not_found", "Не нашёл поле ввода WhatsApp.")

        await asyncio.sleep(1.0)
        current = (await compose.inner_text()).strip()
        if text.split("\n")[0][:20].strip() not in current:
            await compose.click()
            for i, line in enumerate(text.split("\n")):
                if i:
                    await page.keyboard.press("Shift+Enter")
                if line:
                    await page.keyboard.insert_text(line)
        if dry:
            return SendResult(True, "whatsapp", True, "Тест: текст вставлен в WhatsApp, не отправлен.",
                              await self._shot(page, "whatsapp-dry"))
        btn = await self._visible(page, WA_SEND)
        if btn is not None:
            await btn.click()
        else:
            await compose.press("Enter")
        for _ in range(20):
            await asyncio.sleep(0.5)
            if not (await compose.inner_text()).strip():
                return SendResult(True, "whatsapp", False, "Отправлено в WhatsApp.", await self._shot(page, "whatsapp-ok"))
        return await self._fail(page, "whatsapp", dry, "unverified", "Не уверен, что сообщение ушло: проверь скрин.")

    async def _olx(self, page, url: str, text: str, dry: bool) -> SendResult:
        await page.goto(url, wait_until="domcontentloaded")
        await self._cookies(page)
        name, loc = await self._wait_any(page, {"textarea": OLX_TEXTAREA, "button": OLX_MSG_BUTTON}, 20000)
        if name == "captcha":
            return await self._fail(page, "olx", dry, "captcha", "OLX показал капчу. Отправь вручную.")
        if name == "button":
            await loc.click()
            name, loc = await self._wait_any(page, {"textarea": OLX_TEXTAREA}, 15000)
            if name == "captcha":
                return await self._fail(page, "olx", dry, "captcha", "OLX показал капчу. Отправь вручную.")
        if "login" in page.url or "logowanie" in page.url:
            return await self._fail(page, "olx", dry, "login", "OLX просит войти: открой /login.")
        if name != "textarea":
            return await self._fail(page, "olx", dry, "not_found", "Не нашёл чат OLX на странице.")
        await loc.fill(text)
        if dry:
            return SendResult(True, "olx", True, "Тест: текст вставлен в чат OLX, не отправлен.", await self._shot(page, "olx-dry"))
        btn = await self._visible(page, OLX_SUBMIT)
        if btn is None:
            return await self._fail(page, "olx", dry, "not_found", "Не нашёл кнопку отправки OLX.")
        await btn.click()
        for _ in range(16):
            await asyncio.sleep(0.5)
            if await self._captcha(page):
                return await self._fail(page, "olx", dry, "captcha", "OLX показал капчу после отправки. Проверь вручную.")
            try:
                if not (await loc.input_value()).strip():
                    return SendResult(True, "olx", False, "Отправлено в чат OLX.", await self._shot(page, "olx-ok"))
            except Exception:
                break
        return await self._fail(page, "olx", dry, "unverified", "Не уверен, что сообщение ушло: проверь скрин.")

    async def _otodom(self, page, url: str, text: str, profile: dict, dry: bool) -> SendResult:
        await page.goto(url, wait_until="domcontentloaded")
        await self._cookies(page)
        name, loc = await self._wait_any(page, {"textarea": OTODOM_TEXTAREA, "button": OTODOM_OPEN}, 20000)
        if name == "captcha":
            return await self._fail(page, "otodom", dry, "captcha", "Otodom показал капчу. Отправь вручную.")
        if name == "button":
            await loc.click()
            name, loc = await self._wait_any(page, {"textarea": OTODOM_TEXTAREA}, 15000)
        if name != "textarea":
            return await self._fail(page, "otodom", dry, "not_found", "Не нашёл форму контакта Otodom.")
        for field_name, sels in OTODOM_FIELDS.items():
            inp = await self._visible(page, sels)
            if inp is None:
                continue
            if (await inp.input_value()).strip():
                continue
            value = (profile or {}).get(field_name, "")
            if not value and field_name in ("email", "name"):
                return await self._fail(page, "otodom", dry, "error",
                                        f"Для формы Otodom нужно поле «{field_name}»: заполни через /me.")
            if value:
                await inp.fill(value)
        await loc.fill(text)
        if dry:
            return SendResult(True, "otodom", True, "Тест: форма Otodom заполнена, не отправлена.",
                              await self._shot(page, "otodom-dry"))
        btn = await self._visible(page, OTODOM_SUBMIT)
        if btn is None:
            return await self._fail(page, "otodom", dry, "not_found", "Не нашёл кнопку отправки Otodom.")
        await btn.click()
        for _ in range(20):
            await asyncio.sleep(0.5)
            if await self._captcha(page):
                return await self._fail(page, "otodom", dry, "captcha", "Otodom показал капчу. Отправь вручную.")
            try:
                if OTODOM_OK.search((await page.locator("body").inner_text(timeout=1000))[:8000]):
                    return SendResult(True, "otodom", False, "Отправлено через форму Otodom.", await self._shot(page, "otodom-ok"))
            except Exception:
                pass
        return await self._fail(page, "otodom", dry, "unverified", "Форма отправлена, но подтверждения не видно: проверь скрин.")


@dataclass
class SendJob:
    row_id: int
    channel: str
    listing: dict
    text: str
    auto: bool = False
    done: asyncio.Future | None = field(default=None, repr=False)


class SendManager:
    """Очередь отправки: по одной, с паузами, лимитами и защитой от повторов."""

    def __init__(self, sender, db, get_settings, on_result, sleep=asyncio.sleep, now=datetime.now):
        self.sender = sender
        self.db = db
        self.get_settings = get_settings
        self.on_result = on_result  # async (job, SendResult) -> None
        self.queue: asyncio.Queue[SendJob] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._sleep = sleep
        self._now = now

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._worker())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    def _day_key(self) -> str:
        return f"sent_count:{self._now():%Y-%m-%d}"

    def sent_today(self) -> int:
        return int(self.db.kv_get(self._day_key(), "0"))

    def check(self, job: SendJob) -> SendResult | None:
        """Причина не отправлять (или None)."""
        s = self.get_settings()
        snd = s.get("send") or {}
        dry = bool(snd.get("dry_run", True))
        row = self.db.get(job.row_id)
        if row is not None and row["status"] == "contacted":
            return SendResult(False, job.channel, dry, "Этому объявлению уже писали.", kind="duplicate")
        if job.channel == "whatsapp":
            phone = (job.listing.get("phones") or [""])[0]
            if self.db.kv_get(f"sent_phone:{phone}") == "1":
                return SendResult(False, job.channel, dry, f"На номер +{phone} уже писали.", kind="duplicate")
        if dry:
            return None
        if self.sent_today() >= int(snd.get("daily_limit", 10)):
            return SendResult(False, job.channel, dry, "Дневной лимит отправок исчерпан.", kind="limit")
        if job.auto:
            start, end = (snd.get("hours") or [8, 22])[:2]
            if not (int(start) <= self._now().hour < int(end)):
                return SendResult(False, job.channel, dry, f"Вне часов отправки ({start}–{end}).", kind="hours")
        return None

    def submit(self, job: SendJob) -> None:
        self.start()
        self.queue.put_nowait(job)

    async def _worker(self) -> None:
        while True:
            job = await self.queue.get()
            try:
                await self._handle(job)
            except Exception:
                log.exception("очередь отправки")
            finally:
                self.queue.task_done()

    async def _handle(self, job: SendJob) -> None:
        blocked = self.check(job)
        if blocked is not None:
            await self.on_result(job, blocked)
            return
        s = self.get_settings()
        dry = bool((s.get("send") or {}).get("dry_run", True))
        res = await self.sender.send(job.channel, job.listing, job.text, s.get("profile") or {}, dry)
        if res.ok and not res.dry_run:
            self.db.update(job.row_id, status="contacted")
            self.db.kv_set(self._day_key(), str(self.sent_today() + 1))
            if job.channel == "whatsapp":
                self.db.kv_set(f"sent_phone:{job.listing['phones'][0]}", "1")
        elif res.kind == "unverified":
            self.db.update(job.row_id, status="contacted")  # лучше не написать повторно
        await self.on_result(job, res)
        if not res.dry_run and (res.ok or res.kind == "unverified"):
            lo, hi = (120, 360) if job.auto else (20, 60)
            await self._sleep(random.uniform(lo, hi))
