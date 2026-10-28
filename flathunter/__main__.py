from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

if os.name == "nt":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

EXIT_NOT_CONFIGURED = 3
EXIT_RESTART = 4


def _setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx на уровне INFO печатает полные URL, а в них токен бота.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def _check() -> None:
    from .db import DB
    from .settings import DATA_DIR, Settings
    from .sources import build_sources
    from .sources.base import Fetcher, FetchError

    s = Settings(DB(DATA_DIR / "flathunter.db")).get()
    sources = build_sources(s)
    if not sources:
        print("Нет ссылок на выдачу. Пришли их боту командой /links.")
        return
    fetcher = Fetcher()
    try:
        for src in sources:
            print(f"\n=== {src.name}: {src.search_url}")
            try:
                listings = src.parse_search(await fetcher.get(src.search_url))
            except FetchError as e:
                print(f"  Не загрузилось: {e}")
                continue
            print(f"  Найдено объявлений: {len(listings)}")
            for l in listings[:5]:
                print(f"  - {l.title[:70]!r} | {l.price} zł (+{l.admin_rent}) | {l.area} м² | {l.rooms} комн. | {l.location!r}")
            if not listings:
                continue
            first = listings[0]
            try:
                d = src.parse_detail(await fetcher.get(first.url), first)
            except FetchError as e:
                print(f"  Страница объявления не загрузилась: {e}")
                continue
            print(f"  Первое: описание {len(d.description)} симв. | czynsz {d.admin_rent} | kaucja {d.deposit} | телефоны {d.phones}")
    finally:
        await fetcher.aclose()


async def _login() -> None:
    from .sender import BrowserSender
    from .settings import DATA_DIR

    s = BrowserSender(DATA_DIR / "browser-profile", DATA_DIR / "shots")
    await s.open_login_pages()
    await asyncio.to_thread(input, "Войди в WhatsApp/OLX/Otodom в открывшемся окне и нажми Enter… ")
    await s.close()


def main() -> None:
    ap = argparse.ArgumentParser(prog="flathunter")
    ap.add_argument("cmd", choices=["setup", "ai", "run", "login", "check", "update"])
    args = ap.parse_args()
    from .net import drop_dead_proxies

    drop_dead_proxies()
    if args.cmd == "setup":
        from .setup_wizard import main as wizard

        wizard()
        return
    if args.cmd == "ai":
        from .settings import DATA_DIR, load_secrets
        from .setup_wizard import setup_claude

        DATA_DIR.mkdir(exist_ok=True)
        setup_claude(load_secrets())
        print("Готово. Перезапусти бота (закрой окно бота и открой start.bat).")
        return
    if args.cmd == "update":
        from .updater import TOKEN_URL, UpdateError, apply, available, has_token, save_token

        if not has_token():
            print("Для обновлений нужен GitHub-токен (один раз). Сейчас откроется GitHub:")
            print("  Token name: flat-hunter; Repository access: Only select repositories -> flat-hunter;")
            print("  Permissions -> Contents: Read-only. Нажми Generate token и скопируй его.")
            import webbrowser

            webbrowser.open(TOKEN_URL)
            tok = input("Вставь токен: ").strip()
            if not tok:
                print("Токен пустой, отмена.")
                sys.exit(1)
            save_token(tok)
        try:
            new = available()
            if not new:
                print("Уже последняя версия.")
                return
            print(f"Обновляю: {new['message']}")
            res = apply(new)
            print(f"Готово, обновлено файлов: {len(res['changed'])}.")
        except UpdateError as e:
            print(f"Не получилось обновить: {e}")
            sys.exit(1)
        return
    _setup_logging()
    if args.cmd == "check":
        asyncio.run(_check())
    elif args.cmd == "login":
        asyncio.run(_login())
    else:
        from .bot import FlatBot

        try:
            bot = FlatBot()
        except SystemExit as e:
            print(e)
            sys.exit(EXIT_NOT_CONFIGURED)
        bot.run()
        if bot.restart_requested:
            sys.exit(EXIT_RESTART)


if __name__ == "__main__":
    main()
