from __future__ import annotations

import re
from datetime import datetime, timedelta

_NUM = re.compile(r"\d[\d\s ]*(?:[.,]\d+)?")
_PHONE = re.compile(
    r"(?<!\d)(?:(?:\+|00)?48[\s\-]?)?([4-9]\d{2})[\s\-]?(\d{3})[\s\-]?(\d{3})(?!\d)"
)


def parse_number(value) -> float | None:
    """'3 500,50 zł' -> 3500.5, '45 m²' -> 45.0, 'zapytaj o cenę' -> None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = _NUM.search(str(value))
    if not m:
        return None
    s = m.group(0).replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def parse_int(value) -> int | None:
    n = parse_number(value)
    return int(round(n)) if n is not None else None


def find_phones(text: str, limit: int = 3) -> list[str]:
    """Польские номера из текста, нормализованные к виду 48XXXXXXXXX."""
    found: list[str] = []
    for m in _PHONE.finditer(text or ""):
        digits = "48" + "".join(m.groups())
        if digits not in found:
            found.append(digits)
        if len(found) >= limit:
            break
    return found


_PL_MONTHS = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6, "lipca": 7,
    "sierpnia": 8, "września": 9, "października": 10, "listopada": 11, "grudnia": 12,
}
_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?")
_PL_DATE = re.compile(r"(\d{1,2})\s+([a-ząćęłńóśźż]+)\s+(\d{4})", re.I)
_TIME = re.compile(r"(\d{1,2}):(\d{2})")


def parse_posted(text, now: datetime | None = None) -> datetime | None:
    """'2026-10-27T10:15:00Z', 'Dzisiaj o 10:15', 'Wczoraj o 22:01', 'Odświeżono dnia 27 października 2026' -> datetime (локальное, без tz)."""
    if not text:
        return None
    now = now or datetime.now()
    t = str(text).strip()
    try:
        dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
        return dt.astimezone().replace(tzinfo=None) if dt.tzinfo else dt
    except ValueError:
        pass
    m = _ISO.search(t)
    if m:
        y, mo, d, h, mi = m.groups()
        return datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0))
    low = t.lower()
    tm = _TIME.search(low)
    hh, mm = (int(tm.group(1)), int(tm.group(2))) if tm else (12, 0)
    if "dzisiaj" in low or "dziś" in low or "today" in low:
        return now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if "wczoraj" in low or "yesterday" in low:
        return (now - timedelta(days=1)).replace(hour=hh, minute=mm, second=0, microsecond=0)
    m = _PL_DATE.search(low)
    if m and m.group(2) in _PL_MONTHS:
        return datetime(int(m.group(3)), _PL_MONTHS[m.group(2)], int(m.group(1)), 12, 0)
    return None
