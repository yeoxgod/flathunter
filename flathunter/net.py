"""Если в системе задан прокси (HTTPS_PROXY и т. п.), а он не запущен, все запросы бота падают
с «connection refused». Проверяем, отвечает ли прокси, и если нет, работаем напрямую."""
from __future__ import annotations

import logging
import os
import socket
from urllib.parse import urlparse

log = logging.getLogger(__name__)
PROXY_VARS = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY")


def _alive(url: str, timeout: float = 3.0) -> bool:
    p = urlparse(url if "://" in url else f"http://{url}")
    if not p.hostname:
        return False
    port = p.port or (443 if p.scheme == "https" else 1080 if p.scheme.startswith("socks") else 80)
    try:
        with socket.create_connection((p.hostname, port), timeout=timeout):
            return True
    except OSError:
        return False


def drop_dead_proxies() -> list[str]:
    """Убирает из окружения процесса неработающие прокси. Возвращает список убранных переменных."""
    removed = []
    for k in list(os.environ):
        if k.upper() in PROXY_VARS and os.environ[k].strip() and not _alive(os.environ[k]):
            removed.append(f"{k}={os.environ[k]}")
            del os.environ[k]
    if removed:
        log.warning("Прокси не отвечает, работаю без него: %s", ", ".join(removed))
    return removed
