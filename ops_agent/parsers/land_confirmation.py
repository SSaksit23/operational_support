"""Land confirmation reader (Chinese supplier, docx/pdf/xlsx).

No sample has been provided yet, so this module defines the output schema,
the prompt and the JSON-to-model step. The LLM is passed in as a callable
(prompt -> JSON text) so the provider can be chosen later and tests can
use a stub. Values the model returns are kept as raw text on SourceRef so
the verification step can show the reviewer exactly what the model read.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Callable

from ..models import Contact, DocType, HotelNight, LandConfirmationDoc, OptionalTour, SourceRef

PROMPT = """You extract facts from a Chinese ground-handler confirmation (地接确认单) for a Thai tour group.
Return JSON only, with exactly these keys. Use null when a value is not written in the document; never guess.
{{
  "land_operator": {{"name": str, "contact_person": str|null, "phone": str|null, "wechat": str|null, "quote": str}},
  "guides": [{{"name": str, "phone": str|null, "wechat": str|null, "quote": str}}],
  "hotels": [{{"date": "YYYY-MM-DD", "hotel_name": str, "nights": int, "quote": str}}],
  "optional_tours": [{{"name": str, "price": number|null, "currency": "CNY"|"THB"|null, "quote": str}}],
  "pax_total": int|null
}}
"quote" is the exact original text (Chinese as written) the value was taken from.
If the same item appears with two different prices, return both as separate entries.
The tour starts on {start_date}; use it to resolve dates written without a year.

Document text:
<<<
{text}
>>>"""

LLM = Callable[[str], str]


def read_text(path: Path, fmt: str | None = None) -> str:
    suffix = "." + fmt if fmt else path.suffix.lower()
    if suffix == ".docx":
        import docx
        d = docx.Document(str(path))
        parts = [p.text for p in d.paragraphs]
        for t in d.tables:
            for row in t.rows:
                parts.append(" | ".join(dict.fromkeys(c.text for c in row.cells)))
        return "\n".join(parts)
    if suffix == ".pdf":
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(str(path))
        try:
            return "\f".join(doc[i].get_textpage().get_text_range() for i in range(len(doc)))
        finally:
            doc.close()
    if suffix in (".xlsx", ".xlsm"):
        import openpyxl
        with open(path, "rb") as fh:
            wb = openpyxl.load_workbook(fh, data_only=True)
        rows = []
        for ws in wb:
            for r in ws.iter_rows(values_only=True):
                cells = [str(v) for v in r if v not in (None, "")]
                if cells:
                    rows.append(" | ".join(cells))
        return "\n".join(rows)
    return path.read_text(encoding="utf-8", errors="replace")


class UnknownLayout(ValueError):
    pass


def parse_land_confirmation(path: str | Path, llm: LLM | None = None, start_date: date | None = None,
                            display_name: str | None = None, fmt: str | None = None) -> LandConfirmationDoc:
    """Known layouts (团队确认件) are read by rules; anything else needs the LLM."""
    from .team_confirmation import parse_team_confirmation
    path = Path(path)
    fname = display_name or path.name
    text = read_text(path, fmt)
    page_of_line = None
    if (fmt or path.suffix.lower().lstrip(".")) == "pdf":
        page_starts, n = [], 0
        for page_no, chunk in enumerate(text.split("\f"), start=1):
            page_starts.append((n, page_no))
            n += chunk.count("\n") + 1
        page_of_line = lambda i: f"page {max(p for s, p in page_starts if s <= i)}, line {i + 1}"  # noqa: E731
    doc = parse_team_confirmation(text.replace("\f", "\n"), fname, page_of_line)
    if doc is not None:
        return doc
    if llm is None:
        from .general_confirmation import parse_general_confirmation
        if not text.strip():
            raise UnknownLayout("no text in the file (a scanned image?); send it as PDF text, Word or Excel")
        return parse_general_confirmation(text.replace("\f", "\n"), fname, start_date, page_of_line)
    raw = llm(PROMPT.format(text=text, start_date=start_date.isoformat() if start_date else "unknown"))
    doc = from_llm_json(raw, fname)
    doc.layout = "llm"
    return doc


def from_llm_json(raw: str, fname: str) -> LandConfirmationDoc:
    data = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])

    def ref(quote):
        return SourceRef(file=fname, doc_type=DocType.LAND_CONFIRMATION, location="llm extraction", raw=quote)

    lo = data.get("land_operator") or None
    return LandConfirmationDoc(
        land_operator=Contact(role="land_operator", name=lo["name"], nickname=lo.get("contact_person"),
                              phone=lo.get("phone"), wechat=lo.get("wechat"), source=ref(lo.get("quote")))
        if lo and lo.get("name") else None,
        guides=[Contact(role="guide", name=g["name"], phone=g.get("phone"), wechat=g.get("wechat"),
                        source=ref(g.get("quote"))) for g in data.get("guides") or [] if g.get("name")],
        hotels=[HotelNight(date=date.fromisoformat(h["date"]), hotel_name=h["hotel_name"], nights=h.get("nights") or 1,
                           source=ref(h.get("quote"))) for h in data.get("hotels") or [] if h.get("date")],
        optional_tours=[OptionalTour(name=o["name"], price=o.get("price"), currency=o.get("currency"),
                                     source=ref(o.get("quote"))) for o in data.get("optional_tours") or []],
        pax_total=data.get("pax_total"),
        source=SourceRef(file=fname, doc_type=DocType.LAND_CONFIRMATION, location="whole document"),
    )
