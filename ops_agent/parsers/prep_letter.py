"""Preparation letter reader (ใบเตรียมตัวการเดินทาง, PDF).

Page 1 has a stable layout (meeting point, contacts, flights, hotels) and is
read with patterns. Optional tours are found on any page. The day-by-day
itinerary prose is left to the LLM extractor in a later phase.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pypdfium2 as pdfium

from ..models import Contact, DocType, FlightLeg, HotelNight, Meeting, OptionalTour, PrepLetterDoc, SourceRef
from ..normalize import _TH_MONTH_RE, TH_DATE_RE, MONTHS_TH, be_to_ce, clean_thai, hhmm, parse_th_date

FLIGHT_LINE_RE = re.compile(
    r"\b([A-Z0-9]{2}\d{2,4})\s+(\d{1,2}\s*\S+\s*\d{2,4})\s+(\d{1,2}[.:]\d{2})\s*น?\.?\s+(\d{1,2}[.:]\d{2})")
ROUTE_RE = re.compile(r"^([A-Z]{3})\s*[–-]\s*([A-Z]{3})$")
HOTEL_DATE_RE = re.compile(rf"^\s*{TH_DATE_RE.pattern}\s*$")
HOTEL_LINE_RE = re.compile(rf"^\s*(\d{{1,2}})(?:\s*-\s*(\d{{1,2}}))?\s*({_TH_MONTH_RE})\s*(\d{{2,4}})\s*(.*)$")
NIGHTS_RE = re.compile(r"\(พัก\s*(\d+)\s*คืน\)")
OPTIONAL_RE = re.compile(r"OPTIONAL TOUR\s*:\s*(.+?)\s*ท่านละ\s*([\d,]+)\s*(หยวน|บาท|RMB|CNY|THB)")
CURRENCY = {"หยวน": "CNY", "RMB": "CNY", "CNY": "CNY", "บาท": "THB", "THB": "THB"}


PAGE_BREAK = "\f"
W_TYPE = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}type"


def _para_text(p) -> str:
    out = []
    for el in p.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "t":
            out.append(el.text or "")
        elif tag == "tab":
            out.append(" ")
        elif tag in ("br", "cr") and el.get(W_TYPE) != "page":
            out.append("\n")
    return "".join(out)


def _docx_pages(path: str | Path) -> list[str]:
    """A Word preparation letter as page texts laid out like the PDF's text layer."""
    import docx
    body = docx.Document(str(path)).element.body
    lines: list[str] = []
    for el in body.iterchildren():  # paragraphs and tables in document order
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "p":
            if any(b.tag.endswith("}br") and b.get(W_TYPE) == "page" for b in el.iter()):
                lines.append(PAGE_BREAK)
            lines.append(_para_text(el))
        elif tag == "tbl":
            for tr in (e for e in el.iter() if e.tag.endswith("}tr")):
                cells = ["\n".join(_para_text(p) for p in tc.iter() if p.tag.endswith("}p")).strip()
                         for tc in tr if tc.tag.endswith("}tc")]
                cells = [c for c in cells if c]
                if any("\n" in c for c in cells):
                    lines.extend(cells)  # multi-line cells read top to bottom, as in the PDF
                else:
                    lines.append("  ".join(cells))
    pages = "\n".join(lines).split(PAGE_BREAK)
    return [clean_thai(p.strip("\n")) for p in pages if p.strip()] or [""]


def is_docx(path: str | Path) -> bool:
    import zipfile
    try:
        with zipfile.ZipFile(str(path)) as z:
            return "word/document.xml" in z.namelist()
    except (zipfile.BadZipFile, OSError):
        return False


def page_texts(path: str | Path) -> list[str]:
    if is_docx(path):
        return _docx_pages(path)
    doc = pdfium.PdfDocument(str(path))
    try:
        return [clean_thai(doc[i].get_textpage().get_text_range().replace("\r\n", "\n")) for i in range(len(doc))]
    finally:
        doc.close()


def looks_like_prep_letter(first_page: str) -> bool:
    return "ใบเตรียมตัว" in first_page or "นัดหมาย" in first_page and "โรงแรม" in first_page


def parse_prep_letter(path: str | Path, display_name: str | None = None) -> PrepLetterDoc:
    path = Path(path)
    fname = display_name or path.name
    pages = page_texts(path)
    p1 = pages[0]
    lines = [ln.strip() for ln in p1.split("\n")]

    def ref(raw=None, page=1):
        return SourceRef(file=fname, doc_type=DocType.PREP_LETTER, location=f"page {page}", raw=raw)

    out = PrepLetterDoc(source=SourceRef(file=fname, doc_type=DocType.PREP_LETTER, location=f"{len(pages)} pages"))

    # Title: the lines between the heading and the "N วัน N คืน" line.
    try:
        start = next(i for i, ln in enumerate(lines) if "ใบเตรียมตัว" in ln) + 1
        dur_i = next(i for i, ln in enumerate(lines) if re.search(r"\d+\s*วัน\s*\d+\s*คืน", ln))
        out.program_title = " ".join(lines[start:dur_i]).strip() or None
        dur_line = lines[dur_i]
        out.duration = re.search(r"\d+\s*วัน\s*\d+\s*คืน", dur_line).group()
        air = re.search(r"สายการบิน\s*(.+)$", dur_line)
        out.airline = air.group(1).strip() if air else None
    except StopIteration:
        pass

    m = re.search(rf"เดินทาง\s*(\d{{1,2}})\s*-\s*(\d{{1,2}})\s*(\S+)\s*(\d{{2,4}})", p1)
    if m and m.group(3) in MONTHS_TH:
        y, mo = be_to_ce(int(m.group(4))), MONTHS_TH[m.group(3)]
        out.start_date, out.end_date = date(y, mo, int(m.group(1))), date(y, mo, int(m.group(2)))

    # Meeting point
    mt = Meeting(source=ref())
    mm = re.search(r"นัดหมาย[^\n]*", p1)
    if mm:
        mt.date = parse_th_date(mm.group())
        mt.source = ref(mm.group())
    tm = re.search(r"เวลา\s*(\d{1,2}[.:]\d{2})\s*น", p1)
    mt.time = hhmm(tm.group(1)) if tm else None
    fl = re.search(r"ช[ั้]{2}น\s*(\d+)", p1)
    mt.floor = fl.group(1) if fl else None
    gt = re.search(r"ประตู\s*(\d+)", p1)
    mt.gate = gt.group(1) if gt else None
    ct = re.search(r"เคาน์เตอร์\s*(?:สายการบิน)?\s*([^\n]+)", p1)
    mt.counter = ct.group(1).strip() if ct else None
    sg = re.search(r"ป้ายต้อนรับ\s+(.+?)\s+ณ", p1)
    mt.sign = sg.group(1).strip() if sg else None
    ap = re.search(r"สนามบิน\S+", p1)
    mt.airport = ap.group().replace("สนามบิน", "") if ap else None
    out.meeting = mt

    # Contacts
    tl = re.search(r"หัวหน้าทัวร์\s+คุณ(\S+)\s*(?:\(คุณ(\S+?)\))?\s+([\d-]{9,})", p1)
    if tl:
        out.contacts.append(Contact(role="tour_leader", name=tl.group(1), nickname=tl.group(2),
                                    phone=tl.group(3), source=ref(tl.group())))
    em = re.search(r"ฉุกเฉิน\s+คุณ(\S+)\s+([\d-]{9,})", p1)
    if em:
        out.contacts.append(Contact(role="emergency", name=em.group(1), phone=em.group(2), source=ref(em.group())))

    # Flights: route line, carrier line, then the flight line.
    route, carrier = None, None
    for ln in lines:
        r = ROUTE_RE.match(ln)
        if r:
            route = (r.group(1), r.group(2))
            carrier = None
            continue
        f = FLIGHT_LINE_RE.search(ln)
        if f:
            out.flights.append(FlightLeg(
                flight_no=f.group(1), date=parse_th_date(f.group(2)),
                origin=route[0] if route else None, destination=route[1] if route else None,
                dep_time=hhmm(f.group(3)), arr_time=hhmm(f.group(4)), carrier_name=carrier, source=ref(ln)))
            route = None
            continue
        if route and re.fullmatch(r"[A-Za-z][A-Za-z ]+", ln):
            carrier = ln

    bag = re.search(r"ขึ้นเค\S*\s*ได้\s*(\d+)\s*กิโลกรัม", p1)
    out.carry_on_kg = int(bag.group(1)) if bag else None
    chk = re.search(r"ใต้ท้องเครื่อง\s*ท่านละ\s*(\d+)\s*กิโลกรัม", p1)
    out.checked_kg = int(chk.group(1)) if chk else None

    # Hotels: date line, hotel name, "(พัก N คืน)", address lines.
    try:
        h0 = next(i for i, ln in enumerate(lines) if "ชื่อโรงแรม" in ln) + 1
    except StopIteration:
        h0 = len(lines)
    i = h0
    while i < len(lines):
        if lines[i].startswith("ป้ายต้อนรับ"):
            break  # end of the hotel table (a Word letter has no page break before the itinerary)
        hl = HOTEL_LINE_RE.match(lines[i])
        if hl:
            # "26 ต.ค. 69" alone, or "26-29 ต.ค. 69 REZEN JUNDUN HOTEL ..." (a range and the name on one line)
            night = parse_th_date(f"{hl.group(1)} {hl.group(3)} {hl.group(4)}")
            name = hl.group(5).strip() or None
            nights = int(hl.group(2)) - int(hl.group(1)) + 1 if hl.group(2) and int(hl.group(2)) > int(hl.group(1)) else 1
            addr = []
            j = i + 1
            while j < len(lines) and not HOTEL_LINE_RE.match(lines[j]) and not lines[j].startswith("ป้ายต้อนรับ"):
                n = NIGHTS_RE.search(lines[j])
                if n:
                    nights = int(n.group(1))
                elif name is None and lines[j]:
                    name = lines[j]
                elif lines[j]:
                    addr.append(lines[j].rstrip(", "))
                j += 1
            if name:
                out.hotels.append(HotelNight(date=night, hotel_name=name, nights=nights,
                                             address=", ".join(addr) or None, source=ref(lines[i] + " " + name)))
            i = j
        else:
            i += 1

    # Optional tours anywhere in the letter.
    for pno, text in enumerate(pages, start=1):
        flat = " ".join(text.split())
        for o in OPTIONAL_RE.finditer(flat):
            out.optional_tours.append(OptionalTour(
                name=o.group(1).strip(), price=float(o.group(2).replace(",", "")),
                currency=CURRENCY.get(o.group(3)), source=ref(o.group(), pno)))
    return out
