"""Ссылки на выдачу Otodom и OLX, собранные из критериев (/criteria).

Фильтры сайта задаются с запасом: цена на сайтах обычно без czynsz, поэтому берём верхнюю границу
бюджета как есть, а точную проверку «аренда + czynsz <= бюджет» делает локальный фильтр бота.
Районы в ссылку не кладём (у сайтов свои коды районов) — их тоже проверяет локальный фильтр.
"""
from __future__ import annotations

from urllib.parse import urlencode

OTODOM_BASE = "https://www.otodom.pl/pl/wyniki/wynajem/mieszkanie/mazowieckie/warszawa/warszawa/warszawa"
OLX_BASE = "https://www.olx.pl/nieruchomosci/mieszkania/wynajem/warszawa/"

_OTODOM_ROOMS = {1: "ONE", 2: "TWO", 3: "THREE", 4: "FOUR", 5: "FIVE", 6: "SIX", 7: "SEVEN", 8: "EIGHT", 9: "NINE"}
_OLX_ROOMS = {1: "one", 2: "two", 3: "three", 4: "four"}  # «four» на OLX = 4 и больше


def otodom_url(c: dict) -> str:
    q: list[tuple[str, str]] = []
    if c.get("max_price"):
        q.append(("priceMax", str(c["max_price"])))
    if c.get("min_price"):
        q.append(("priceMin", str(c["min_price"])))
    if c.get("min_area"):
        q.append(("areaMin", str(c["min_area"])))
    if c.get("max_area"):
        q.append(("areaMax", str(c["max_area"])))
    rooms = [_OTODOM_ROOMS[r] for r in sorted(set(c.get("rooms") or [])) if r in _OTODOM_ROOMS]
    if rooms:
        q.append(("roomsNumber", "[" + ",".join(rooms) + "]"))
    q += [("by", "LATEST"), ("direction", "DESC"), ("limit", "72")]
    return OTODOM_BASE + "?" + urlencode(q)


def olx_url(c: dict) -> str:
    q: list[tuple[str, str]] = [("search[order]", "created_at:desc")]
    if c.get("max_price"):
        q.append(("search[filter_float_price:to]", str(c["max_price"])))
    if c.get("min_price"):
        q.append(("search[filter_float_price:from]", str(c["min_price"])))
    if c.get("min_area"):
        q.append(("search[filter_float_m:from]", str(c["min_area"])))
    if c.get("max_area"):
        q.append(("search[filter_float_m:to]", str(c["max_area"])))
    rooms = sorted({_OLX_ROOMS[min(r, 4)] for r in (c.get("rooms") or []) if r >= 1},
                   key=list(_OLX_ROOMS.values()).index)
    for i, r in enumerate(rooms):
        q.append((f"search[filter_enum_rooms][{i}]", r))
    return OLX_BASE + "?" + urlencode(q)


def apply_auto_links(settings: dict) -> dict:
    """Обновляет ссылки, которые бот ведёт сам (sources.X.auto != False). Возвращает settings."""
    c = settings.get("search") or {}
    for name, build in (("otodom", otodom_url), ("olx", olx_url)):
        src = settings["sources"][name]
        if src.get("auto", True):
            src["search_url"] = build(c)
            src["auto"] = True
    return settings
