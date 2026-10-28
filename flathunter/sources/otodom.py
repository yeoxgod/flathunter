"""Парсер Otodom.

Otodom — Next.js-сайт: данные лежат в <script id="__NEXT_DATA__"> в виде JSON.
Структура JSON может меняться, поэтому поиск полей сделан по ключам, а не по
жёстким путям, и есть запасной разбор по ссылкам /pl/oferta/.
Проверить на живом сайте: `python -m flathunter check`.
"""
from __future__ import annotations

import json
import re

from bs4 import BeautifulSoup

from ..models import Listing
from ..util import find_phones, parse_int, parse_number

BASE = "https://www.otodom.pl"
_ROOMS = {
    "ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5,
    "SIX": 6, "SEVEN": 7, "EIGHT": 8, "NINE": 9, "TEN": 10,
}
_ID_FROM_URL = re.compile(r"-ID([A-Za-z0-9]+)(?:[/?#]|$)")


def _next_data(html: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("script", id="__NEXT_DATA__")
    if tag is None:
        return None
    raw = tag.string if tag.string else tag.get_text()
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def _walk(obj):
    """Обход JSON в глубину с сохранением порядка."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _money(v) -> int | None:
    if isinstance(v, dict):
        v = v.get("value")
    return parse_int(v)


def _rooms(v) -> int | None:
    if isinstance(v, list):
        return _rooms(v[0]) if v else None
    if isinstance(v, str) and v.upper() in _ROOMS:
        return _ROOMS[v.upper()]
    return parse_int(v)


def _label(v) -> str:
    if isinstance(v, dict):
        v = v.get("value") or v.get("fullName") or ""
    return str(v or "")


def _posted(d: dict) -> str:
    for k in ("dateCreatedFirst", "createdAtFirst", "dateCreated", "createdAt", "pushedUpAt", "modifiedAt"):
        v = d.get(k)
        if isinstance(v, str) and v[:4].isdigit():
            return v
    return ""


def parse_search(html: str) -> list[Listing]:
    data = _next_data(html)
    out: list[Listing] = []
    seen: set[str] = set()
    if data:
        for d in _walk(data):
            slug, ext = d.get("slug"), d.get("id")
            if not isinstance(slug, str) or ext is None or "title" not in d:
                continue
            ext = str(ext)
            if ext in seen:
                continue
            seen.add(ext)
            photo = ""
            imgs = d.get("images")
            if isinstance(imgs, list) and imgs and isinstance(imgs[0], dict):
                photo = imgs[0].get("medium") or imgs[0].get("large") or imgs[0].get("small") or ""
            out.append(
                Listing(
                    source="otodom",
                    ext_id=ext,
                    url=f"{BASE}/pl/oferta/{slug}",
                    title=str(d.get("title") or ""),
                    price=_money(d.get("totalPrice")),
                    admin_rent=_money(d.get("rentPrice")),
                    area=parse_number(d.get("areaInSquareMeters")),
                    rooms=_rooms(d.get("roomsNumber")),
                    location=_label(d.get("locationLabel")),
                    photo=photo,
                    posted=_posted(d),
                )
            )
    return out or _parse_search_fallback(html)


def _parse_search_fallback(html: str) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in soup.select('a[href*="/pl/oferta/"]'):
        href = a["href"].split("#")[0]
        url = href if href.startswith("http") else BASE + href
        m = _ID_FROM_URL.search(url)
        ext = m.group(1) if m else url.rstrip("/").rsplit("/", 1)[-1]
        if ext in seen:
            continue
        seen.add(ext)
        out.append(Listing("otodom", ext, url, title=a.get_text(" ", strip=True)))
    return out


def _find_ad(data: dict) -> dict | None:
    ad = ((data.get("props") or {}).get("pageProps") or {}).get("ad")
    if isinstance(ad, dict):
        return ad
    for d in _walk(data):
        if "description" in d and isinstance(d.get("target"), dict):
            return d
    return None


def _address(ad: dict) -> str:
    try:
        loc = ad.get("location") or {}
        addr = loc.get("address") or {}
        parts = []
        for k in ("street", "district", "city"):
            v = addr.get(k)
            if isinstance(v, dict):
                v = v.get("name")
            if v:
                parts.append(str(v))
        if parts:
            return ", ".join(parts)
        locs = (loc.get("reverseGeocoding") or {}).get("locations") or []
        names = [x.get("fullName") for x in locs if isinstance(x, dict) and x.get("fullName")]
        return names[-1] if names else ""
    except AttributeError:
        return ""


def _html_to_text(html: str) -> str:
    text = BeautifulSoup(html or "", "html.parser").get_text("\n")
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def parse_detail(html: str, l: Listing) -> Listing:
    data = _next_data(html)
    ad = _find_ad(data) if data else None
    if ad:
        l.title = str(ad.get("title") or l.title)
        l.description = _html_to_text(str(ad.get("description") or ""))
        target = ad.get("target") or {}
        l.price = _money(target.get("Price")) or l.price
        l.admin_rent = _money(target.get("Rent")) or l.admin_rent
        l.deposit = _money(target.get("Deposit")) or l.deposit
        l.area = parse_number(target.get("Area")) or l.area
        l.rooms = _rooms(target.get("Rooms_num")) or l.rooms
        for c in ad.get("characteristics") or []:
            if not isinstance(c, dict):
                continue
            label = c.get("label") or c.get("key")
            val = c.get("localizedValue") or c.get("value")
            if label and val is not None:
                l.params[str(label)] = val
            key = str(c.get("key") or "").lower()
            if key == "rent" and not l.admin_rent:
                l.admin_rent = _money(val)
            elif key == "deposit" and not l.deposit:
                l.deposit = _money(val)
        l.location = _address(ad) or l.location
    else:
        soup = BeautifulSoup(html, "html.parser")
        h1 = soup.find("h1")
        if h1:
            l.title = h1.get_text(" ", strip=True) or l.title
        d = soup.select_one('[data-cy="adPageAdDescription"]')
        if d:
            l.description = d.get_text("\n", strip=True)
    l.phones = find_phones(l.description)
    return l
