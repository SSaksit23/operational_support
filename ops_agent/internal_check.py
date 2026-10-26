"""Internal check: passports x name list x preparation letter, one row per traveller.

For the operations team, not the tour leader. Each row lines up what the name
list says against the passport's machine-readable zone, and tests the passport
against the trip dates from the preparation letter (6-month validity rule).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from pydantic import BaseModel

from .models import GroupBundle, Passenger, PassportScan, SourceRef
from .verify import Settings, _letters, match_scans

RULES = ("passports.", "dates.", "flights.", "hotels.", "people.", "pax.", "passengers.")


class Cell(BaseModel):
    field: str
    name_list: Optional[str] = None
    passport: Optional[str] = None
    status: str  # ok | differ | missing | unread


class Row(BaseModel):
    no: Optional[int]
    name_th: Optional[str]
    name_en: str
    passport_file: Optional[str] = None
    cells: list[Cell]
    expiry_status: str  # ok | short | expired | unknown
    expiry_note: str
    problems: int


class TicketRef(BaseModel):
    name: str
    ticket_no: Optional[str] = None
    passport_no: Optional[str] = None
    source: SourceRef


def tickets(b: GroupBundle) -> list[TicketRef]:
    """Tickets from the e-tickets, plus PNR names that have no e-ticket (one entry per name)."""
    out: dict[str, TicketRef] = {}
    for d in b.etickets:
        for t in d.tickets:
            out.setdefault(_letters(t.passenger_name), TicketRef(name=t.passenger_name, ticket_no=t.ticket_no,
                                                               passport_no=t.passport_no, source=t.source))
    for d in b.pnrs:
        for t in d.passengers:
            ref = out.get(_letters(t.name))
            if ref is None:
                out[_letters(t.name)] = TicketRef(name=t.name, ticket_no=t.ticket_no, source=t.source)
            elif not ref.ticket_no:
                ref.ticket_no = t.ticket_no
    return list(out.values())


def _ticket_cell(p: Passenger, t: Optional[TicketRef], have_tickets: bool) -> Cell:
    if not have_tickets:
        return Cell(field="ticket", name_list=p.airline_name, status="none")
    if t is None:
        return Cell(field="ticket", name_list=p.airline_name, status="missing")
    label = t.name + (f" · {t.ticket_no}" if t.ticket_no else " · ไม่มีเลขตั๋ว")
    same = _letters(t.name) == _letters(p.airline_name)
    pno_ok = not (t.passport_no and p.passport_no) or t.passport_no.upper() == p.passport_no.upper()
    status = "ok" if same and pno_ok and t.ticket_no else "differ"
    if not pno_ok:
        label += f" · พาสปอร์ตในตั๋ว {t.passport_no}"
    return Cell(field="ticket", name_list=p.airline_name, passport=label, status=status)


def _sex(p: Passenger) -> Optional[str]:
    g = (p.gender or p.title or "").upper().rstrip(".")
    if g in ("M", "MALE", "MR", "MSTR", "ชาย", "นาย", "ด.ช."):
        return "M"
    if g in ("F", "FEMALE", "MRS", "MS", "MISS", "หญิง", "นาง", "นางสาว", "ด.ญ."):
        return "F"
    return None


def _cell(field: str, a, b, unread: bool = False) -> Cell:
    a_s = None if a is None else str(a)
    b_s = None if b is None else str(b)
    if b_s is None:
        status = "unread" if unread else "missing"
    elif a_s is None:
        status = "missing"
    else:
        same = _letters(a_s) == _letters(b_s) if field == "name" else a_s.upper() == b_s.upper()
        status = "ok" if same else "differ"
    return Cell(field=field, name_list=a_s, passport=b_s, status=status)


def trip_end(b: GroupBundle) -> Optional[date]:
    return (b.prep_letter and b.prep_letter.end_date) or (b.name_list and b.name_list.end_date)


def build_rows(b: GroupBundle, s: Settings = Settings()) -> list[Row]:
    if not b.name_list:
        return []
    end = trip_end(b)
    limit = end + timedelta(days=round(s.passport_min_months * 30.5)) if end else None
    rows = []
    pairs = match_scans(b.name_list.passengers, b.passports)
    tks = tickets(b)
    tpairs = match_scans(b.name_list.passengers, tks)
    for p in b.name_list.passengers:
        sc = pairs.get(id(p))
        unread = bool(sc and not sc.checks_ok)
        cells = [
            _cell("name", p.airline_name, sc and sc.airline_name),
            _cell("passport_no", p.passport_no and p.passport_no.upper(), sc and sc.passport_no, unread),
            _cell("dob", p.dob, sc and sc.dob, unread),
            _cell("expiry", p.passport_expiry, sc and sc.expiry, unread),
            _cell("sex", _sex(p), sc and sc.sex, unread),
        ]
        if not sc:
            for c in cells:
                c.status = "missing"
        cells.append(_ticket_cell(p, tpairs.get(id(p)), bool(tks)))
        expiry = (sc and sc.expiry) or p.passport_expiry
        if not expiry or not limit:
            st, note = "unknown", "ไม่ทราบวันหมดอายุหรือวันกลับ" if not expiry else "ไม่ทราบวันกลับ"
        elif expiry < end:
            st, note = "expired", f"หมดอายุ {expiry} ก่อนวันกลับ {end}"
        elif expiry < limit:
            st, note = "short", f"หมดอายุ {expiry} เหลือไม่ถึง {s.passport_min_months} เดือนนับจากวันกลับ {end}"
        else:
            st, note = "ok", f"หมดอายุ {expiry}"
        problems = sum(c.status in ("differ", "unread") or c.field == "ticket" and c.status == "missing" for c in cells) + (st in ("short", "expired")) + (sc is None)
        rows.append(Row(no=p.no, name_th=" ".join(x for x in (p.given_name_th, p.surname_th) if x) or None,
                        name_en=p.airline_name, passport_file=sc and f"{sc.source.file} {sc.source.location}",
                        cells=cells, expiry_status=st, expiry_note=note, problems=problems))
    return rows


def extra_scans(b: GroupBundle) -> list[PassportScan]:
    if not b.name_list:
        return list(b.passports)
    used = {id(sc) for sc in match_scans(b.name_list.passengers, b.passports).values()}
    return [s for s in b.passports if id(s) not in used]


def extra_tickets(b: GroupBundle) -> list[TicketRef]:
    tks = tickets(b)
    if not b.name_list:
        return tks
    used = {id(t) for t in match_scans(b.name_list.passengers, tks).values()}
    return [t for t in tks if id(t) not in used]
