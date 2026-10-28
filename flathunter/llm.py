"""Доступ к Claude: через подписку (Claude Code, `claude -p`) или через API-ключ."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

IS_WIN = os.name == "nt"


class LLMError(Exception):
    pass


def _resolve_shim(shim: Path) -> list[str] | None:
    """npm-обёртка claude.cmd -> настоящий claude.exe (или node + cli.js).
    Через cmd.exe длинные аргументы с кавычками и переводами строк ломаются, поэтому запускаем напрямую."""
    d = shim.parent
    try:
        text = shim.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        text = ""
    for m in re.finditer(r'"%~?dp0%?\\?([^"%]+)"', text):
        target = d / m.group(1).replace("\\", "/").lstrip("/")
        if target.name.lower() == "node.exe":
            continue
        if target.suffix.lower() == ".exe" and target.is_file():
            return [str(target)]
        if target.suffix.lower() in (".js", ".mjs", ".cjs") and target.is_file():
            node = shutil.which("node") or str(d / "node.exe")
            return [node, str(target)]
    pkg = d / "node_modules" / "@anthropic-ai"
    if pkg.is_dir():
        for exe in sorted(pkg.rglob("claude.exe")):
            return [str(exe)]
    return None


def find_claude() -> list[str] | None:
    """Команда для запуска Claude Code (список аргументов) или None."""
    home_bin = Path.home() / ".local" / "bin"
    for cand in (home_bin / "claude.exe", home_bin / "claude"):
        if cand.is_file():
            return [str(cand)]
    found = shutil.which("claude")
    cands = [Path(found)] if found else []
    if IS_WIN:
        cands.append(Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd")
    for c in cands:
        if not c.is_file():
            continue
        if c.suffix.lower() in (".cmd", ".bat"):
            real = _resolve_shim(c)
            if real:
                return real
            continue
        return [str(c)]
    return None


PROXY_VARS = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY")
_REDIRECT_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_USE_")


def claude_config_dir() -> Path:
    from .settings import DATA_DIR

    return (DATA_DIR / "claude-config").resolve()


def interfering_env() -> list[str]:
    """Переменные, которые перенаправляют Claude Code на другой сервер/ключ (бот их не передаёт)."""
    return sorted(k for k in os.environ if k.upper().startswith(_REDIRECT_PREFIXES))


def clean_env(no_proxy: bool = False) -> dict:
    """Окружение для Claude Code, не зависящее от чужих настроек на этом ПК:
    - убраны ANTHROPIC_* (ключи, ANTHROPIC_BASE_URL на локальные роутеры и т. п.) и CLAUDE_CODE_USE_*;
    - своя папка конфигурации (CLAUDE_CONFIG_DIR): ~/.claude/settings.json с его env не подхватывается,
      вход в аккаунт для бота отдельный."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(_REDIRECT_PREFIXES)}
    cfg = claude_config_dir()
    cfg.mkdir(parents=True, exist_ok=True)
    env["CLAUDE_CONFIG_DIR"] = str(cfg)
    if no_proxy:
        for k in list(env):
            if k.upper() in PROXY_VARS:
                env.pop(k)
    return env


def is_connection_error(msg: str) -> bool:
    m = msg.lower()
    return any(x in m for x in ("connection refused", "connectionrefused", "econnrefused", "firewall or proxy",
                                "unable to connect", "econnreset", "enotfound", "fetch failed"))


async def _kill(proc) -> None:
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
        else:
            proc.kill()
    except Exception:
        pass
    try:
        await asyncio.wait_for(proc.wait(), 10)
    except Exception:
        pass


class ClaudeCodeLLM:
    """Вызывает `claude -p` с вашей подпиской Pro/Max. Расход идёт из лимитов подписки."""

    def __init__(self, model: str = "sonnet", workdir: str | Path = "data/claude-work", timeout: int = 240,
                 no_proxy: bool = False):
        self.cmd = find_claude()
        self.no_proxy = no_proxy
        self.model = model
        self.workdir = Path(workdir)
        self.timeout = timeout

    async def _run(self, system: str, user: str, schema: dict | None) -> dict:
        if not self.cmd:
            raise LLMError("Claude Code не найден. Запусти setup.bat ещё раз.")
        self.workdir.mkdir(parents=True, exist_ok=True)
        tag = uuid.uuid4().hex[:8]
        sys_file = self.workdir / f"system-{tag}.txt"
        err_file = self.workdir / f"stderr-{tag}.txt"
        sys_file.write_text(system, encoding="utf-8")
        args = [
            *self.cmd, "-p",
            "--output-format", "json",
            "--system-prompt-file", str(sys_file.resolve()),
            "--tools", "",
            "--strict-mcp-config",
            "--no-session-persistence",
            "--model", self.model,
        ]
        if schema:
            args += ["--json-schema", json.dumps(schema, ensure_ascii=True, separators=(",", ":"))]
        try:
            with open(err_file, "wb") as errf:
                proc = await asyncio.create_subprocess_exec(
                    *args,
                    cwd=str(self.workdir),
                    env=clean_env(self.no_proxy),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=errf,
                )
                try:
                    out, _ = await asyncio.wait_for(proc.communicate(user.encode("utf-8")), self.timeout)
                except asyncio.TimeoutError:
                    await _kill(proc)
                    raise LLMError(f"Claude не ответил за {self.timeout} с. {self._tail(err_file)}".strip())
            text = out.decode("utf-8", "replace").strip()
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                msg = (text or self._tail(err_file)).strip()[:400]
                raise LLMError(f"Claude Code: {msg or 'пустой ответ'}")
            if data.get("is_error"):
                raise LLMError(f"Claude Code: {str(data.get('result') or data.get('subtype'))[:300]}")
            return data
        finally:
            for f in (sys_file, err_file):
                try:
                    f.unlink()
                except OSError:
                    pass

    @staticmethod
    def _tail(path: Path) -> str:
        try:
            t = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return ""
        return f"stderr: {t[-400:]}" if t else ""

    async def structured(self, system: str, user: str, schema: dict) -> dict:
        data = await self._run(system, user, schema)
        so = data.get("structured_output")
        if isinstance(so, dict):
            return so
        try:
            return json.loads(data.get("result") or "")
        except (json.JSONDecodeError, TypeError):
            raise LLMError("Claude не вернул структурированный ответ")

    async def text(self, system: str, user: str) -> str:
        data = await self._run(system, user, None)
        return str(data.get("result") or "").strip()


class ApiLLM:
    """Запасной вариант: Claude API по ключу (оплачивается отдельно)."""

    def __init__(self, api_key: str, model: str = "claude-sonnet-5-5", client=None):
        if client is None:
            from anthropic import AsyncAnthropic

            client = AsyncAnthropic(api_key=api_key)
        self.client = client
        self.model = model

    async def structured(self, system: str, user: str, schema: dict) -> dict:
        tool = {"name": "report", "description": "Return the result.", "input_schema": schema}
        resp = await self.client.messages.create(
            model=self.model, max_tokens=2000, system=system, tools=[tool],
            tool_choice={"type": "tool", "name": "report"},
            messages=[{"role": "user", "content": user}],
        )
        for b in resp.content:
            if getattr(b, "type", None) == "tool_use":
                return b.input
        raise LLMError("модель не вернула результат")

    async def text(self, system: str, user: str) -> str:
        resp = await self.client.messages.create(
            model=self.model, max_tokens=800, system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text").strip()


GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_DEFAULT_MODEL = "gemini-3.8-flash"
GEMINI_FALLBACK_MODELS = ("gemini-flash-latest", "gemini-3.6-flash", "gemini-3.5-flash-lite")
_SCHEMA_KEYS = {"type", "description", "properties", "required", "items", "enum"}


def _gemini_schema(schema: dict) -> dict:
    """JSON Schema -> подмножество, которое точно принимает responseSchema Gemini."""
    out = {}
    for k, v in schema.items():
        if k not in _SCHEMA_KEYS:
            continue
        if k == "properties":
            out[k] = {name: _gemini_schema(sub) for name, sub in v.items()}
        elif k == "items":
            out[k] = _gemini_schema(v)
        else:
            out[k] = v
    return out


def _extract_json(text: str) -> dict:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            return json.loads(m.group(0))
        raise


class GeminiLLM:
    """Gemini API по ключу из Google AI Studio (есть бесплатный уровень)."""

    def __init__(self, api_key: str, model: str = GEMINI_DEFAULT_MODEL, client=None, timeout: float = 120):
        import httpx

        self.api_key = api_key
        self.model = model
        self.client = client or httpx.AsyncClient(timeout=timeout)

    async def _call(self, system: str, user: str, gen: dict) -> str:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": gen,
        }
        models = [self.model] + [m for m in GEMINI_FALLBACK_MODELS if m != self.model]
        last = ""
        for attempt, model in enumerate(models):
            for retry in range(3):
                try:
                    r = await self.client.post(GEMINI_URL.format(model=model), json=body,
                                               headers={"x-goog-api-key": self.api_key})
                except Exception as e:
                    last = f"сеть: {e}"
                    await asyncio.sleep(2 * (retry + 1))
                    continue
                if r.status_code == 404:  # модель переименовали или убрали
                    last = f"модель {model} не найдена"
                    break
                if r.status_code in (429, 500, 503):
                    last = f"HTTP {r.status_code}: {r.text[:200]}"
                    if r.status_code == 429 and retry == 2:
                        raise LLMError("Gemini: превышен лимит запросов (бесплатный уровень). Попробую позже.")
                    await asyncio.sleep(5 * (retry + 1))
                    continue
                if r.status_code in (400, 401, 403):
                    raise LLMError(f"Gemini HTTP {r.status_code}: {r.text[:300]}")
                r.raise_for_status()
                data = r.json()
                if model != self.model:
                    self.model = model
                cands = data.get("candidates") or []
                if not cands:
                    raise LLMError(f"Gemini не ответил: {str(data.get('promptFeedback') or data)[:300]}")
                parts = (cands[0].get("content") or {}).get("parts") or []
                text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
                if not text.strip():
                    raise LLMError(f"Gemini вернул пустой ответ ({cands[0].get('finishReason')})")
                return text
        raise LLMError(f"Gemini недоступен: {last}")

    async def structured(self, system: str, user: str, schema: dict) -> dict:
        gen = {"responseMimeType": "application/json", "responseSchema": _gemini_schema(schema), "temperature": 0.4}
        try:
            text = await self._call(system, user, gen)
        except LLMError as e:
            if "400" not in str(e):
                raise
            # схему не приняли: просим JSON текстом
            text = await self._call(
                system + "\n\nReturn ONLY a JSON object matching this JSON Schema:\n" + json.dumps(schema, ensure_ascii=False),
                user, {"responseMimeType": "application/json", "temperature": 0.4},
            )
        try:
            return _extract_json(text)
        except (json.JSONDecodeError, ValueError):
            raise LLMError("Gemini вернул некорректный JSON")

    async def text(self, system: str, user: str) -> str:
        return (await self._call(system, user, {"temperature": 0.6})).strip()


def build_llm(secrets: dict, data_dir: str | Path):
    if secrets.get("llm") == "gemini" and secrets.get("gemini_api_key"):
        return GeminiLLM(secrets["gemini_api_key"], secrets.get("gemini_model") or GEMINI_DEFAULT_MODEL)
    if secrets.get("llm") == "api" and secrets.get("anthropic_api_key"):
        return ApiLLM(secrets["anthropic_api_key"], secrets.get("api_model") or "claude-sonnet-5-5")
    return ClaudeCodeLLM(secrets.get("cc_model") or "sonnet", Path(data_dir) / "claude-work",
                         no_proxy=bool(secrets.get("cc_no_proxy")))
