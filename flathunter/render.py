from __future__ import annotations

from html import escape
from urllib.parse import quote


def _zl(v) -> str:
    return f"{int(v):,}".replace(",", " ") + " zł"


def _cut(s, n: int) -> str:
    s = str(s).strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def facts_line(l: dict) -> str:
    parts = []
    if l.get("price") is not None:
        s = _zl(l["price"])
        if l.get("admin_rent"):
            s += f" + {_zl(l['admin_rent'])} доп."
        parts.append("💰 " + s)
    else:
        parts.append("💰 цена не указана")
    if l.get("area"):
        parts.append(f"📐 {l['area']:g} м²")
    if l.get("rooms"):
        parts.append(f"🚪 {l['rooms']} комн.")
    if l.get("deposit"):
        parts.append(f"🔒 kaucja {_zl(l['deposit'])}")
    return " · ".join(parts)


def _section(icon: str, title: str, items: list[str], limit: int) -> str:
    if not items:
        return ""
    body = "\n".join(f"{icon} {escape(_cut(i, 170))}" for i in items[:limit])
    return f"\n<b>{title}</b>\n{body}\n"


def _render(l: dict, a: dict, draft: str, note: str, n_pros: int, n_cons: int, n_flags: int, n_q: int) -> str:
    score = a.get("fit_score", 0)
    icon = "🟢" if score >= 8 else "🟡" if score >= 6 else "🟠"
    url = escape(l.get("url", ""), quote=True)
    title = escape(_cut(l.get("title") or "Объявление", 120))
    head = f'{icon} <b>{score}/10</b> · <a href="{url}">{title}</a>'
    lines = [head, escape(facts_line(l))]
    extra = []
    if l.get("location"):
        extra.append("📍 " + escape(_cut(l["location"], 80)))
    seller = (l.get("params") or {}).get("seller")
    if seller:
        extra.append("👤 " + escape(str(seller)))
    if extra:
        lines.append(" · ".join(extra))
    text = "\n".join(lines) + "\n"
    page_note = (l.get("params") or {}).get("_note")
    if page_note:
        text += f"\nℹ️ {escape(str(page_note))}\n"
    if a.get("verdict"):
        text += f"\n{escape(_cut(a['verdict'], 320))}\n"
    text += _section("🚩", "Красные флаги", a.get("red_flags", []), n_flags)
    text += _section("✅", "Плюсы", a.get("pros", []), n_pros)
    text += _section("⚠️", "Минусы", a.get("cons", []), n_cons)
    text += _section("❓", "Уточнить у владельца", a.get("questions", []), n_q)
    text += f"\n✉️ <b>Черновик (PL)</b>\n<pre>{escape(_cut(draft, 1200))}</pre>"
    if note:
        text += f"\n\n{escape(note)}"
    return text


def render_card(l: dict, a: dict, draft: str, note: str = "") -> str:
    text = ""
    for limits in ((5, 5, 4, 6), (3, 3, 3, 4), (2, 2, 2, 3)):
        text = _render(l, a, draft, note, *limits)
        if len(text) <= 3900:
            return text
    return text


def wa_link(phone: str, text: str) -> str:
    """Ссылка wa.me с готовым текстом; текст укорачивается, чтобы ссылка осталась короткой."""
    t = text
    while len(quote(t)) > 1500 and len(t) > 50:
        t = t[: int(len(t) * 0.9)]
    return f"https://wa.me/{phone}?text={quote(t)}"
