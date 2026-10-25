"""Billing companies: the master table behind the company selector."""

from __future__ import annotations

import json
import re
from pathlib import Path

from typing import Optional

from pydantic import BaseModel, field_validator

DATA = Path(__file__).parent / "data" / "companies.json"


def valid_thai_tax_id(tax_id: str) -> bool:
    """Thai 13-digit tax/citizen ID checksum."""
    d = re.sub(r"\D", "", tax_id)
    if len(d) != 13:
        return False
    total = sum(int(d[i]) * (13 - i) for i in range(12))
    return (11 - total % 11) % 10 == int(d[12])


class Company(BaseModel):
    id: str
    company: str
    lang: str  # "th" | "en"
    tax_id: str
    branch: str
    lines: list[str]
    logo_sha256: Optional[str] = None  # the logo image as embedded in the expense template

    @field_validator("tax_id")
    @classmethod
    def _checksum(cls, v: str) -> str:
        if not valid_thai_tax_id(v):
            raise ValueError(f"tax ID {v} fails the Thai checksum")
        return v

    @property
    def label(self) -> str:
        return f"{self.company} ({self.lang.upper()})"

    def block(self) -> str:
        return "\n".join(self.lines)


def load_companies(path: Path = DATA) -> dict[str, Company]:
    data = json.loads(path.read_text(encoding="utf-8"))
    companies = [Company(**c) for c in data["companies"]]
    for c in companies:
        digits_in_text = re.sub(r"\D", "", " ".join(c.lines))
        if c.tax_id not in digits_in_text:
            raise ValueError(f"{c.id}: tax ID {c.tax_id} does not appear in its address lines")
    return {c.id: c for c in companies}
