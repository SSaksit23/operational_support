"""Airline e-ticket itinerary reader (PDF, one passenger per page).

Chinese-issued itineraries (电子客票行程单) embed CJK fonts that poppler's
pdftotext cannot map, so it returns blanks. pdfium reads them correctly.
"""

from __future__ import annotations

import re
from pathlib import Path

import pypdfium2 as pdfium

from ..models import DocType, ETicketDoc, FlightLeg, SourceRef, Ticket
from ..normalize import hhmm, parse_ddmon, parse_ddmonyy

NAME_RE = re.compile(r"旅客姓名\s*[:：]\s*([A-Z][A-Z ]*/[A-Z][A-Z ]*?)\s+票号")
TICKET_RE = re.compile(r"票号\s*[:：]\s*(\d{10,14})")
PASSPORT_RE = re.compile(r"身份识别代码\s*[:：]\s*(?:PP|NI)?([A-Z0-9]+)")
RECORD_RE = re.compile(r"航空公司记录编号\s*[:：]\s*([A-Z0-9]{5,6})")
AIRLINE_RE = re.compile(r"出票航空公司\s*[:：]\s*([A-Z ]+?)\s+出票时间")
ISSUED_RE = re.compile(r"出票时间\s*[:：]\s*(\d{1,2}[A-Za-z]{3}\d{2})")
# "CKG HU423 V 28OCT 0805 1045 28OCT6/28OCT6 OK 1PC T3 -- BKK"
LEG_RE = re.compile(
    r"\b([A-Z]{3})\s+([A-Z0-9]{2}\d{1,4})\s+([A-Z])\s+(\d{1,2}[A-Z]{3})\s+(\d{4})\s+(\d{4})\s+\S+\s+"
    r"(OK|HK|RQ|SA|NS)\s+\S+\s+\S+\s+\S+\s+([A-Z]{3})\b")


def page_texts(path: str | Path) -> list[str]:
    doc = pdfium.PdfDocument(str(path))
    try:
        return [doc[i].get_textpage().get_text_range().replace("\r\n", "\n") for i in range(len(doc))]
    finally:
        doc.close()


def looks_like_eticket(text: str) -> bool:
    return "电子客票行程单" in text or ("票号" in text and "旅客姓名" in text)


def parse_eticket(path: str | Path, display_name: str | None = None) -> ETicketDoc:
    path = Path(path)
    fname = display_name or path.name
    tickets: list[Ticket] = []
    for i, raw in enumerate(page_texts(path), start=1):
        if not looks_like_eticket(raw):
            continue
        flat = " ".join(raw.split())
        name_m = NAME_RE.search(flat)
        tk_m = TICKET_RE.search(flat)
        if not name_m or not tk_m:
            continue
        issued = parse_ddmonyy(ISSUED_RE.search(flat).group(1)) if ISSUED_RE.search(flat) else None
        src = SourceRef(file=fname, doc_type=DocType.ETICKET, location=f"page {i}")
        legs = []
        for m in LEG_RE.finditer(flat):
            ref_date = issued
            legs.append(FlightLeg(
                flight_no=m.group(2),
                date=parse_ddmon(m.group(4), ref_date) if ref_date else None,
                origin=m.group(1), destination=m.group(8),
                dep_time=hhmm(m.group(5)), arr_time=hhmm(m.group(6)), status=m.group(7),
                source=SourceRef(file=fname, doc_type=DocType.ETICKET, location=f"page {i}", raw=m.group(0)),
            ))
        tickets.append(Ticket(
            passenger_name=" ".join(name_m.group(1).split()),
            ticket_no=tk_m.group(1),
            passport_no=PASSPORT_RE.search(flat).group(1) if PASSPORT_RE.search(flat) else None,
            airline_record=RECORD_RE.search(flat).group(1) if RECORD_RE.search(flat) else None,
            issuing_airline=AIRLINE_RE.search(flat).group(1).strip() if AIRLINE_RE.search(flat) else None,
            issue_date=issued,
            legs=legs,
            source=src,
        ))
    return ETicketDoc(tickets=tickets, source=SourceRef(file=fname, doc_type=DocType.ETICKET, location="all pages"))
