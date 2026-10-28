"""Источник: публичный Telegram-канал через веб-превью https://t.me/s/<канал>.

Логин не нужен, личный аккаунт не используется. Превью отдаёт последние ~20 постов.
Из поста берём ссылку на OLX и то, что есть в тексте (цена, площадь, район, продавец);
полный текст объявления потом подтягивается со страницы OLX (parse_detail).
Формат постов проверен только по описанию, поэтому весь текст поста целиком
кладётся в params["post_text"], и модель видит его независимо от разбора полей.
Проверить на живом канале: `python -m flathunter check`.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse, urlunparse

from bs4 import BeautifulSoup

from ..models import Listing
from ..util import parse_int, parse_number
from . import olx

_MONEY = re.compile(r"(\d[\d\s ]*)\s*(?:zł|zl|pln)", re.I)
_AREA = re.compile(r"(\d+(?:[.,]\d+)?)\s*m[²2]", re.I)
_ROOMS = re.compile(r"(\d)\s*[_\s-]?\s*pok", re.I)
_EXTRA_WORDS = ("media", "czynsz", "opłat", "oplat", "dodatk", "administr")


def _olx_url(href: str) -> str | None:
    p = urlparse(href)
    if not p.netloc.endswith("olx.pl") or "/d/oferta/" not in p.path:
        return None
    return urlunparse((p.scheme or "https", p.netloc, p.path, "", "", ""))


def _post_number(data_post: str) -> int:
    try:
        return int(data_post.rsplit("/", 1)[-1])
    except ValueError:
        return 0


def parse_search(html: str) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    posts = []
    for msg in soup.select("div.tgme_widget_message"):
        text_el = msg.select_one(".tgme_widget_message_text")
        if text_el is None:
            continue
        url = None
        for a in msg.find_all("a", href=True):
            url = _olx_url(a["href"])
            if url:
                break
        if not url:
            continue
        data_post = msg.get("data-post", "")
        flat = text_el.get_text(" ", strip=True)
        lines = [x for x in text_el.get_text("\n", strip=True).split("\n") if x.strip()]
        hashtags = [a.get_text(strip=True) for a in text_el.find_all("a") if a.get_text(strip=True).startswith("#")]

        price = admin = None
        money = _MONEY.findall(flat)
        if money:
            price = parse_int(money[0])
            if len(money) > 1 and any(w in flat.lower() for w in _EXTRA_WORDS):
                admin = parse_int(money[1])
        am = _AREA.search(flat)
        rooms = None
        rm = _ROOMS.search(" ".join(hashtags) + " " + flat)
        if rm:
            rooms = int(rm.group(1))
        elif re.search(r"kawalerk|garsonier", flat, re.I):
            rooms = 1

        low = flat.lower()
        params = {"post_text": flat[:1500], "hashtags": hashtags}
        if "agencj" in low or "biur" in low:
            params["seller"] = "агентство"
        elif "prywatn" in low or "właściciel" in low or "wlasciciel" in low:
            params["seller"] = "частное лицо"
        t = msg.select_one("time")
        if t is not None and t.get("datetime"):
            params["posted"] = t["datetime"]

        district = ""
        for h in hashtags:
            name = h.lstrip("#")
            if _ROOMS.search(name) or re.search(r"kawalerk|garsonier|prywat|agencj|bez|prowiz|pokoj", name, re.I):
                continue
            if name.isdigit():
                continue
            district = name.replace("_", " ")
            break

        posts.append(
            (
                _post_number(data_post),
                Listing(
                    source="olx",
                    ext_id=olx._ext_id(url),
                    url=url,
                    title=lines[0] if lines else "",
                    price=price,
                    admin_rent=admin,
                    area=parse_number(am.group(1)) if am else None,
                    rooms=rooms,
                    location=district,
                    params=params,
                    posted=params.get("posted", ""),
                ),
            )
        )
    posts.sort(key=lambda x: x[0], reverse=True)  # сначала самые новые
    seen: set[str] = set()
    out = []
    for _, l in posts:
        if l.ext_id not in seen:
            seen.add(l.ext_id)
            out.append(l)
    return out


def parse_detail(html: str, l: Listing) -> Listing:
    return olx.parse_detail(html, l)
