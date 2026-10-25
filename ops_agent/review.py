"""Evidence review of a drafted TL message.

The officer may edit the draft before sending, so the edited text is
checked again here, independently of how it was produced: every flight,
time, date, phone number, head count, amount, hotel and tax ID found in
the text must trace back to an uploaded document or the company table.

The result uses the harness note's format: claim, verdict, source,
correction. An LLM reviewer can be added on top later (REVIEW_PROMPT); it
gets read-only access and the same output format, and never edits.
"""

from __future__ import annotations

import re
from typing import Optional

from pydantic import BaseModel

from .companies import Company
from .message import TH_MONTH_SHORT
from .models import GroupBundle
from .normalize import hhmm

REVIEW_PROMPT = """You review a Thai tour-leader assignment message against the source documents.
For every factual claim (flight, time, date, hotel, name, count, amount, phone, tax ID) return one row:
claim | verdict (verified / incorrect / unresolved) | source (file + page/cell/line) | required correction.
Report only. Do not rewrite the message."""


class Claim(BaseModel):
    claim: str
    verdict: str  # verified | incorrect | unresolved
    source: str = ""
    correction: str = ""


def review_message(text: str, bundle: GroupBundle, company: Optional[Company] = None,
                   gift_budget_per_pax: Optional[float] = None) -> list[Claim]:
    nl, pl = bundle.name_list, bundle.prep_letter
    claims: list[Claim] = []

    legs = []  # (flight, dep, arr, source label)
    for doc in bundle.etickets:
        for t in doc.tickets:
            legs += [(l.flight_no, l.dep_time, l.arr_time, f"{l.source.file} {l.source.location}") for l in t.legs]
    for doc in bundle.pnrs:
        legs += [(l.flight_no, l.dep_time, l.arr_time, f"{l.source.file} {l.source.location}") for l in doc.segments]
    for d in ([pl] if pl else []) + ([nl] if nl else []):
        legs += [(l.flight_no, l.dep_time, l.arr_time, f"{l.source.file} {l.source.location}") for l in d.flights]

    # Flights with times: "PN6430 BKK-CKG 19.35-23.40" or "PN6429 14.40-16.25"
    for m in re.finditer(r"\b([A-Z0-9]{2}\d{2,4})\b(?:\s+[A-Z]{3}-[A-Z]{3})?\s+(\d{1,2}[.:]\d{2})-(\d{1,2}[.:]\d{2})", text):
        fno, dep, arr = m.group(1), hhmm(m.group(2)), hhmm(m.group(3))
        hits = [src for f, d, a, src in legs if (f, d, a) == (fno, dep, arr)]
        line = text[text.rfind("\n", 0, m.start()) + 1:m.start()]
        if hits:
            claims.append(Claim(claim=m.group(0), verdict="verified", source=_sources(hits),
                                correction="quoted as an outdated value in a warning note" if "**" in line else ""))
        else:
            known = sorted({f"{d}-{a}" for f, d, a, _ in legs if f == fno})
            claims.append(Claim(claim=m.group(0), verdict="incorrect",
                                correction=f"{fno} times in documents: {', '.join(known) or 'flight not found'}"))

    # Phone numbers
    phones = {}
    for c in (pl.contacts if pl else []):
        phones[_digits(c.phone)] = f"{c.source.file} {c.source.location}"
    if bundle.land_confirmation:
        for c in bundle.land_confirmation.guides + ([bundle.land_confirmation.land_operator]
                                                    if bundle.land_confirmation.land_operator else []):
            if c.phone:
                phones[_digits(c.phone)] = f"{c.source.file} {c.source.location}"
    for p in (nl.passengers if nl else []):
        if p.tel:
            phones[_digits(p.tel)] = f"{p.source.file} {p.source.location}"
    for m in re.finditer(r"(\+\d{2}\s?)?\d{2,3}-\d{3,4}-\d{4}", text):
        src = phones.get(_digits(m.group(0)))
        claims.append(Claim(claim=m.group(0), verdict="verified" if src else "unresolved", source=src or "",
                            correction="" if src else "phone not found in any uploaded document"))

    # Head count, rooms, trip dates
    if nl:
        n = len(nl.passengers)
        rooms = len({p.room for p in nl.passengers})
        src = f"{nl.source.file} passenger rows"
        for m in re.finditer(r"=\s*(\d+)\s*ท่าน", text):
            ok = int(m.group(1)) == n
            claims.append(Claim(claim=m.group(0), verdict="verified" if ok else "incorrect", source=src,
                                correction="" if ok else f"name list has {n} people"))
        # Only the group's room total ("= 13 ท่าน / 6 ห้อง"), not counts quoted from customer remarks.
        for m in re.finditer(r"(?<=/)\s*(\d+)\s*ห้อง", text):
            ok = int(m.group(1)) == rooms
            claims.append(Claim(claim=m.group(0), verdict="verified" if ok else "incorrect", source=src,
                                correction="" if ok else f"name list uses {rooms} rooms"))
        month = TH_MONTH_SHORT[nl.start_date.month - 1]
        for m in re.finditer(rf"เดินทาง\s+(\d{{1,2}})-(\d{{1,2}})\s+{re.escape(month)}", text):
            ok = (int(m.group(1)), int(m.group(2))) == (nl.start_date.day, nl.end_date.day)
            claims.append(Claim(claim=m.group(0), verdict="verified" if ok else "incorrect",
                                source=f"{nl.source.file} header",
                                correction="" if ok else f"trip is {nl.start_date} to {nl.end_date}"))

    # Gift budget arithmetic
    for m in re.finditer(r"(\d+)\s*ท่าน\s*=\s*([\d,]+)\s*บาท", text):
        if gift_budget_per_pax is None:
            continue
        expect = gift_budget_per_pax * int(m.group(1))
        ok = float(m.group(2).replace(",", "")) == expect
        claims.append(Claim(claim=m.group(0), verdict="verified" if ok else "incorrect", source="company rule",
                            correction="" if ok else f"{gift_budget_per_pax:g} x {m.group(1)} = {expect:,.0f}"))

    # Hotels named in the text
    for h in (pl.hotels if pl else []):
        if h.hotel_name in text:
            land = bundle.land_confirmation
            confirmed = land and any(x.date == h.date for x in land.hotels)
            claims.append(Claim(claim=h.hotel_name, verdict="verified" if confirmed else "unresolved",
                                source=f"{h.source.file} {h.source.location}",
                                correction="" if confirmed else "not yet confirmed by the land operator"))

    # Company block and tax ID
    if company:
        missing = [ln for ln in company.lines if ln not in text]
        claims.append(Claim(claim=f"company block {company.label}", verdict="incorrect" if missing else "verified",
                            source="company table", correction=("lines differ: " + " / ".join(missing)) if missing else ""))
        for m in re.finditer(r"\b\d[\d-]{11,15}\d\b", text):
            d = _digits(m.group(0))
            if len(d) == 13:
                ok = d == company.tax_id
                claims.append(Claim(claim=m.group(0), verdict="verified" if ok else "incorrect", source="company table",
                                    correction="" if ok else f"{company.label} tax ID is {company.tax_id}"))
    return claims


def _sources(refs: list[str]) -> str:
    """'a.pdf page 1', 'a.pdf page 2', ... -> 'a.pdf pages 1-2'."""
    by_file: dict[str, list[str]] = {}
    for r in sorted(set(refs)):
        f, _, loc = r.rpartition(" page ") if " page " in r else (r, "", "")
        by_file.setdefault(f, []).append(loc)
    out = []
    for f, locs in by_file.items():
        nums = sorted(int(x) for x in locs if x.isdigit())
        if len(nums) > 1 and nums == list(range(nums[0], nums[-1] + 1)):
            out.append(f"{f} pages {nums[0]}-{nums[-1]}")
        elif nums:
            out.append(f"{f} page " + ", ".join(map(str, nums)))
        else:
            out.append(f)
    return "; ".join(out)


def _digits(s: Optional[str]) -> str:
    d = re.sub(r"\D", "", s or "")
    return "0" + d[2:] if d.startswith("66") and len(d) == 11 else d


def claims_markdown(claims: list[Claim]) -> str:
    rows = ["| Claim | Verdict | Source | Correction |", "| --- | --- | --- | --- |"]
    rows += [f"| {c.claim} | {c.verdict} | {c.source} | {c.correction} |" for c in claims]
    return "\n".join(rows)
