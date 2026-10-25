"""Turn a checked group into searchable text chunks, with personal IDs removed.

Chunks are what gets embedded. They describe people by name, age, room and
remarks, never by citizen ID, passport number, date of birth or phone.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, NamedTuple

from ..detect import detect
from ..models import DocType, GroupBundle
from ..verify import Finding

PASSPORT_RE = re.compile(r"\b(?:PP)?[A-Z]{1,2}\d{7}\b")
CITIZEN_RE = re.compile(r"(?<!\d)\d{13}(?!\d)|\b\d-\d{4}-\d{5}-\d{2}-\d\b")
PHONE_RE = re.compile(r"(?<![\d-])0\d{1,2}-?\d{3}-?\d{4}(?![\d-])")


class Chunk(NamedTuple):
    kind: str
    location: str
    content: str
    source_file: str | None = None  # display name of the document, when the chunk comes from one


def scrub(text: str, keep: Iterable[str] = ()) -> str:
    """Remove passport numbers, 13-digit IDs and phone numbers, except values listed in keep
    (company tax IDs, staff and supplier phones that belong in the record)."""
    keep = {k for k in keep if k}

    def sub(pattern, label):
        nonlocal text
        text = pattern.sub(lambda m: m.group(0) if m.group(0) in keep else label, text)

    sub(PASSPORT_RE, "[passport]")
    sub(CITIZEN_RE, "[id]")
    sub(PHONE_RE, "[phone]")
    return text


def group_chunks(bundle: GroupBundle, findings: list[Finding], keep: Iterable[str] = ()) -> list[Chunk]:
    nl, pl, lc = bundle.name_list, bundle.prep_letter, bundle.land_confirmation
    keep = set(keep)
    if pl:
        keep |= {c.phone for c in pl.contacts if c.phone}
    if lc:
        keep |= {c.phone for c in lc.guides if c.phone} | ({lc.land_operator.phone} if lc.land_operator else set())
    out: list[Chunk] = []
    code = nl.tour_code if nl else "?"

    if nl:
        hotels = "; ".join(f"{h.date} {h.hotel_name}" for h in (pl.hotels if pl else []))
        flights = "; ".join(f"{f.flight_no} {f.date} {f.origin}-{f.destination} {f.dep_time}-{f.arr_time}"
                            for f in (pl.flights if pl else nl.flights))
        out.append(Chunk("group", "summary", scrub(
            f"กรุ๊ป {code} {nl.program_name or ''} ({nl.program_code or ''}) เดินทาง {nl.start_date} ถึง {nl.end_date}. "
            f"{len(nl.passengers)} ท่าน, {nl.rooms_total} ห้อง {nl.room_mix}. หัวหน้าทัวร์ {nl.tour_leader}. "
            f"ไฟลต์ {flights}. โรงแรม {hotels}.", keep), nl.source.file))
        for p in nl.passengers:
            bits = [f"กรุ๊ป {code}", f"{p.title or ''} {p.given_name_th or ''} {p.surname_th or ''}".strip(),
                    f"({p.given_name} {p.surname})", f"อายุ {p.age}" if p.age is not None else "",
                    f"ห้อง {p.room}" if p.room else "", "หัวหน้าทัวร์" if p.is_tour_leader else "",
                    f"agency {p.agency}" if p.agency else "", f"booking {p.booking_no}" if p.booking_no else "",
                    f"หมายเหตุ {' '.join(p.remark.split())}" if p.remark else ""]
            out.append(Chunk("person", p.source.location, scrub(" ".join(b for b in bits if b), keep), p.source.file))
    if pl:
        for h in pl.hotels:
            out.append(Chunk("hotel", h.source.location, f"กรุ๊ป {code} คืนวันที่ {h.date} พักที่ {h.hotel_name}, {h.address or ''}",
                             h.source.file))
        for o in pl.optional_tours:
            out.append(Chunk("optional_tour", o.source.location,
                             f"กรุ๊ป {code} OPTIONAL TOUR {o.name} ราคา {o.price:g} {o.currency}", o.source.file))
        if pl.meeting:
            m = pl.meeting
            out.append(Chunk("meeting", m.source.location, scrub(
                f"กรุ๊ป {code} นัดหมาย {m.date} เวลา {m.time} สนามบิน{m.airport or ''} ชั้น {m.floor} ประตู {m.gate} "
                f"เคาน์เตอร์ {m.counter} ป้าย {m.sign}", keep), m.source.file))
        for c in pl.contacts:
            out.append(Chunk("contact", c.source.location, f"กรุ๊ป {code} {c.role} คุณ{c.name} {c.nickname or ''} {c.phone or ''}",
                             c.source.file))
    if lc:
        for g in lc.guides:
            out.append(Chunk("contact", g.source.location, f"กรุ๊ป {code} ไกด์ท้องถิ่น {g.name} {g.phone or ''} {g.wechat or ''}",
                             g.source.file))
    for f in findings:
        out.append(Chunk("finding", f.rule, scrub(f"กรุ๊ป {code} [{f.verdict.value}] {f.title}. {f.detail}", keep)))
    return out


def document_page_chunks(path: str | Path, display_name: str, keep: Iterable[str] = ()) -> list[Chunk]:
    """Raw text of a document, one chunk per page (PDF) or per block of lines (docx), scrubbed."""
    dtype, fmt = detect(path)
    out: list[Chunk] = []
    if fmt == "pdf":
        import pypdfium2 as pdfium
        from ..normalize import clean_thai
        doc = pdfium.PdfDocument(str(path))
        try:
            for i in range(len(doc)):
                text = clean_thai(doc[i].get_textpage().get_text_range())
                text = " ".join(text.split())
                if text:
                    out.append(Chunk("page", f"page {i + 1}", scrub(text, keep)[:4000], display_name))
        finally:
            doc.close()
    elif fmt == "docx" and dtype in (DocType.PNR, DocType.LAND_CONFIRMATION):
        from ..parsers.pnr import read_lines
        lines, _ = read_lines(Path(path), fmt)
        text = "\n".join(ln for ln in lines if ln.strip())
        if text:
            out.append(Chunk("page", "document", scrub(text, keep)[:4000], display_name))
    return out
