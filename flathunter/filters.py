from __future__ import annotations

from .models import Listing


def total_price(l: Listing) -> int | None:
    if l.price is None:
        return None
    return l.price + (l.admin_rent or 0)


def prefilter(l: Listing, s: dict) -> tuple[bool, str]:
    """Жёсткие критерии. Неизвестные поля не отсекают объявление."""
    tp = total_price(l)
    if tp is not None:
        if s.get("max_price") and tp > s["max_price"]:
            return False, f"цена {tp} > {s['max_price']}"
        if s.get("min_price") and tp < s["min_price"]:
            return False, f"цена {tp} < {s['min_price']}"
    if l.area is not None:
        if s.get("min_area") and l.area < s["min_area"]:
            return False, f"площадь {l.area} < {s['min_area']}"
        if s.get("max_area") and l.area > s["max_area"]:
            return False, f"площадь {l.area} > {s['max_area']}"
    rooms = s.get("rooms")
    if rooms and l.rooms is not None and l.rooms not in rooms:
        return False, f"комнат {l.rooms} не в {rooms}"
    districts = [str(d).lower() for d in (s.get("districts") or [])]
    if districts and l.location and not any(d in l.location.lower() for d in districts):
        return False, f"район «{l.location}» не в списке"
    text = f"{l.title}\n{l.description}".lower()
    for kw in s.get("exclude_keywords") or []:
        if str(kw).lower() in text:
            return False, f"стоп-слово «{kw}»"
    return True, ""
