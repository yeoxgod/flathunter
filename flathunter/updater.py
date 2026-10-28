"""Обновление из GitHub: скачивает свежую версию и заменяет файлы программы.
Папки data/ (настройки, база, входы) и .venv/ не трогаются."""
from __future__ import annotations

import hashlib
import io
import json
import shutil
import zipfile
from pathlib import Path

import httpx

REPO = "yeoxgod/flat-hunter"
BRANCH = "main"
ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "data" / "installed_version.json"
NEEDS_PIP = ROOT / "data" / "needs_pip"
SKIP_TOP = {"data", ".venv", ".git"}


TOKEN_URL = "https://github.com/settings/personal-access-tokens/new"


class UpdateError(Exception):
    pass


def _token() -> str:
    try:
        return json.loads((ROOT / "data" / "secrets.json").read_text(encoding="utf-8")).get("github_token", "")
    except Exception:
        return ""


def save_token(token: str) -> None:
    p = ROOT / "data" / "secrets.json"
    try:
        sec = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        sec = {}
    sec["github_token"] = token.strip()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(sec, ensure_ascii=False, indent=2), encoding="utf-8")


def _headers() -> dict:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    t = _token()
    if t:
        h["Authorization"] = f"Bearer {t}"
    return h


def has_token() -> bool:
    return bool(_token())


def installed() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def latest() -> dict:
    """{'sha', 'message', 'date'} последнего коммита в GitHub."""
    try:
        r = httpx.get(f"https://api.github.com/repos/{REPO}/commits/{BRANCH}", timeout=20, headers=_headers())
    except Exception as e:
        raise UpdateError(f"GitHub недоступен: {e}")
    if r.status_code in (401, 403, 404):
        raise UpdateError("нет доступа к репозиторию: нужен GitHub-токен (запусти update.bat, он спросит)"
                          if not has_token() else f"GitHub отказал в доступе (HTTP {r.status_code}): проверь токен, запусти update.bat")
    try:
        r.raise_for_status()
        d = r.json()
    except Exception as e:
        raise UpdateError(f"GitHub: {e}")
    return {"sha": d["sha"], "message": d["commit"]["message"].split("\n")[0], "date": d["commit"]["committer"]["date"]}


def available() -> dict | None:
    lt = latest()
    return None if installed().get("sha") == lt["sha"] else lt


def _sha(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()


def apply(target: dict | None = None) -> dict:
    """Скачивает и раскладывает файлы. Возвращает {'sha','message','changed':[...],'pip':bool}."""
    target = target or latest()
    try:
        r = httpx.get(f"https://api.github.com/repos/{REPO}/zipball/{target['sha']}", timeout=180,
                      follow_redirects=True, headers=_headers())
        r.raise_for_status()
    except Exception as e:
        raise UpdateError(f"не скачалось: {e}")
    changed, pip = [], False
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            parts = Path(info.filename).parts[1:]  # без корневой папки архива
            if not parts or parts[0] in SKIP_TOP or "__pycache__" in parts:
                continue
            dest = ROOT.joinpath(*parts)
            data = z.read(info)
            if dest.suffix.lower() in (".bat", ".cmd"):
                data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            if dest.exists() and _sha(dest.read_bytes()) == _sha(data):
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(dest.suffix + ".new")
            tmp.write_bytes(data)
            tmp.replace(dest)
            changed.append("/".join(parts))
            if parts[-1] == "requirements.txt":
                pip = True
    pyc = ROOT / "flathunter" / "__pycache__"
    if changed and pyc.exists():
        shutil.rmtree(pyc, ignore_errors=True)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    if pip:
        NEEDS_PIP.write_text("1", encoding="utf-8")
    STATE.write_text(json.dumps(target, ensure_ascii=False), encoding="utf-8")
    return {**target, "changed": changed, "pip": pip}
