"""Разбор объявления из текста сообщения (бот-агрегатор в Telegram и т. п.).
Формат сообщений заранее неизвестен, поэтому: ссылки ищутся везде (текст, скрытые ссылки, кнопки),
поля — по общим шаблонам, а весь текст кладётся в params["post_text"], чтобы модель видела всё."""
from __future__ import annotations

import re
from urllib.parse import urlparse, urlunparse

from ..models import Listing
from ..util import parse_int, parse_number
from . import olx, otodom

URL_RE = re.compile(r"https?://[^\s<>\"'()\]]+")
_MONEY = re.compile(r"(\d[\d\s .]*\d|\d)\s*(?:zł|zl|pln|злот)", re.I)
_AREA = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:m²|m2|м²|м2|кв\.?\s*м|sq\.?\s*m)", re.I)
_ROOMS = re.compile(r"(\d)\s*[-_ ]?\s*(?:pok|комн|кімн|room|bedroom|-?к\b)", re.I)
_ROOMS_LABEL = re.compile(r"(?:pokoje|pokoi|liczba pokoi|комнат\w*|rooms?)\s*[:\-]?\s*(\d)", re.I)
_DISTRICT_LABEL = re.compile(r"(?:dzielnica|район|district|lokalizacja|локация|адрес|adres)\s*[:\-]\s*([^\n,;|]+)", re.I)
_EXTRA_WORDS = ("media", "czynsz", "opłat", "oplat", "dodatk", "administr", "коммун", "чинш")

WARSAW_DISTRICTS = [
    "Bemowo", "Białołęka", "Bielany", "Mokotów", "Ochota", "Praga-Południe", "Praga-Północ", "Praga Południe",
    "Praga Północ", "Rembertów", "Śródmieście", "Targówek", "Ursus", "Ursynów", "Wawer", "Wesoła", "Wilanów",
    "Włochy", "Wola", "Żoliborz",
]


def clean_listing_url(url: str) -> tuple[str, str] | None:
    """-> (source, clean_url) для ссылок на объявление Otodom/OLX."""
    p = urlparse(url.rstrip(".,);]"))
    host = p.netloc.lower()
    if host.endswith("olx.pl") and "/d/oferta/" in p.path:
        return "olx", urlunparse(("https", p.netloc, p.path, "", "", ""))
    if host.endswith("otodom.pl") and "/oferta/" in p.path:
        return "otodom", urlunparse(("https", p.netloc, p.path, "", "", ""))
    return None


def listings_from_text(text: str, extra_urls: list[str] | None = None, posted: str = "") -> list[Listing]:
    urls = list(extra_urls or []) + URL_RE.findall(text or "")
    found: dict[str, Listing] = {}
    flat = re.sub(r"\s+", " ", text or "").strip()
    fields = fields_from_text(text or "")
    for u in urls:
        c = clean_listing_url(u)
        if not c:
            continue
        source, clean = c
        if source == "olx":
            ext = olx._ext_id(clean)
        else:
            m = otodom._ID_FROM_URL.search(clean)
            ext = m.group(1) if m else clean.rstrip("/").rsplit("/", 1)[-1]
        key = f"{source}:{ext}"
        if key in found:
            continue
        found[key] = Listing(
            source=source, ext_id=ext, url=clean, posted=posted,
            params={"post_text": flat[:1500], **({"seller": fields["seller"]} if fields.get("seller") else {})},
            **{k: v for k, v in fields.items() if k in ("title", "price", "admin_rent", "area", "rooms", "location")},
        )
    # в одном сообщении обычно одно объявление; если ссылок несколько, поля из текста относятся к первой
    items = list(found.values())
    for l in items[1:]:
        l.price = l.admin_rent = l.area = l.rooms = None
    return items


def fields_from_text(text: str) -> dict:
    lines = [x.strip() for x in (text or "").split("\n") if x.strip() and not URL_RE.fullmatch(x.strip())]
    flat = " ".join(lines)
    out: dict = {}
    if lines:
        out["title"] = URL_RE.sub("", lines[0]).strip(" -—:|")[:150]
    money = [parse_int(m) for m in _MONEY.findall(flat)]
    money = [m for m in money if m and 300 <= m <= 100000]
    if money:
        out["price"] = money[0]
        if len(money) > 1 and any(w in flat.lower() for w in _EXTRA_WORDS) and money[1] < money[0]:
            out["admin_rent"] = money[1]
    am = _AREA.search(flat)
    if am:
        out["area"] = parse_number(am.group(1))
    rm = _ROOMS.search(flat) or _ROOMS_LABEL.search(flat)
    if rm:
        out["rooms"] = int(rm.group(1))
    elif re.search(r"kawalerk|garsonier|studio|студи", flat, re.I):
        out["rooms"] = 1
    dm = _DISTRICT_LABEL.search(text or "")
    if dm:
        out["location"] = dm.group(1).strip()[:80]
    else:
        low = flat.lower()
        for d in WARSAW_DISTRICTS:
            if d.lower() in low or d.lower().replace("ó", "o").replace("ł", "l").replace("ś", "s").replace("ż", "z") in low:
                out["location"] = d
                break
    low = flat.lower()
    if any(w in low for w in ("agencj", "biuro nieruch", "агентств", "agency")):
        out["seller"] = "агентство"
    elif any(w in low for w in ("prywatn", "właściciel", "wlasciciel", "собственник", "владел", "private")):
        out["seller"] = "частное лицо"
    return out
