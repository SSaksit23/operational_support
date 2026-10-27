"""Cross-document verification.

Each rule compares the same fact across documents and returns Findings with
one of four verdicts. The rules are plain code: the LLM never decides
whether two documents agree.

  verified    every source that states the fact agrees
  incorrect   sources disagree, or a document holds a wrong value; blocks drafting
  unresolved  the document that would settle it is missing, or a person must decide
  info        worth telling the TL (special care, remarks), not an error
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import date, timedelta
from difflib import SequenceMatcher
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from .models import FlightLeg, GroupBundle, Passenger, SourceRef


class Verdict(str, Enum):
    VERIFIED = "verified"
    INCORRECT = "incorrect"
    UNRESOLVED = "unresolved"
    INFO = "info"


class Compare(BaseModel):
    """One source's value, for the side-by-side view."""
    source: str  # "PNR", "name list", "land confirmation", ...
    value: str
    location: str = ""  # file + page/cell
    used: bool = False  # the value the outputs use unless the officer picks another


class Finding(BaseModel):
    rule: str  # "flights.return", "passengers.tickets", ...
    verdict: Verdict
    title: str  # one line, shown in the review list
    detail: str = ""
    evidence: list[SourceRef] = Field(default_factory=list)
    accepted_value: Optional[str] = None  # the value the message will use, when one wins
    compare: list[Compare] = Field(default_factory=list)


class Settings(BaseModel):
    """Company rules that are not in any document. Defaults follow the plan's open questions."""

    passport_min_months: int = 6
    elderly_age: int = 70
    minor_age: int = 20  # Thai legal age; immigration asks for parental consent below it
    gift_budget_per_pax: Optional[float] = 35  # baht, from the example TL message; open question


ROOM_CAPACITY = {"SGL": 1, "TWN": 2, "DBL": 2, "TRP": 3, "QUAD": 4}


def verify(bundle: GroupBundle, settings: Settings | None = None) -> list[Finding]:
    s = settings or Settings()
    findings: list[Finding] = []
    for rule in (check_flights, check_dates, check_tickets, check_pnr_names, check_pax_rooms, check_hotels,
                 check_land_contacts, check_land_group, check_optional_tours, check_passport_scans, check_passports, check_special_cases,
                 check_name_spelling, check_expense_template):
        findings.extend(rule(bundle, s))
    return findings


def blocking(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.verdict == Verdict.INCORRECT]


# ---------------------------------------------------------------- flights

def _flight_sources(b: GroupBundle) -> list[tuple[str, FlightLeg, int]]:
    """(label, leg, priority) for every flight leg stated anywhere. Lower priority number wins."""
    out = []
    for doc in b.etickets:
        latest = max((t.issue_date for t in doc.tickets if t.issue_date), default=None)
        seen = set()
        for t in doc.tickets:
            for leg in t.legs:
                key = (leg.flight_no, leg.date, leg.dep_time, leg.arr_time)
                if key not in seen:
                    seen.add(key)
                    # The newest ticket issue beats everything else.
                    out.append((f"e-ticket (issued {t.issue_date})", leg, 0 if t.issue_date == latest else 1))
    for doc in b.pnrs:
        out.extend(("PNR", leg, 2) for leg in doc.segments)
    if b.prep_letter:
        out.extend(("preparation letter", leg, 3) for leg in b.prep_letter.flights)
    if b.name_list:
        out.extend(("name list", leg, 4) for leg in b.name_list.flights)
    if b.land_confirmation:
        out.extend(("land confirmation", leg, 5) for leg in b.land_confirmation.flights)
    return out


def _where(ref) -> str:
    return f"{ref.file} {ref.location}".strip() if ref else ""


def _direction(leg: FlightLeg, home: str) -> Optional[str]:
    if leg.origin == home:
        return "outbound"
    if leg.destination == home:
        return "return"
    return None


def _fmt(leg: FlightLeg) -> str:
    day = f"{leg.date:%d %b}" if leg.date else "?? ???"
    return f"{leg.flight_no} {day} {leg.origin or '?'}-{leg.destination or '?'} {leg.dep_time}-{leg.arr_time}"


def check_flights(b: GroupBundle, s: Settings, home: str = "BKK") -> list[Finding]:
    by_dir: dict[str, list] = defaultdict(list)
    for label, leg, prio in _flight_sources(b):
        d = _direction(leg, home)
        if d:
            by_dir[d].append((label, leg, prio))
    findings = []
    for direction in ("outbound", "return"):
        items = by_dir.get(direction, [])
        if not items:
            findings.append(Finding(rule=f"flights.{direction}", verdict=Verdict.UNRESOLVED,
                                    title=f"No {direction} flight found in any document"))
            continue
        items.sort(key=lambda x: x[2])
        winner = items[0][1]
        key = lambda l: (l.flight_no, l.date, l.dep_time, l.arr_time)  # noqa: E731
        route = lambda l: (l.origin, l.destination)  # noqa: E731
        disagree = [(lab, leg) for lab, leg, _ in items
                    if key(leg) != key(winner) or (all(route(leg)) and all(route(winner)) and route(leg) != route(winner))]
        evidence = [leg.source for _, leg, _ in items]
        compare = [Compare(source=lab, value=_fmt(leg), location=_where(leg.source), used=leg is winner)
                   for lab, leg, _ in items]
        if not disagree and len({lab for lab, _, _ in items}) == 1:
            findings.append(Finding(rule=f"flights.{direction}", verdict=Verdict.UNRESOLVED,
                                    title=f"{direction.capitalize()} flight {_fmt(winner)} is only in the {items[0][0]}",
                                    detail="Upload the e-ticket or preparation letter to confirm it.",
                                    evidence=evidence, accepted_value=_fmt(winner)))
            continue
        if not disagree:
            findings.append(Finding(rule=f"flights.{direction}", verdict=Verdict.VERIFIED,
                                    title=f"{direction.capitalize()} flight {_fmt(winner)} agrees in all documents",
                                    evidence=evidence, accepted_value=_fmt(winner)))
            continue
        lines = [f"{items[0][0]}: {_fmt(winner)} (used)"] + [f"{lab}: {_fmt(leg)}" for lab, leg in disagree]
        detail = "\n".join(lines)
        if any(lab == "PNR" for lab, _ in disagree):
            detail += "\nThe PNR still holds a segment that differs from the ticket: check it was cancelled."
        findings.append(Finding(
            rule=f"flights.{direction}", verdict=Verdict.INCORRECT,
            title=f"{direction.capitalize()} flight differs across {len({l for l, _ in disagree}) + 1} sources",
            detail=detail, evidence=evidence, accepted_value=_fmt(winner), compare=compare))
    return findings


# ---------------------------------------------------------------- dates

def check_dates(b: GroupBundle, s: Settings) -> list[Finding]:
    stated = []
    if b.name_list and b.name_list.start_date:
        stated.append(("name list", b.name_list.start_date, b.name_list.end_date, b.name_list.source))
    if b.prep_letter and b.prep_letter.start_date:
        stated.append(("preparation letter", b.prep_letter.start_date, b.prep_letter.end_date, b.prep_letter.source))
    fl = {f.rule: f for f in check_flights(b, s)}
    if not stated:
        return [Finding(rule="dates.trip", verdict=Verdict.UNRESOLVED, title="No travel dates found")]
    start, end = stated[0][1], stated[0][2]
    problems = [f"{lab}: {a} to {z}" for lab, a, z, _ in stated if (a, z) != (start, end)]
    # Flight dates must sit on the trip's first and last day.
    for direction, expected in (("outbound", start), ("return", end)):
        f = fl.get(f"flights.{direction}")
        if f and f.accepted_value:
            leg = next(leg for _, leg, p in sorted(_flight_sources(b), key=lambda x: x[2])
                       if _direction(leg, "BKK") == direction)
            leg_date = leg.date
            # A flight just after midnight belongs to the evening before: the group meets that night.
            after_midnight = direction == "outbound" and leg_date and expected and \
                leg_date - expected == timedelta(days=1) and (leg.dep_time or "99") < "06:00"
            if leg_date != expected and not after_midnight:
                problems.append(f"{direction} flight is on {leg_date}, trip says {expected}")
    if problems:
        return [Finding(rule="dates.trip", verdict=Verdict.INCORRECT, title="Travel dates disagree",
                        detail="\n".join(problems), evidence=[x[3] for x in stated])]
    return [Finding(rule="dates.trip", verdict=Verdict.VERIFIED,
                    title=f"Travel dates {start:%d %b} to {end:%d %b %Y} agree, flights on the first and last day",
                    evidence=[x[3] for x in stated], accepted_value=f"{start.isoformat()}/{end.isoformat()}")]


# ---------------------------------------------------------------- passengers vs tickets

def check_tickets(b: GroupBundle, s: Settings) -> list[Finding]:
    if b.name_list and not b.etickets and any(p.ticket_no for d in b.pnrs for p in d.passengers):
        return []  # the PNR carries the ticket numbers; check_pnr_names covers them
    if not b.name_list or not b.etickets:
        return [Finding(rule="passengers.tickets", verdict=Verdict.UNRESOLVED,
                        title="Cannot check tickets: name list or e-ticket missing")]
    group = {p.airline_name: p for p in b.name_list.passengers}
    findings = []
    for doc in b.etickets:
        tickets = defaultdict(list)
        for t in doc.tickets:
            tickets[t.passenger_name].append(t)
        missing = [n for n in group if n not in tickets]
        extra = sorted(n for n in tickets if n not in group)
        dup = [n for n, ts in tickets.items() if len(ts) > 1]
        wrong_pp = [n for n in group if n in tickets and group[n].passport_no
                    and tickets[n][0].passport_no and tickets[n][0].passport_no != group[n].passport_no]
        legs = sorted({_fmt(l) for ts in tickets.values() for t in ts for l in t.legs})
        if missing or wrong_pp or dup:
            lines = ([f"No ticket: {', '.join(missing)}"] if missing else []) + \
                    ([f"Passport differs: {', '.join(wrong_pp)}"] if wrong_pp else []) + \
                    ([f"More than one ticket: {', '.join(dup)}"] if dup else [])
            findings.append(Finding(rule="passengers.tickets", verdict=Verdict.INCORRECT,
                                    title=f"Tickets do not cover the group correctly ({doc.source.file})",
                                    detail="\n".join(lines), evidence=[doc.source]))
        else:
            findings.append(Finding(
                rule="passengers.tickets", verdict=Verdict.VERIFIED,
                title=f"All {len(group)} travellers hold a ticket with a matching passport ({'; '.join(legs)})",
                evidence=[tickets[n][0].source for n in group]))
        if extra:
            findings.append(Finding(
                rule="passengers.extra_tickets", verdict=Verdict.UNRESOLVED,
                title=f"{len(extra)} ticket(s) for people not on the name list",
                detail="\n".join(f"{n} (record {tickets[n][0].airline_record}, {tickets[n][0].source.location})"
                                 for n in extra) + "\nAnother group's tickets, or missing from the name list?",
                evidence=[tickets[n][0].source for n in extra]))
    return findings


def check_pnr_names(b: GroupBundle, s: Settings) -> list[Finding]:
    if not b.name_list or not b.pnrs:
        return []
    group = {p.airline_name for p in b.name_list.passengers}
    findings = []
    by_name = {p.airline_name: p for p in b.name_list.passengers}
    for doc in b.pnrs:
        pnr = {p.name: p for p in doc.passengers}
        missing = sorted(group - set(pnr))
        extra = sorted(set(pnr) - group)
        no_tk = sorted(p.name for p in doc.passengers if p.name in group and not p.ticket_no)
        # A name missing on one side and a near-identical one on the other is a spelling difference.
        pairs = []
        for n in list(missing):
            best = max(extra, key=lambda e: SequenceMatcher(None, n, e).ratio(), default=None)
            if best and SequenceMatcher(None, n, best).ratio() >= 0.8:
                pairs.append((n, best))
                missing.remove(n)
                extra.remove(best)
        for n, other in pairs:
            p = by_name[n]
            findings.append(Finding(
                rule="passengers.spelling_pnr", verdict=Verdict.INCORRECT,
                title=f"Name spelled differently: {n} (name list) / {other} (PNR)",
                detail=f"Check the passport of {p.title or ''} {p.given_name_th or ''} {p.surname_th or ''}".strip()
                       + (f" ({p.passport_no})" if p.passport_no else "")
                       + ". The ticket must match the passport exactly.",
                evidence=[p.source, pnr[other].source if hasattr(pnr[other], "source") else doc.source],
                compare=[Compare(source="name list", value=n, location=_where(p.source)),
                         Compare(source="PNR / ticket", value=other + (f" (ticket {pnr[other].ticket_no})" if pnr[other].ticket_no else ""),
                                 location=_where(doc.source))]))
        if missing or no_tk or extra:
            findings.append(Finding(rule="passengers.pnr", verdict=Verdict.INCORRECT,
                                    title="PNR does not match the name list",
                                    detail="\n".join(([f"Not in PNR: {', '.join(missing)}"] if missing else []) +
                                                     ([f"In PNR but not on the name list: {', '.join(extra)}"] if extra else []) +
                                                     ([f"No ticket number: {', '.join(no_tk)}"] if no_tk else [])),
                                    evidence=[doc.source]))
        elif pairs:
            findings.append(Finding(rule="passengers.pnr", verdict=Verdict.VERIFIED,
                                    title=f"All {len(group)} travellers are in the PNR with ticket numbers "
                                          f"({len(pairs)} spelled differently, see above)", evidence=[doc.source]))
        else:
            findings.append(Finding(rule="passengers.pnr", verdict=Verdict.VERIFIED,
                                    title=f"All {len(group)} travellers are in the PNR with ticket numbers",
                                    evidence=[doc.source]))
    return findings


# ---------------------------------------------------------------- pax and rooms

def _merge_dbl(mix: dict) -> dict:
    out = {k: v for k, v in mix.items() if v and k not in ("DBL", "T/L")}
    if mix.get("DBL"):
        out["TWN"] = out.get("TWN", 0) + mix["DBL"]
    if mix.get("T/L"):
        out["SGL"] = out.get("SGL", 0) + mix["T/L"]
    return out


def _mix_txt(mix: dict) -> str:
    return " + ".join(f"{n} {k}" for k, n in mix.items() if n) or "-"


def check_pax_rooms(b: GroupBundle, s: Settings) -> list[Finding]:
    nl = b.name_list
    if not nl:
        return []
    problems = []
    if nl.pax_total is not None and nl.pax_total != len(nl.passengers):
        problems.append(f"Header says {nl.pax_total} pax, the list has {len(nl.passengers)} people")
    rooms = defaultdict(list)
    for p in nl.passengers:
        rooms[p.room].append(p)
    if nl.rooms_total is not None and nl.rooms_total != len(rooms):
        problems.append(f"Header says {nl.rooms_total} rooms, the list uses {len(rooms)}")
    mix = Counter((r or "")[:3] for r in rooms)
    notes = []
    if nl.room_mix and dict(mix) != nl.room_mix:
        if _merge_dbl(dict(mix)) == _merge_dbl(nl.room_mix):
            notes.append(f"The header counts {mix.get('DBL', 0)} double room(s) as twin ({_mix_txt(nl.room_mix)}); "
                         f"the room list uses {_mix_txt(dict(mix))}. Make sure the hotel and land operator know which rooms need a double bed.")
        else:
            problems.append(f"Header room mix {_mix_txt(nl.room_mix)} differs from the rooms used {_mix_txt(dict(mix))}")
    for room, people in rooms.items():
        cap = ROOM_CAPACITY.get((room or "")[:3])
        if cap and len(people) != cap:
            problems.append(f"{room} has {len(people)} people")
    if problems:
        return [Finding(rule="pax.rooms", verdict=Verdict.INCORRECT, title="Pax or rooming does not add up",
                        detail="\n".join(problems + notes), evidence=[nl.source],
                        compare=[Compare(source="name list header", value=_mix_txt(nl.room_mix or {}), location=_where(nl.source)),
                                 Compare(source="name list rooms", value=_mix_txt(dict(mix)), location=_where(nl.source))])]
    mix_txt = " + ".join(f"{n} {k}" for k, n in nl.room_mix.items())
    out = []
    if notes:
        out.append(Finding(rule="pax.rooms_dbl", verdict=Verdict.INFO, title="Double rooms are counted as twin in the header",
                           detail="\n".join(notes), evidence=[nl.source],
                           compare=[Compare(source="name list header", value=_mix_txt(nl.room_mix), location=_where(nl.source)),
                                    Compare(source="name list rooms", value=_mix_txt(dict(mix)), location=_where(nl.source), used=True)]))
    return out + [Finding(rule="pax.rooms", verdict=Verdict.VERIFIED,
                    title=f"{len(nl.passengers)} pax, {len(rooms)} rooms ({mix_txt}) match the room list",
                    evidence=[nl.source], accepted_value=f"{len(nl.passengers)} pax / {len(rooms)} rooms ({mix_txt})")]


# ---------------------------------------------------------------- hotels, land, options

def _same_hotel(a: str, b: str) -> bool:
    norm = lambda x: "".join(ch for ch in x.upper() if ch.isalnum()).replace("HOTEL", "").replace("酒店", "")  # noqa: E731
    if SequenceMatcher(None, norm(a), norm(b)).ratio() >= 0.8:
        return True
    la, lb = (_latin(x).replace("HOTEL", "") for x in (a, b))
    if len(la) >= 5 and len(lb) >= 5:  # "Rezen Kingtown Hotel (…) 丽呈君顿酒店" against an English-only name
        short, long_ = sorted((la, lb), key=len)
        return SequenceMatcher(None, la, lb).ratio() >= 0.8 or (len(short) >= 10 and short in long_)
    return False


def _han(text: str) -> str:
    return "".join(ch for ch in text if "\u4e00" <= ch <= "\u9fff")


def _latin(text: str) -> str:
    return "".join(ch for ch in text.upper() if "A" <= ch <= "Z")


def _comparable(a: str, b: str) -> bool:
    """Both names have a Latin-letter part, or both are in Chinese. A Thai/English name against a Chinese-only
    one can't be compared by spelling."""
    if len(_latin(a)) >= 5 and len(_latin(b)) >= 5:
        return True
    return bool(_han(a)) == bool(_han(b))


def _hotel_compare(prep: HotelNight, land: HotelNight) -> list[Compare]:
    def show(h):
        return " · ".join(x for x in (h.hotel_name, h.address, h.phone and f"tel {h.phone}") if x)
    return [Compare(source="preparation letter", value=show(prep), location=_where(prep.source)),
            Compare(source="land confirmation", value=show(land), location=_where(land.source), used=True)]


def check_hotels(b: GroupBundle, s: Settings) -> list[Finding]:
    findings = []
    pl, lc, nl = b.prep_letter, b.land_confirmation, b.name_list
    if pl and nl and nl.start_date and nl.end_date:
        nights = {nl.start_date + timedelta(days=i) for i in range((nl.end_date - nl.start_date).days)}
        covered = {h.date + timedelta(days=k) for h in pl.hotels for k in range(h.nights)}
        if nights - covered:
            findings.append(Finding(rule="hotels.coverage", verdict=Verdict.INCORRECT,
                                    title="Preparation letter is missing a hotel night",
                                    detail=", ".join(str(d) for d in sorted(nights - covered)), evidence=[pl.source]))
    if not pl:
        if lc and lc.hotels:
            findings.append(Finding(
                rule="hotels.match", verdict=Verdict.UNRESOLVED,
                title=f"{len(lc.hotels)} hotel nights read from the land confirmation, no preparation letter to compare",
                detail="\n".join(f"{h.date:%d %b}: {h.hotel_name}" + (f" (tel {h.phone})" if h.phone else "") for h in lc.hotels),
                evidence=[h.source for h in lc.hotels]))
        return findings
    if not lc:
        findings.append(Finding(
            rule="hotels.match", verdict=Verdict.UNRESOLVED,
            title=f"{len(pl.hotels)} hotel nights read from the preparation letter, not confirmed by the land operator",
            detail="Upload the land confirmation to check each night.", evidence=[h.source for h in pl.hotels]))
        return findings
    land = {h.date: h for h in lc.hotels}
    rows = []  # (night, verdict, kind, prep, land)
    for h in pl.hotels:
        for k in range(h.nights):
            night = h.date + timedelta(days=k)
            other = land.get(night)
            if not other:
                rows.append((night, Verdict.UNRESOLVED, "missing", h, None))
            elif _same_hotel(h.hotel_name, other.hotel_name) or (
                    _han(h.hotel_name) and _han(other.hotel_name) and _han(other.hotel_name).replace("酒店", "") in _han(h.hotel_name)):
                rows.append((night, Verdict.VERIFIED, "same", h, other))
            elif not _comparable(h.hotel_name, other.hotel_name):
                rows.append((night, Verdict.UNRESOLVED, "language", h, other))
            else:
                rows.append((night, Verdict.INCORRECT, "differ", h, other))
    # One finding per run of consecutive nights with the same pair of hotels.
    groups: list[list] = []
    for r in rows:
        last = groups[-1][-1] if groups else None
        if last and (r[0] - last[0]).days == 1 and r[1:3] == last[1:3] and r[3].hotel_name == last[3].hotel_name \
                and (r[4].hotel_name if r[4] else None) == (last[4].hotel_name if last[4] else None):
            groups[-1].append(r)
        else:
            groups.append([r])
    for g in groups:
        first, lastn = g[0][0], g[-1][0]
        when = f"Night {first:%d %b}" if len(g) == 1 else f"Nights {first:%d}–{lastn:%d %b} ({len(g)} nights)"
        _, verdict, kind, h, other = g[0]
        ev = [h.source] + ([other.source] if other else [])
        if kind == "missing":
            findings.append(Finding(rule="hotels.match", verdict=verdict, title=f"{when}: land confirmation names no hotel",
                                    detail=f"Preparation letter: {h.hotel_name}", evidence=ev))
        elif kind == "same":
            findings.append(Finding(rule="hotels.match", verdict=verdict, title=f"{when}: {other.hotel_name} matches",
                                    evidence=ev, accepted_value=other.hotel_name))
        elif kind == "language":
            findings.append(Finding(rule="hotels.match", verdict=verdict,
                                    title=f"{when}: hotel names are in different languages, check they are the same",
                                    detail="Compare the address and phone below.", evidence=ev,
                                    accepted_value=other.hotel_name, compare=_hotel_compare(h, other)))
        else:
            findings.append(Finding(rule="hotels.match", verdict=verdict, title=f"{when}: hotels differ",
                                    detail="The preparation letter and the land confirmation name different hotels. "
                                           "Ask the land operator which one is booked, then correct the other document.",
                                    evidence=ev, accepted_value=other.hotel_name, compare=_hotel_compare(h, other)))
    return findings


def check_land_contacts(b: GroupBundle, s: Settings) -> list[Finding]:
    lc = b.land_confirmation
    if not lc:
        return [Finding(rule="contacts.land", verdict=Verdict.UNRESOLVED,
                        title="Local guide and land operator contacts unknown",
                        detail="No land confirmation uploaded; these lines stay marked in the TL message.")]
    out = []
    if lc.layout != "team_confirmation":
        out.append(Finding(rule="land.layout", verdict=Verdict.UNRESOLVED,
                           title=f"{lc.source.file}: new confirmation layout, read with general rules",
                           detail="Check the hotels, guide, dates and head count below against the file itself. "
                                  "Send a copy so a reader for this layout can be added.",
                           evidence=[lc.source]))
    if not lc.guides or not any(g.phone for g in lc.guides):
        hint = "\n".join([f"The land operator left the guide blank: {n.raw.split(': ', 1)[-1]} ({n.location})"
                          for n in lc.notes if "guide" in (n.raw or "")] +
                         [f"Contact on the document: {c.role} {c.name} {c.phone or ''} ({c.source.location})" +
                          (". It may be the guide; ask the land operator." if c.role == "unlabelled" else "")
                          for c in lc.other_contacts])
        out.append(Finding(rule="contacts.guide", verdict=Verdict.UNRESOLVED,
                           title="No guide named in the land confirmation yet" if not lc.guides
                           else "No guide phone in the land confirmation",
                           detail=hint, evidence=[c.source for c in lc.other_contacts] or [lc.source]))
    else:
        g = lc.guides[0]
        out.append(Finding(rule="contacts.guide", verdict=Verdict.VERIFIED, title=f"Guide {g.name} {g.phone}",
                           evidence=[g.source], accepted_value=f"{g.name} {g.phone}"))
    if not lc.land_operator or not (lc.land_operator.phone or lc.land_operator.wechat):
        lo = lc.land_operator
        out.append(Finding(rule="contacts.land", verdict=Verdict.UNRESOLVED,
                           title=f"No phone for the land operator{' ' + lo.name if lo else ''}",
                           detail=f"Sender: {lo.nickname}" if lo and lo.nickname else "",
                           evidence=[lo.source] if lo else [lc.source]))
    else:
        lo = lc.land_operator
        out.append(Finding(rule="contacts.land", verdict=Verdict.VERIFIED, title=f"Land operator {lo.name}",
                           evidence=[lo.source], accepted_value=lo.name))
    return out


ROOM_WORDS = {"T/L": "SGL", "标准间": "TWN", "双床房": "TWN", "大床房": "DBL", "单人间": "SGL", "三人间": "TRP", "家庭房": "TRP"}


def _letters(name: Optional[str]) -> str:
    return "".join(ch for ch in (name or "").upper() if "A" <= ch <= "Z")


def check_land_group(b: GroupBundle, s: Settings) -> list[Finding]:
    """The land operator's confirmation against our name list: dates, head count, tour leader, rooms."""
    lc, nl = b.land_confirmation, b.name_list
    if not lc:
        return []
    out = []
    if not nl:
        out.append(Finding(rule="land.group", verdict=Verdict.UNRESOLVED,
                           title="Land confirmation read, but no name list to check it against", evidence=[lc.source]))
        return out
    if lc.start_date and nl.start_date:
        same = (lc.start_date, lc.end_date) == (nl.start_date, nl.end_date)
        out.append(Finding(rule="land.dates", verdict=Verdict.VERIFIED if same else Verdict.INCORRECT,
                           title=f"Land confirmation dates {lc.start_date:%d %b} to {lc.end_date:%d %b} "
                                 + ("match the name list" if same else f"differ from the name list ({nl.start_date:%d %b} to {nl.end_date:%d %b})"),
                           evidence=[lc.source, nl.source],
                           compare=[] if same else [
                               Compare(source="land confirmation", value=f"{lc.start_date} – {lc.end_date}", location=_where(lc.source)),
                               Compare(source="name list", value=f"{nl.start_date} – {nl.end_date}", location=_where(nl.source), used=True)]))
    if lc.pax_total is not None:
        customers = sum(not p.is_tour_leader for p in nl.passengers)
        tls = len(nl.passengers) - customers
        ok = lc.adults == customers and (lc.escorts or 0) == tls
        out.append(Finding(rule="land.pax", verdict=Verdict.VERIFIED if ok else Verdict.INCORRECT,
                           title=f"Land operator expects {lc.adults} guests + {lc.escorts or 0} TL"
                                 + ("" if ok else f", name list has {customers} + {tls}"),
                           evidence=[lc.source, nl.source],
                           compare=[] if ok else [
                               Compare(source="land confirmation", value=f"{lc.adults} + {lc.escorts or 0}", location=_where(lc.source)),
                               Compare(source="name list", value=f"{customers} + {tls}", location=_where(nl.source), used=True)]))
    if lc.tour_leader and nl.tour_leader:
        ok = _letters(lc.tour_leader) in _letters(nl.tour_leader) or SequenceMatcher(
            None, _letters(lc.tour_leader), _letters(nl.tour_leader)).ratio() >= 0.85
        out.append(Finding(rule="land.tour_leader", verdict=Verdict.VERIFIED if ok else Verdict.INCORRECT,
                           title=f"Tour leader on the land confirmation: {lc.tour_leader}"
                                 + ("" if ok else f", name list says {nl.tour_leader}"),
                           evidence=[lc.source, nl.source]))
    if lc.rooms:
        land_mix: dict[str, int] = {}
        for word, n in lc.rooms.items():
            land_mix[ROOM_WORDS.get(word, word)] = land_mix.get(ROOM_WORDS.get(word, word), 0) + n
        ours = {k: v for k, v in Counter((p.room or "")[:3] for p in {p.room: p for p in nl.passengers}.values()).items() if v} \
            or {k: v for k, v in (nl.room_mix or {}).items() if v}
        ok = _merge_dbl(land_mix) == _merge_dbl(ours)
        dbl_note = ok and land_mix.get("DBL", 0) != ours.get("DBL", 0)
        out.append(Finding(rule="land.rooms",
                           verdict=Verdict.UNRESOLVED if dbl_note else Verdict.VERIFIED if ok else Verdict.INCORRECT,
                           title="Rooms on the land confirmation: " + ", ".join(f"{n} {w}" for w, n in lc.rooms.items())
                                 + (" — same count, but the name list has double rooms" if dbl_note
                                    else "" if ok else " — name list: " + _mix_txt(ours)),
                           detail="" if ok and not dbl_note else "标准间 = twin, 大床房 = double, T/L = tour leader's room. "
                                  "Send the land operator the rooming list with the double rooms marked.",
                           evidence=[lc.source, nl.source],
                           compare=[Compare(source="land confirmation", value=_mix_txt(lc.rooms), location=_where(lc.source)),
                                    Compare(source="name list rooms", value=_mix_txt(ours), location=_where(nl.source), used=True)]))
    return out


def check_optional_tours(b: GroupBundle, s: Settings) -> list[Finding]:
    prep = b.prep_letter.optional_tours if b.prep_letter else []
    land = b.land_confirmation.optional_tours if b.land_confirmation else []
    if not prep and not land:
        return []
    fmt = lambda o: f"{o.name}: {o.price:g} {o.currency or ''}".strip()  # noqa: E731
    compare = [Compare(source="preparation letter", value=fmt(o), location=_where(o.source)) for o in prep] + \
              [Compare(source="land confirmation", value=fmt(o), location=_where(o.source), used=True) for o in land]
    if not land:
        return [Finding(rule="options.price", verdict=Verdict.UNRESOLVED,
                        title="Optional tour price not confirmed by the land operator",
                        detail="\n".join(f"{o.name}: {o.price:g} {o.currency} ({o.source.location})" for o in prep),
                        evidence=[o.source for o in prep], compare=compare)]
    if not prep:
        return [Finding(rule="options.price", verdict=Verdict.INFO,
                        title=f"{len(land)} optional tour(s) offered by the land operator",
                        detail="\n".join(fmt(o) for o in land), evidence=[o.source for o in land], compare=compare)]
    land_prices = {o.price for o in land}
    missing = [o for o in prep if o.price not in land_prices]
    if missing:
        return [Finding(rule="options.price", verdict=Verdict.INCORRECT,
                        title="Optional tour price in the preparation letter is not on the land confirmation",
                        detail="\n".join(f"{fmt(o)} ({o.source.location})" for o in missing)
                               + "\nLand confirmation offers: " + "; ".join(fmt(o) for o in land),
                        evidence=[o.source for o in prep + land], compare=compare)]
    return [Finding(rule="options.price", verdict=Verdict.UNRESOLVED,
                    title="Optional tour prices appear on both documents; check the tours are the same",
                    detail="Names are in different languages, so they are matched by price only.",
                    evidence=[o.source for o in prep + land], compare=compare)]


# ---------------------------------------------------------------- passports and people

def match_scans(passengers, scans, min_ratio: float = 0.8) -> dict:
    """Pair travellers with passport scans one to one: by passport number first, then by
    the closest name. A scan is never given to two people (sisters share a surname)."""
    out, used = {}, set()
    for p in passengers:
        if not p.passport_no:
            continue
        hit = next((sc for sc in scans if id(sc) not in used and sc.passport_no
                    and sc.passport_no.upper() == p.passport_no.upper()), None)
        if hit:
            out[id(p)] = hit
            used.add(id(hit))
    pairs = []
    for p in passengers:
        if id(p) in out:
            continue
        key = _letters(p.airline_name)
        for sc in scans:
            if id(sc) not in used:
                r = SequenceMatcher(None, _letters(getattr(sc, "airline_name", None) or sc.name), key).ratio()
                if r >= min_ratio:
                    pairs.append((r, id(p), sc))
    for r, pid, sc in sorted(pairs, key=lambda t: -t[0]):
        if pid not in out and id(sc) not in used:
            out[pid] = sc
            used.add(id(sc))
    for p in passengers:
        if id(p) in out:
            _repair_separator(out[id(p)], p)
    return out


def _repair_separator(sc, p) -> None:
    """The '<<' between surname and given name is often read as 'K' or 'KK' ("TESTSURNAMEKMALI").
    When the passenger's own name explains the reading exactly, split it the same way."""
    if getattr(sc, "given_name", None) != "" or not p.surname or not p.given_name:
        return
    sur, given = _letters(p.surname), _letters(p.given_name)
    if re.fullmatch(re.escape(sur) + "K{1,2}" + re.escape(given), _letters(sc.surname)):
        sc.surname, sc.given_name = p.surname.upper(), p.given_name.upper()


def check_passport_scans(b: GroupBundle, s: Settings) -> list[Finding]:
    """Name list (and tickets) against the passports themselves."""
    nl, scans = b.name_list, b.passports
    if not scans:
        return []
    if not nl:
        return [Finding(rule="passports.scan", verdict=Verdict.INFO, title=f"{len(scans)} passport(s) read, no name list to compare",
                        detail="\n".join(f"{sc.airline_name} {sc.passport_no or '?'} exp {sc.expiry or '?'}" for sc in scans),
                        evidence=[sc.source for sc in scans])]
    out, matched, ok = [], set(), []
    tickets = {p.name: p for d in b.pnrs for p in d.passengers}
    for d in b.etickets:
        for tk in d.tickets:
            tickets.setdefault(tk.passenger_name, tk)
    pairs = match_scans(nl.passengers, scans)
    for p in nl.passengers:
        sc = pairs.get(id(p))
        if not sc:
            continue
        matched.add(id(sc))
        diffs = []
        if _letters(sc.airline_name) != _letters(p.airline_name):
            diffs.append("name")
        if sc.passport_no and p.passport_no and sc.passport_no != p.passport_no.upper():
            diffs.append("passport number")
        if sc.dob and p.dob and sc.dob != p.dob:
            diffs.append("date of birth")
        if sc.expiry and p.passport_expiry and sc.expiry != p.passport_expiry:
            diffs.append("expiry")
        tk = next((t for n, t in tickets.items() if _letters(n) == _letters(p.airline_name)), None) or \
            next((t for n, t in tickets.items() if SequenceMatcher(None, _letters(n), _letters(sc.airline_name)).ratio() >= 0.85), None)
        tk_name = getattr(tk, "name", None) or getattr(tk, "passenger_name", None)
        if tk_name and _letters(tk_name) != _letters(sc.airline_name):
            diffs.append("ticket name")
        if not diffs:
            ok.append(sc)
            continue
        show = lambda name, no, dob, exp: " · ".join(x for x in (name, no and f"PP {no}", dob and f"DOB {dob}", exp and f"exp {exp}") if x)  # noqa: E731
        compare = [Compare(source="passport (MRZ)", value=show(sc.airline_name, sc.passport_no, sc.dob, sc.expiry),
                           location=_where(sc.source), used=True),
                   Compare(source="name list", value=show(p.airline_name, p.passport_no, p.dob, p.passport_expiry),
                           location=_where(p.source))]
        if tk_name:
            compare.append(Compare(source="ticket / PNR", value=tk_name, location=_where(getattr(tk, "source", None))))
        out.append(Finding(rule="passports.scan", verdict=Verdict.INCORRECT,
                           title=f"{p.given_name_th or p.given_name} {p.surname_th or p.surname}: {', '.join(diffs)} differ from the passport",
                           detail="The passport is the reference. Correct the name list"
                                  + (" and ask the airline to fix the ticket name" if "ticket name" in diffs else "") + ".",
                           evidence=[sc.source, p.source], accepted_value=sc.airline_name, compare=compare))
    unread = [sc for sc in scans if not sc.checks_ok]
    if unread:
        out.append(Finding(rule="passports.unreadable", verdict=Verdict.UNRESOLVED,
                           title=f"{len(unread)} passport(s) only partly readable",
                           detail="\n".join(f"{sc.airline_name} ({sc.source.location}): a check digit failed; values that failed are left out"
                                            for sc in unread) + "\nRescan these pages straight and in focus.",
                           evidence=[sc.source for sc in unread]))
    extra = [sc for sc in scans if id(sc) not in matched]
    if extra:
        out.append(Finding(rule="passports.extra", verdict=Verdict.UNRESOLVED,
                           title=f"{len(extra)} passport(s) not on the name list",
                           detail="\n".join(f"{sc.airline_name} {sc.passport_no or ''} ({sc.source.location})" for sc in extra),
                           evidence=[sc.source for sc in extra]))
    no_scan = [p for p in nl.passengers if id(p) not in pairs]
    if no_scan:
        out.append(Finding(rule="passports.missing", verdict=Verdict.INFO,
                           title=f"{len(no_scan)} traveller(s) without a passport scan",
                           detail=", ".join(p.airline_name for p in no_scan), evidence=[nl.source]))
    if ok:
        out.append(Finding(rule="passports.scan", verdict=Verdict.VERIFIED,
                           title=f"{len(ok)} passport(s) match the name list" + (" and tickets" if tickets else ""),
                           evidence=[sc.source for sc in ok]))
    return out


def check_passports(b: GroupBundle, s: Settings) -> list[Finding]:
    nl = b.name_list
    if not nl or not nl.end_date:
        return []
    limit = nl.end_date + timedelta(days=round(s.passport_min_months * 30.5))
    # Expiry from the name list, or from the passport scan when the list has none.
    pairs = match_scans(nl.passengers, b.passports)
    expiry = {id(p): p.passport_expiry or getattr(pairs.get(id(p)), "expiry", None) for p in nl.passengers}
    short = [p for p in nl.passengers if expiry[id(p)] and expiry[id(p)] < limit]
    missing = [p for p in nl.passengers if not expiry[id(p)]]
    if short or missing:
        return [Finding(rule="passports.validity", verdict=Verdict.INCORRECT,
                        title="Passport validity problem",
                        detail="\n".join([f"{p.airline_name}: expires {expiry[id(p)]}" for p in short] +
                                         [f"{p.airline_name}: no expiry date" for p in missing]),
                        evidence=[p.source for p in short + missing])]
    return [Finding(rule="passports.validity", verdict=Verdict.VERIFIED,
                    title=f"All passports valid {s.passport_min_months}+ months after return", evidence=[nl.source])]


def _thai_name(p: Passenger) -> str:
    if p.given_name_th:
        return f"{p.given_name_th} {p.surname_th or ''}".strip()
    return p.airline_name


def check_special_cases(b: GroupBundle, s: Settings) -> list[Finding]:
    nl = b.name_list
    if not nl:
        return []
    out = []
    bookings = defaultdict(list)
    for p in nl.passengers:
        bookings[p.booking_no].append(p)
    for p in nl.passengers:
        if p.age is not None and p.age >= s.elderly_age:
            out.append(Finding(rule="people.elderly", verdict=Verdict.INFO,
                               title=f"{_thai_name(p)}, {p.age}: needs extra care during the trip", evidence=[p.source]))
        if p.age is not None and p.age < s.minor_age:
            companions = [q for q in bookings[p.booking_no] if q is not p and (q.age or 0) >= s.minor_age]
            if not any(q.surname == p.surname for q in companions):
                with_ = ", ".join(f"{_thai_name(q)} ({q.age})" for q in companions) or "no adult in the booking"
                out.append(Finding(
                    rule="people.minor", verdict=Verdict.INFO,
                    title=f"{_thai_name(p)}, {p.age}: travels without a same-surname parent",
                    detail=f"With {with_}. Check parental consent letter and birth certificate before check-in.",
                    evidence=[p.source]))
        if p.remark:
            out.append(Finding(rule="people.remark", verdict=Verdict.INFO,
                               title=f"{_thai_name(p)}: {' '.join(p.remark.split())}", evidence=[p.source]))
    return out


def check_name_spelling(b: GroupBundle, s: Settings) -> list[Finding]:
    nl = b.name_list
    if not nl:
        return []
    out = []
    by_booking = defaultdict(list)
    for p in nl.passengers:
        by_booking[p.booking_no].append(p)
    for people in by_booking.values():
        surnames = sorted({p.surname for p in people})
        for i, a in enumerate(surnames):
            for z in surnames[i + 1:]:
                if SequenceMatcher(None, a, z).ratio() >= 0.85:
                    out.append(Finding(rule="people.spelling", verdict=Verdict.INFO,
                                       title=f"Similar surnames in one booking: {a} / {z}",
                                       detail="Check against the passports that the different spelling is intended.",
                                       evidence=[p.source for p in people if p.surname in (a, z)]))
    return out


# ---------------------------------------------------------------- expense template

def check_expense_template(b: GroupBundle, s: Settings) -> list[Finding]:
    et, nl = b.expense_template, b.name_list
    if not et or not nl:
        return []
    from .companies import load_companies
    logos = {c.logo_sha256 for c in load_companies().values() if c.logo_sha256}
    stray = [img for img in et.images if img.value not in logos]
    image_findings = [Finding(
        rule="expense.images", verdict=Verdict.INCORRECT,
        title=f"Expense template holds {len(stray)} picture(s) that are not a company logo",
        detail="\n".join(f"Picture at {img.source.location}" for img in stray) +
               "\nMost likely a copy of a previous payee's ID card. The workbook filler removes it;"
               " attach the current TL's ID copy instead.",
        evidence=[img.source for img in stray])] if stray else []
    pax = len(nl.passengers)
    customers = sum(1 for p in nl.passengers if not p.is_tour_leader)
    days = (nl.end_date - nl.start_date).days + 1 if nl.start_date and nl.end_date else None
    checks = [
        # The insurance and cover sheets print the tip-sheet pax as "N + 1 TL", so it counts customers only.
        ("tip_pax", (customers,), "tip sheet pax (customers)"),
        ("cover_pax", (pax,), "cover sheet pax"),
        ("tl_tip_people", (customers,), "TL tip people (customers)"),
        ("tip_days", (days,), "tip sheet days"),
        ("allowance_days", (days,), "allowance days"),
        ("tour_code", (nl.tour_code,), "tour code"),
    ]
    if s.gift_budget_per_pax is not None:
        checks.append(("cover_budget_per_pax", (s.gift_budget_per_pax,), "gift budget per pax (baht)"))
    wrong, ok = [], []
    for slot, allowed, label in checks:
        cell = et.slots.get(slot)
        if cell is None:
            continue
        v = cell.value
        if isinstance(v, str) and v.startswith("="):
            continue  # a formula linked to another cell; the filler leaves it alone
        good = any(v == a or (isinstance(v, str) and str(a) == v.strip()) for a in allowed if a is not None)
        (ok if good else wrong).append((label, v, allowed, cell))
    if wrong:
        return image_findings + [Finding(
            rule="expense.leftovers", verdict=Verdict.INCORRECT,
            title=f"Expense template holds {len(wrong)} value(s) that do not fit this group",
            detail="\n".join(f"{label}: {v} at {c.source.location}, expected {' or '.join(str(a) for a in allowed)}"
                             for label, v, allowed, c in wrong) + "\nThe workbook download fills these in for you.",
            evidence=[c.source for *_, c in wrong],
            compare=[Compare(source=f"template {label}", value=f"{v}  →  {' or '.join(str(a) for a in allowed)}",
                             location=_where(c.source)) for label, v, allowed, c in wrong])]
    return image_findings + [Finding(rule="expense.leftovers", verdict=Verdict.VERIFIED, title="Expense template values fit this group",
                    evidence=[c.source for *_, c in ok])]
