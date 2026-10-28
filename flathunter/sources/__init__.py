from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from . import olx, otodom, tgchannel


@dataclass
class Source:
    name: str
    search_url: str
    parse_search: Callable
    parse_detail: Callable


def build_sources(cfg: dict) -> list[Source]:
    srcs = cfg.get("sources") or {}
    out: list[Source] = []
    ch = srcs.get("channel") or {}
    if ch.get("enabled") and ch.get("name"):
        name = str(ch["name"]).lstrip("@")
        out.append(Source("channel", f"https://t.me/s/{name}", tgchannel.parse_search, tgchannel.parse_detail))
    for name, mod in (("otodom", otodom), ("olx", olx)):
        s = srcs.get(name) or {}
        if s.get("enabled") and s.get("search_url"):
            out.append(Source(name, s["search_url"], mod.parse_search, mod.parse_detail))
    return out
