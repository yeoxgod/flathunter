"""Парсер OLX.pl (разметка со стабильными data-cy / data-testid).

Объявления, которые OLX показывает с Otodom, пропускаются: их находит парсер Otodom.
Проверить на живом сайте: `python -m flathunter check`.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ..models import Listing
from ..util import find_phones, parse_int, parse_number, parse_posted

BASE = "https://www.olx.pl"
_ID = re.compile(r"-ID([A-Za-z0-9]+)\.html")
_AREA = re.compile(r"(\d+(?:[.,]\d+)?)\s*m[²2]")


def _ext_id(href: str) -> str:
    m = _ID.search(href)
    return m.group(1) if m else href.split("?")[0].rstrip("/").rsplit("/", 1)[-1]


def _rooms(v: str) -> int | None:
    if "kawalerka" in v.lower() or "garsoniera" in v.lower():
        return 1
    return parse_int(v)


def parse_search(html: str) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select('[data-cy="l-card"]')
    if cards:
        items = [(c, c.find("a", href=True)) for c in cards]
    else:
        items = [(a.parent, a) for a in soup.select('a[href*="/d/oferta/"]')]
    out: list[Listing] = []
    seen: set[str] = set()
    for card, a in items:
        if a is None or card is None:
            continue
        href = urljoin(BASE, a["href"].split("#")[0])
        if "otodom.pl" in urlparse(href).netloc:
            continue
        ext = _ext_id(href)
        if ext in seen:
            continue
        seen.add(ext)
        title_el = card.select_one("h4, h6, h3, h2")
        title = (title_el or a).get_text(" ", strip=True)
        price_el = card.select_one('[data-testid="ad-price"]')
        loc_el = card.select_one('[data-testid="location-date"]')
        loc_text = loc_el.get_text(" ", strip=True) if loc_el else ""
        location = loc_text.split(" - ")[0]
        posted_dt = parse_posted(loc_text.split(" - ", 1)[1]) if " - " in loc_text else None
        am = _AREA.search(card.get_text(" ", strip=True))
        img = card.find("img")
        out.append(
            Listing(
                source="olx",
                ext_id=ext,
                url=href,
                title=title,
                price=parse_int(price_el.get_text(" ", strip=True)) if price_el else None,
                area=parse_number(am.group(1)) if am else None,
                location=location,
                photo=(img.get("src") or "") if img else "",
                posted=posted_dt.isoformat(timespec="minutes") if posted_dt else "",
            )
        )
    return out


def parse_detail(html: str, l: Listing) -> Listing:
    soup = BeautifulSoup(html, "html.parser")
    t = soup.select_one('[data-cy="ad_title"]') or soup.find("h1") or soup.select_one("h4")
    if t and t.get_text(strip=True):
        l.title = t.get_text(" ", strip=True)
    d = soup.select_one('[data-cy="ad_description"]')
    if d:
        l.description = d.get_text("\n", strip=True)
    price_el = soup.select_one('[data-testid="ad-price-container"]')
    if price_el:
        p = parse_int(price_el.get_text(" ", strip=True))
        if p:
            l.price = p
    for el in soup.select(
        '[data-testid="ad-parameters-container"] p, [data-testid="ad-parameters-container"] li'
    ):
        txt = el.get_text(" ", strip=True)
        if ":" not in txt:
            continue
        k, v = (x.strip() for x in txt.split(":", 1))
        l.params[k] = v
        kl = k.lower()
        if "czynsz" in kl:
            l.admin_rent = parse_int(v) or l.admin_rent
        elif "kaucj" in kl:
            l.deposit = parse_int(v) or l.deposit
        elif "powierzchnia" in kl:
            l.area = parse_number(v) or l.area
        elif "liczba pokoi" in kl:
            l.rooms = _rooms(v) or l.rooms
    loc = soup.select_one('[data-testid="map-aside-section"]')
    if loc:
        txt = loc.get_text(" ", strip=True).removeprefix("Lokalizacja").strip()
        if txt:
            l.location = txt
    l.phones = find_phones(l.description)
    return l
