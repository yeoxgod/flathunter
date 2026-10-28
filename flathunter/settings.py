"""Секреты (токен, chat id, способ доступа к Claude) лежат в data/secrets.json и пишутся мастером установки.
Всё остальное (критерии, профиль, ссылки, режимы) хранится в БД и меняется из бота."""
from __future__ import annotations

import copy
import json
from pathlib import Path

DATA_DIR = Path("data")

DEFAULTS = {
    "search": {
        "max_price": 0, "min_price": 0, "min_area": 0, "max_area": 0,
        "rooms": [], "districts": [], "exclude_keywords": [], "wishes": "", "summary_ru": "",
    },
    "profile": {k: "" for k in ("name", "about", "move_in", "lease", "occupants", "pets", "extra", "email", "phone")},
    "sources": {
        "otodom": {"enabled": True, "search_url": "", "auto": True},
        "olx": {"enabled": True, "search_url": "", "auto": True},
        "channel": {"enabled": False, "name": "home_Warszawa"},
        "tgbot": {"enabled": False, "chat": "thewarsawrentbot"},
        "channels": [],  # id каналов, из которых бот читает посты (подтверждённые владельцем)
    },
    "poll_interval_minutes": 7,
    "max_new_per_cycle": 8,
    "min_score": 5,
    "seed_on_first_run": True,
    "send": {
        "mode": "manual",          # manual | auto
        "dry_run": True,           # тестовый режим: вставляет текст, но не нажимает «Отправить»
        "auto_min_score": 8,
        "auto_delay_minutes": 3,
        "daily_limit": 10,
        "hours": [8, 22],
        "channels": {"whatsapp": True, "olx": True, "otodom": True},
    },
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Settings:
    KEY = "settings"

    def __init__(self, db):
        self.db = db

    def get(self) -> dict:
        raw = self.db.kv_get(self.KEY)
        return _merge(DEFAULTS, json.loads(raw) if raw else {})

    def save(self, s: dict) -> None:
        self.db.kv_set(self.KEY, json.dumps(s, ensure_ascii=False))

    def update(self, path: str, value) -> dict:
        s = self.get()
        node = s
        keys = path.split(".")
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        node[keys[-1]] = value
        self.save(s)
        return s


def secrets_path(data_dir: Path = DATA_DIR) -> Path:
    return Path(data_dir) / "secrets.json"


def load_secrets(data_dir: Path = DATA_DIR) -> dict:
    p = secrets_path(data_dir)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_secrets(sec: dict, data_dir: Path = DATA_DIR) -> None:
    p = secrets_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(sec, ensure_ascii=False, indent=2), encoding="utf-8")
