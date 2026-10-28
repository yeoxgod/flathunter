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


class UpdateError(Exception):
    pass


def installed() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def latest() -> dict:
    """{'sha', 'message', 'date'} последнего коммита в GitHub."""
    try:
        r = httpx.get(f"https://api.github.com/repos/{REPO}/commits/{BRANCH}", timeout=20,
                      headers={"Accept": "application/vnd.github+json"})
        r.raise_for_status()
        d = r.json()
    except Exception as e:
        raise UpdateError(f"GitHub недоступен: {e}")
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
        r = httpx.get(f"https://codeload.github.com/{REPO}/zip/{target['sha']}", timeout=120, follow_redirects=True)
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
