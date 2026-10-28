from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Listing:
    source: str                      # "otodom" | "olx"
    ext_id: str
    url: str
    title: str = ""
    price: Optional[int] = None      # аренда в месяц, zł
    admin_rent: Optional[int] = None  # czynsz administracyjny, zł
    deposit: Optional[int] = None    # kaucja, zł
    area: Optional[float] = None     # м²
    rooms: Optional[int] = None
    location: str = ""
    description: str = ""
    params: dict = field(default_factory=dict)
    photo: str = ""
    phones: list = field(default_factory=list)  # нормализованные, вида 48600123456
    posted: str = ""  # дата публикации/обновления, ISO, если известна

    @property
    def key(self) -> str:
        return f"{self.source}:{self.ext_id}"
