from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .filters import prefilter
from .models import Listing
from .sources import build_sources, olx, otodom

DETAIL = {"olx": olx.parse_detail, "otodom": otodom.parse_detail}
from .sources.base import FetchError
from .util import parse_posted

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 3
BLOCKED_NOTE = "Страница объявления недоступна (блокировка): оценка только по данным из поста."


def page_url(url: str, n: int) -> str:
    p = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k != "page"]
    q.append(("page", str(n)))
    return urlunparse(p._replace(query=urlencode(q)))


def seed_key(src) -> str:
    return f"seeded:{src.name}:{hashlib.sha1(src.search_url.encode()).hexdigest()[:10]}"


def listing_from_url(url: str) -> tuple[Listing, object] | None:
    """Объявление по прямой ссылке (когда вы сами присылаете ссылку боту)."""
    host = urlparse(url).netloc.lower()
    if host.endswith("otodom.pl") and "/oferta/" in url:
        ext = otodom._ID_FROM_URL.search(url)
        return Listing("otodom", ext.group(1) if ext else url, url.split("?")[0]), otodom
    if host.endswith("olx.pl") and "/d/oferta/" in url:
        clean = url.split("?")[0].split("#")[0]
        return Listing("olx", olx._ext_id(clean), clean), olx
    return None


class _Src:
    def __init__(self, parse_detail):
        self.parse_detail = parse_detail


class Pipeline:
    """Один проход: источники -> новые -> предфильтр -> страница -> модель -> карточка."""

    def __init__(self, db, get_settings, fetcher, analyzer, notify, alert):
        self.db = db
        self.get_settings = get_settings
        self.fetcher = fetcher
        self.analyzer = analyzer
        self.notify = notify  # async (row_id, listing_dict, analysis) -> None
        self.alert = alert    # async (text) -> None
        self.lock = asyncio.Lock()
        self.extra_backfill = None  # async (hours) -> list[Listing], например чат с ботом-агрегатором

    def _bump(self, key: str) -> int:
        n = int(self.db.kv_get(key, "0")) + 1
        self.db.kv_set(key, str(n))
        return n

    def _reset(self, key: str) -> None:
        self.db.kv_set(key, "0")

    async def cycle(self) -> dict:
        async with self.lock:
            s = self.get_settings()
            stats: Counter = Counter()
            sources = build_sources(s)
            if not sources:
                stats["no_sources"] = 1
            for src in sources:
                try:
                    await self._run_source(src, s, stats)
                except Exception:
                    log.exception("источник %s упал", src.name)
                    stats["errors"] += 1
            self.db.kv_set("last_poll", str(time.time()))
            return dict(stats)

    async def _run_source(self, src, s: dict, stats: Counter) -> None:
        fail_key, empty_key = f"fail:{src.name}", f"empty:{src.name}"
        try:
            html = await self.fetcher.get(src.search_url)
            listings = src.parse_search(html)
        except Exception as e:
            n = self._bump(fail_key)
            log.warning("%s: ошибка загрузки (%s подряд): %s", src.name, n, e)
            if n == 3:
                await self.alert(f"⚠️ {src.name}: 3 неудачи подряд ({e}). Продолжаю пробовать.")
            stats["errors"] += 1
            return
        self._reset(fail_key)
        if not listings:
            if self._bump(empty_key) == 3:
                await self.alert(
                    f"⚠️ {src.name}: в выдаче 3 раза подряд ничего не нашлось. Возможно, поменялась вёрстка "
                    "сайта или ссылка неверная. Проверь /links."
                )
            return
        self._reset(empty_key)

        skey = seed_key(src)
        if self.db.kv_get(skey) != "1" and s.get("seed_on_first_run", True):
            n = 0
            for l in listings:
                if not self.db.has(l.key):
                    self.db.insert(l, "seed")
                    n += 1
            self.db.kv_set(skey, "1")
            stats["seeded"] += n
            await self.alert(f"👀 {src.name}: запомнил {n} текущих объявлений. Дальше пришлю только новые.")
            return

        new = [l for l in listings if not self.db.has(l.key)]
        for l in new[: int(s.get("max_new_per_cycle", 8))]:
            await self.process(src.parse_detail, l, stats, s)

    def _retry_or_give_up(self, l, stats: Counter) -> None:
        if self._bump(f"try:{l.key}") >= MAX_ATTEMPTS:
            self.db.insert(l, "error")
            stats["errors"] += 1
        else:
            stats["retry"] += 1

    async def process(self, parse_detail, l: Listing, stats: Counter, s: dict, forced: bool = False,
                      allow_auto: bool = True) -> int | None:
        """forced=True: объявление прислали вручную, фильтры и порог не применяются."""
        search = s.get("search") or {}
        if not forced:
            ok, why = prefilter(l, search)
            if not ok:
                log.info("отсев по посту: %s (%s)", l.url, why)
                self.db.insert(l, "filtered")
                stats["filtered"] += 1
                return None

        try:
            html = await self.fetcher.get(l.url)
            l = parse_detail(html, l)
        except FetchError as e:
            if e.blocked or forced:
                l.params["_note"] = BLOCKED_NOTE if e.blocked else f"Страница не загрузилась: {e}"
            else:
                log.warning("страница объявления: %s", e)
                self._retry_or_give_up(l, stats)
                return None
        except Exception:
            log.exception("разбор страницы объявления %s", l.url)
            if not forced:
                self._retry_or_give_up(l, stats)
                return None

        if not forced:
            ok, why = prefilter(l, search)
            if not ok:
                log.info("отсев по описанию: %s (%s)", l.url, why)
                self.db.insert(l, "filtered")
                stats["filtered"] += 1
                return None

        try:
            analysis = await self.analyzer.analyze(l)
        except Exception as e:
            log.exception("анализ %s", l.url)
            stats["errors"] += 1
            if forced:
                raise
            if self._bump("llm_fail") == 3:
                await self.alert(f"⚠️ Claude не отвечает 3 раза подряд: {e}. Возможно, кончился лимит подписки.")
            return None
        self._reset("llm_fail")

        if not forced and analysis["fit_score"] < int(s.get("min_score", 5)):
            self.db.insert(l, "lowscore", analysis=analysis, draft=analysis["message_pl"])
            stats["lowscore"] += 1
            return None

        if forced and self.db.has(l.key):
            row = self.db.c.execute("SELECT id FROM listings WHERE key=?", (l.key,)).fetchone()
            self.db.delete(row["id"])
        row_id = self.db.insert(l, "sent", analysis=analysis, draft=analysis["message_pl"])
        try:
            await self.notify(row_id, asdict(l), analysis, allow_auto=allow_auto)
        except Exception:
            log.exception("не удалось отправить карточку, повторю на следующем проходе")
            self.db.delete(row_id)
            stats["errors"] += 1
            if forced:
                raise
            return None
        stats["sent"] += 1
        return row_id

    async def process_url(self, url: str) -> int | None:
        found = listing_from_url(url)
        if not found:
            return None
        l, mod = found
        async with self.lock:
            return await self.process(mod.parse_detail, l, Counter(), self.get_settings(), forced=True)

    async def backfill(self, hours: int = 48, max_pages: int = 5, max_analyze: int = 40, progress=None) -> dict:
        """Объявления за последние `hours` часов: несколько страниц выдачи -> фильтры -> оценка -> карточки.
        Автоотправки нет. `progress` — async (text) -> None."""
        async def say(text):
            if progress:
                try:
                    await progress(text)
                except Exception:
                    pass

        async with self.lock:
            s = self.get_settings()
            now = datetime.now()
            cutoff = now - timedelta(hours=hours)
            stats: Counter = Counter()
            candidates, seen_all, keys = [], [], set()
            for src in build_sources(s):
                pages = 1 if src.name == "channel" else max_pages
                for n in range(1, pages + 1):
                    await say(f"Читаю {src.name}, страница {n}…")
                    url = src.search_url if n == 1 else page_url(src.search_url, n)
                    try:
                        listings = src.parse_search(await self.fetcher.get(url))
                    except Exception as e:
                        log.warning("backfill %s p%s: %s", src.name, n, e)
                        stats["errors"] += 1
                        break
                    fresh = [l for l in listings if l.key not in keys]
                    if not fresh:
                        break
                    in_window = 0
                    for l in fresh:
                        keys.add(l.key)
                        seen_all.append(l)
                        dt = parse_posted(l.posted)
                        if (dt is None and n == 1) or (dt is not None and dt >= cutoff):
                            candidates.append((dt or now, src, l))
                            in_window += 1
                    if in_window == 0:
                        break
                self.db.kv_set(seed_key(src), "1")

            if self.extra_backfill is not None:
                await say("Читаю сообщения бота-агрегатора…")
                try:
                    for l in await self.extra_backfill(hours):
                        if l.key in keys:
                            continue
                        keys.add(l.key)
                        seen_all.append(l)
                        dt = parse_posted(l.posted)
                        candidates.append((dt or now, _Src(DETAIL[l.source]), l))
                except Exception as e:
                    log.warning("backfill из чата: %s", e)
                    stats["errors"] += 1

            candidates.sort(key=lambda x: x[0], reverse=True)
            stats["found"] = len(candidates)
            await say(f"За {hours} ч нашёл {len(candidates)} объявлений, фильтрую и оцениваю…")
            analyzed = 0
            for i, (_, src, l) in enumerate(candidates, 1):
                row = self.db.c.execute("SELECT id, status FROM listings WHERE key=?", (l.key,)).fetchone()
                if row is not None and row["status"] not in ("seed", "filtered"):  # фильтры могли поменяться
                    stats["already"] += 1
                    continue
                if row is not None:
                    self.db.delete(row["id"])
                if not prefilter(l, s.get("search") or {})[0]:
                    self.db.insert(l, "filtered")
                    stats["filtered"] += 1
                    continue
                if analyzed >= max_analyze:
                    stats["over_limit"] += 1
                    continue
                analyzed += 1
                await self.process(src.parse_detail, l, stats, s, allow_auto=False)
                if i % 5 == 0:
                    await say(f"Обработано {i} из {len(candidates)}, карточек: {stats['sent']}…")
            for l in seen_all:
                if not self.db.has(l.key):
                    self.db.insert(l, "seed")
            return dict(stats)

    async def ingest(self, listings: list) -> dict:
        """Объявления, пришедшие извне (сообщения бота-агрегатора): фильтры -> оценка -> карточка."""
        async with self.lock:
            s = self.get_settings()
            stats: Counter = Counter()
            for l in listings:
                if self.db.has(l.key):
                    stats["already"] += 1
                    continue
                await self.process(DETAIL[l.source], l, stats, s)
            return dict(stats)
