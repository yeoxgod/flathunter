"""Мастер установки: Claude, Telegram-бот, вход в сайты. Всё в диалоге, файлы руками не правятся."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

import httpx

from .llm import GEMINI_DEFAULT_MODEL, ClaudeCodeLLM, GeminiLLM, LLMError, clean_env, find_claude, interfering_env, is_connection_error
from .settings import DATA_DIR, load_secrets, save_secrets


def say(text: str = "") -> None:
    print(text, flush=True)


def ask(prompt: str, default: str = "") -> str:
    try:
        v = input(f"{prompt} ").strip()
    except EOFError:
        v = ""
    return v or default


def yes(prompt: str, default: bool = True) -> bool:
    v = ask(f"{prompt} [{'Д/н' if default else 'д/Н'}]").lower()
    if not v:
        return default
    return v[0] in ("д", "y", "т")


def step(n: int, title: str) -> None:
    say()
    say(f"=== Шаг {n}. {title} ===")


# --- Claude --------------------------------------------------------------------------
def _auth_status(cmd: list[str]) -> dict:
    try:
        r = subprocess.run([*cmd, "auth", "status"], capture_output=True, text=True, timeout=120,
                           encoding="utf-8", errors="replace", env=clean_env())
        return json.loads(r.stdout or "{}")
    except Exception:
        return {}


def _install_claude() -> list[str] | None:
    say("Устанавливаю Claude Code официальным установщиком (claude.ai/install.ps1)…")
    if os.name == "nt":
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", "irm https://claude.ai/install.ps1 | iex"]
    else:
        cmd = ["bash", "-c", "curl -fsSL https://claude.ai/install.sh | bash"]
    subprocess.run(cmd)
    return find_claude()


def _user_settings_env() -> dict:
    p = Path.home() / ".claude" / "settings.json"
    try:
        return (json.loads(p.read_text(encoding="utf-8")) or {}).get("env") or {}
    except Exception:
        return {}


def _report_redirects() -> None:
    found = [f"переменная Windows {k}" for k in interfering_env()]
    found += [f"{k} в ~/.claude/settings.json" for k in _user_settings_env()
              if k.upper().startswith(("ANTHROPIC_", "CLAUDE_CODE_USE_"))]
    if found:
        say("Нашёл настройки, которые перенаправляют Claude Code (их ставят сторонние AI-утилиты):")
        for f in found:
            say(f"  - {f}")
        say("Бот их не использует: у него своя отдельная конфигурация Claude. Твои настройки я не трогаю.")


def _api_reachable() -> str:
    try:
        httpx.get("https://api.anthropic.com/", timeout=10)
        return "сервер Anthropic доступен"
    except Exception as e:
        return f"сервер Anthropic НЕдоступен: {type(e).__name__}: {e}"


def _test_llm(no_proxy: bool) -> str | None:
    llm = ClaudeCodeLLM("sonnet", DATA_DIR / "claude-work", no_proxy=no_proxy)
    try:
        out = asyncio.run(llm.structured(
            "Reply with the requested JSON.", "Return ok=true.",
            {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
        ))
        return None if out.get("ok") else f"неожиданный ответ: {out}"
    except LLMError as e:
        return str(e)


def setup_claude(sec: dict) -> None:
    step(1, "ИИ для разбора объявлений и текстов")
    say("1 — Claude по вашей подписке Pro/Max (лимиты общие с чатом);")
    say("2 — Gemini по бесплатному ключу Google AI Studio;")
    say("3 — Claude по API-ключу (оплата отдельно).")
    cur = {"claude_code": "1", "gemini": "2", "api": "3"}.get(sec.get("llm", ""), "1")
    choice = ask(f"Выбор [{cur}]:", cur)
    if choice == "2":
        setup_gemini(sec)
        return
    if choice == "3":
        key = ask("Вставь API-ключ Claude (console.anthropic.com → API Keys):")
        if key:
            sec.update(llm="api", anthropic_api_key=key)
            save_secrets(sec)
            say("Ключ сохранён.")
            return
        say("Ключ пустой, переключаюсь на подписку.")
    sec["llm"] = "claude_code"
    sec.pop("anthropic_api_key", None)

    exe = find_claude()
    if not exe:
        if not yes("Claude Code не установлен. Установить сейчас?"):
            raise SystemExit("Без Claude бот не сможет разбирать объявления. Запусти setup.bat снова.")
        exe = _install_claude()
        if not exe:
            raise SystemExit("Установка не нашлась. Закрой окно, открой setup.bat заново (обновится PATH).")
    say("Claude Code: " + " ".join(exe))
    _report_redirects()

    st = _auth_status(exe)
    if not st.get("loggedIn") or st.get("authMethod") not in ("claude.ai", "oauth_token"):
        say("Для бота нужен отдельный вход в Claude (один раз).")
        say("Сейчас откроется браузер: войди в аккаунт Claude с подпиской Pro/Max и подтверди доступ.")
        input("Нажми Enter, чтобы начать вход… ")
        subprocess.run([*exe, "auth", "login"], env=clean_env())
        st = _auth_status(exe)
        if not st.get("loggedIn"):
            raise SystemExit("Вход не завершён. Запусти setup.bat снова.")
    say(f"Вход выполнен ({st.get('authMethod')}).")

    say("Проверяю, что Claude отвечает (первый запуск может занять минуту-две)…")
    err = _test_llm(no_proxy=False)
    if err and is_connection_error(err):
        say("Нет соединения, пробую напрямую, без системного прокси…")
        if _test_llm(no_proxy=True) is None:
            sec["cc_no_proxy"] = True
            err = None
    if err:
        say(f"Claude не ответил: {err}")
        say(f"Проверка сети: {_api_reachable()}")
        say("Пришли весь этот текст в чат, где мы делали бота, и я поправлю.")
        raise SystemExit(1)
    save_secrets(sec)
    say("✓ Claude подключён.")


def setup_gemini(sec: dict) -> None:
    say("Ключ Gemini бесплатный: сейчас откроется Google AI Studio.")
    say("Нажми «Create API key», скопируй ключ и вставь сюда.")
    say("Учти: на бесплатном уровне Google может использовать запросы (тексты объявлений и")
    say("данные о тебе из /me) для улучшения своих продуктов.")
    while True:
        if not sec.get("gemini_api_key") or not yes("Ключ уже есть. Оставить его?"):
            webbrowser.open("https://aistudio.google.com/apikey")
            key = ask("Ключ Gemini:")
            if not key:
                continue
            sec["gemini_api_key"] = key
        say("Проверяю ключ…")
        llm = GeminiLLM(sec["gemini_api_key"], sec.get("gemini_model") or GEMINI_DEFAULT_MODEL)
        try:
            out = asyncio.run(llm.structured(
                "Reply with the requested JSON.", "Return ok=true.",
                {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
            ))
        except LLMError as e:
            say(f"Не получилось: {e}")
            sec.pop("gemini_api_key", None)
            if not yes("Попробовать другой ключ?"):
                raise SystemExit(1)
            continue
        if out.get("ok"):
            break
        say(f"Странный ответ: {out}")
    sec["llm"] = "gemini"
    sec["gemini_model"] = llm.model
    save_secrets(sec)
    say(f"✓ Gemini подключён ({llm.model}).")


# --- Telegram -----------------------------------------------------------------------
def _tg(token: str, method: str, **params) -> dict:
    r = httpx.post(f"https://api.telegram.org/bot{token}/{method}", json=params, timeout=40)
    return r.json()


def setup_telegram(sec: dict) -> None:
    step(2, "Telegram-бот")
    token = sec.get("telegram_token")
    if token and not _tg(token, "getMe").get("ok"):
        token = None
    if token and sec.get("chat_id"):
        if not yes("Бот уже настроен. Настроить заново?", False):
            say("✓ Оставляю текущего бота.")
            return
        token = None
    while not token:
        say("1) Сейчас откроется @BotFather в Telegram. Отправь ему /newbot,")
        say("   придумай имя и username (должен заканчиваться на bot).")
        say("2) BotFather пришлёт токен вида 123456789:AA… — скопируй его сюда.")
        webbrowser.open("https://t.me/BotFather")
        t = ask("Токен:")
        me = _tg(t, "getMe") if t else {}
        if me.get("ok"):
            token = t
            sec["bot_username"] = me["result"]["username"]
        else:
            say("Токен не подошёл, попробуй ещё раз.")
    sec["telegram_token"] = token
    username = sec.get("bot_username") or _tg(token, "getMe")["result"]["username"]

    _tg(token, "deleteWebhook")
    say(f"Теперь открой своего бота @{username} и нажми «Start» (или отправь любое сообщение).")
    webbrowser.open(f"https://t.me/{username}")
    offset, chat_id, deadline = None, None, time.time() + 600
    while not chat_id and time.time() < deadline:
        upd = _tg(token, "getUpdates", timeout=25, **({"offset": offset} if offset else {}))
        for u in upd.get("result", []):
            offset = u["update_id"] + 1
            m = u.get("message") or {}
            if m.get("chat", {}).get("type") == "private":
                chat_id = m["chat"]["id"]
                sec["owner_name"] = m.get("from", {}).get("first_name", "")
    if not chat_id:
        raise SystemExit("Не дождался сообщения боту. Запусти setup.bat снова.")
    if offset:
        _tg(token, "getUpdates", offset=offset, timeout=0)
    sec["chat_id"] = chat_id
    save_secrets(sec)
    _tg(token, "sendMessage", chat_id=chat_id, text="✅ Связь есть. Сейчас запущу бота и спрошу, что ищешь.")
    say("✓ Бот подключён. Отвечать он будет только тебе.")


# --- браузер ------------------------------------------------------------------------
def setup_browser() -> None:
    step(3, "Вход в WhatsApp, OLX и Otodom")
    say("Для отправки сообщений бот использует отдельное окно браузера на этом компьютере.")
    if not yes("Открыть его сейчас, чтобы войти?"):
        say("Можно сделать позже командой /login в боте.")
        return
    from .sender import BrowserSender

    async def run():
        s = BrowserSender(DATA_DIR / "browser-profile", DATA_DIR / "shots")
        await s.open_login_pages()
        say("В окне три вкладки:")
        say("  1) WhatsApp Web — на телефоне: WhatsApp → Связанные устройства → Привязать, отсканируй QR;")
        say("  2) OLX — войди в аккаунт;")
        say("  3) Otodom — войди (необязательно).")
        await asyncio.to_thread(input, "Когда закончишь, нажми здесь Enter… ")
        await s.close()

    asyncio.run(run())
    say("✓ Сессии сохранены.")


def setup_autostart() -> None:
    if os.name != "nt":
        return
    step(4, "Автозапуск")
    if not yes("Запускать бота автоматически при входе в Windows?", False):
        return
    startup = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    root = Path.cwd()
    (startup / "flat-hunter.bat").write_text(
        f'@echo off\r\ncd /d "{root}"\r\nstart "flat-hunter" /min "{root / "start.bat"}"\r\n', encoding="utf-8"
    )
    say("✓ Добавил в автозагрузку.")


def main() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    sec = load_secrets()
    say("Мастер настройки flat-hunter. Отвечай на вопросы, Enter = вариант по умолчанию.")
    setup_claude(sec)
    setup_telegram(sec)
    setup_browser()
    setup_autostart()
    say()
    say("Готово! Запускаю бота. Дальше всё настраивается в Telegram.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
