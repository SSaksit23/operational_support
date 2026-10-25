"""TL assignment message (มอบหมายงานทัวร์), drafted only from checked facts.

The layout follows the office's example message. Every factual line comes
from the bundle and the verification findings; nothing is invented. Where
a fact is missing or unresolved, the line says so in brackets so the
officer sees it before sending.

Drafting is refused while any `incorrect` finding is neither acknowledged
by the officer nor fixed by a later step (the expense workbook filler fixes
`expense.leftovers`).
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from pydantic import BaseModel

from .companies import Company
from .models import GroupBundle, Passenger
from .verify import Finding, Verdict

TH_MONTH_SHORT = ["ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.", "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]

# Airport -> (country code, Thai city name). Extend as new routes appear.
AIRPORTS = {"CKG": ("CN", "ฉงชิ่ง"), "BKK": ("TH", "กรุงเทพ"), "DMK": ("TH", "กรุงเทพ")}
# Words in a hotel's name or address -> Thai city name, for the "hotel moves" line.
HOTEL_CITIES = [("XUAN", "ซวนเอิน"), ("ENSHI", "เอินซือ"), ("CHONGQING", "ฉงชิ่ง"), ("ZHANGJIAJIE", "จางเจียเจี้ย")]
COUNTRY_NOTES = {
    "CN": ["จีนบล็อก LINE/Google ต้องเปิดโรมมิ่งหรือซิมที่ใช้ได้จริง ทดสอบก่อนออกจากสนามบิน",
           "เวลาจีนเร็วกว่าไทย 1 ชม."],
}
FIXED_BY_LATER_STEP = {"expense.leftovers", "expense.images"}  # the workbook filler fixes these


class MessageSettings(BaseModel):
    sender_particle: str = "ครับ"
    sender_line_name: str = "[ชื่อ LINE ของเรา]"
    tip_per_pax_thb: Optional[int] = None  # open question; the example used 2,000
    gift_budget_per_pax_thb: Optional[float] = 35
    report_days_after_trip: int = 3


class DraftBlocked(Exception):
    def __init__(self, findings: list[Finding]):
        self.findings = findings
        super().__init__("Fix or acknowledge first: " + "; ".join(f.title for f in findings))


def th_date(d: date) -> str:
    return f"{d.day} {TH_MONTH_SHORT[d.month - 1]}"


def th_range(a: date, z: date) -> str:
    yy = (z.year + 543) % 100
    if a.month == z.month:
        return f"{a.day}-{z.day} {TH_MONTH_SHORT[z.month - 1]} {yy:02d}"
    return f"{th_date(a)} - {th_date(z)} {yy:02d}"


def dot_time(hhmm: Optional[str]) -> str:
    return hhmm.replace(":", ".") if hhmm else "?"


def person(p: Passenger) -> str:
    prefix = "น้อง" if p.age is not None and p.age < 15 else "คุณ"
    if p.given_name_th:
        return f"{prefix}{p.given_name_th} {p.surname_th or ''}".strip()
    return f"{prefix}{p.given_name.title()} {p.surname.title()}"


def draft_tl_message(bundle: GroupBundle, findings: list[Finding], company: Company, today: date,
                     acknowledged: set[str] = frozenset(), settings: MessageSettings | None = None) -> str:
    s = settings or MessageSettings()
    open_incorrect = [f for f in findings if f.verdict == Verdict.INCORRECT
                      and f.rule not in acknowledged and f.rule not in FIXED_BY_LATER_STEP]
    if open_incorrect:
        raise DraftBlocked(open_incorrect)

    nl, pl, lc = bundle.name_list, bundle.prep_letter, bundle.land_confirmation
    if not nl or not nl.start_date:
        raise ValueError("A name list with travel dates is required")
    by_rule: dict[str, list[Finding]] = {}
    for f in findings:
        by_rule.setdefault(f.rule, []).append(f)
    k = s.sender_particle
    out: list[str] = []

    # Header
    out.append(f"มอบหมายงานทัวร์ | {nl.tour_code}")
    days_left = (nl.start_date - today).days
    when = {0: "วันนี้", 1: "พรุ่งนี้"}.get(days_left, f"ในอีก {days_left} วัน")
    out.append(f"*** ออกเดินทาง{when} {th_date(nl.start_date)} ***")
    out.append("")
    tl_contact = next((c for c in (pl.contacts if pl else []) if c.role == "tour_leader"), None)
    tl_call = tl_contact.nickname or tl_contact.name if tl_contact else (nl.tour_leader or "")
    out.append(f"สวัสดี{k}คุณ{tl_call}")
    out.append(f"รบกวนยืนยันรับงานกรุ๊ปนี้ด้วยนะ{k}")
    out.append("")

    # Group
    out.append("[ ข้อมูลกรุ๊ป ]")
    program = _program_line(nl.program_name, pl)
    out.append(f"- {program}")
    out.append(f"- เดินทาง {th_range(nl.start_date, nl.end_date)}")
    customers = [p for p in nl.passengers if not p.is_tour_leader]
    tls = [p for p in nl.passengers if p.is_tour_leader]
    rooms = len({p.room for p in nl.passengers})
    mix = " + ".join(f"{n}{t}" for t, n in nl.room_mix.items())
    out.append(f"- {len(customers)} ลูกค้า + TL {len(tls)} = {len(nl.passengers)} ท่าน / {rooms} ห้อง ({mix})")
    moves = _hotel_moves(pl)
    if moves:
        out.append(f"- {moves}")
    out.append("")

    # Flights
    out.append("[ ไฟลต์ ]")
    for direction, label in (("outbound", "ขาไป"), ("return", "ขากลับ")):
        f = (by_rule.get(f"flights.{direction}") or [None])[0]
        if not f or not f.accepted_value:
            out.append(f"{label} [ไม่พบข้อมูลไฟลต์ รอตรวจสอบ]")
            continue
        flight_no, day, mon, route, times = f.accepted_value.split(" ")[:5]
        leg_date = nl.start_date if direction == "outbound" else nl.end_date
        dep, arr = times.split("-")
        out.append(f"{label} {th_date(leg_date)} {flight_no} {route} {dot_time(dep)}-{dot_time(arr)}")
        if f.verdict == Verdict.INCORRECT:
            others = [ln for ln in f.detail.split("\n")[1:] if ":" in ln and not ln.startswith("The PNR")]
            stale = "; ".join(_th_source(ln) for ln in others)
            out.append(f"** {stale} ไม่ตรงกับตั๋ว รบกวนเช็กกับออฟฟิศให้ชัดก่อนแจ้งลูกค้า **")
    out.append("")

    # Meeting
    out.append("[ นัดพบลูกค้า ]")
    mt = pl.meeting if pl else None
    if mt and mt.date:
        out.append(f"{th_date(mt.date)} เวลา {dot_time(mt.time)} น.")
        place = " ".join(x for x in [mt.airport and mt.airport.replace("อาคารผู้โดยสารขาออก", "").strip(),
                                       mt.floor and f"ชั้น {mt.floor}", mt.gate and f"ประตู {mt.gate}",
                                       mt.counter and f"เคาน์เตอร์ {mt.counter}"] if x)
        out.append(place)
        if mt.sign:
            out.append(f'ถือป้าย "{mt.sign}"')
    else:
        out.append("[ไม่พบจุดนัดพบในใบเตรียมตัว]")
    em = next((c for c in (pl.contacts if pl else []) if c.role == "emergency"), None)
    if em:
        out += ["", f"เบอร์ฉุกเฉิน คุณ{em.name} {em.phone}"]
    out.append("")

    # On-site contacts
    out.append("[ ติดต่อหน้างาน ]")
    if lc and lc.guides:
        for g in lc.guides:
            out.append(f"ไกด์ท้องถิ่น คุณ{g.name} {g.phone or ''}".rstrip())
    elif lc:
        out.append("ไกด์ท้องถิ่น [แลนด์ยังไม่แจ้งชื่อไกด์ รอยืนยัน]")
    else:
        out.append("ไกด์ท้องถิ่น [รอใบคอนเฟิร์มแลนด์]")
    if lc and lc.land_operator:
        lo = lc.land_operator
        bits = [f"แลนด์ {lo.name}"] + ([f"(คุณ{lo.nickname})"] if lo.nickname else []) + \
               ([f"โทร {lo.phone}"] if lo.phone else []) + ([f"/ WeChat {lo.wechat}"] if lo.wechat else [])
        out.append(" ".join(bits))
    else:
        out.append("แลนด์ [รอใบคอนเฟิร์มแลนด์]")
    role_th = {"经理": "ผู้จัดการ", "总经理": "ผู้จัดการ", "OP": "OP", "联系人": "ผู้ติดต่อ", "负责人": "ผู้รับผิดชอบ",
               "计调": "OP", "操作": "OP", "销售": "เซลส์", "Manager": "ผู้จัดการ", "MANAGER": "ผู้จัดการ", "unlabelled": "ผู้ติดต่อ"}
    for c in (lc.other_contacts if lc else []):
        out.append(f"- {role_th.get(c.role, c.role)} {c.name} {c.phone or ''}".rstrip())
    out.append("")

    # Today's tasks
    out.append("[ สิ่งที่ต้องทำวันนี้ ]")
    tasks = ["รับเอกสารชุดเตรียมงาน + เซ็นใบปะหน้า (F01)"]
    confirm = []
    if any(f.verdict != Verdict.VERIFIED for f in by_rule.get("flights.return", [])):
        confirm.append("เวลาไฟลต์ขากลับ")
    if any(f.verdict != Verdict.VERIFIED for f in by_rule.get("flights.outbound", [])):
        confirm.append("เวลาไฟลต์ขาไป")
    if any(f.verdict != Verdict.VERIFIED for f in by_rule.get("hotels.match", [])):
        confirm.append("ที่พักทุกคืนกับแลนด์")
    if confirm:
        tasks.append("ยืนยัน" + " และ".join(confirm) + " (ดูหมายเหตุด้านล่าง)")
    call = "โทรหาลูกค้าให้ครบทุกท่าน"
    to_call = _agencies_to_call(nl.passengers)
    if to_call:
        call += " โดยเฉพาะ" + ", ".join(to_call) + " ที่ยังไม่ได้ติดต่อ"
    tasks.append(call)
    tasks.append("โพสต์นัดหมายในกลุ่มลูกค้า (จุดนัดพบ + สิ่งที่ต้องเตรียม + เบอร์ติดต่อ)")
    tip = f"{s.tip_per_pax_thb:,} บาท/ท่าน" if s.tip_per_pax_thb else "[ระบุค่าทิป] บาท/ท่าน"
    tasks.append(f"เก็บทิป {tip} ที่สนามบินวันเดินทาง")
    out += [f"{i}. {t}" for i, t in enumerate(tasks, start=1)]
    out.append("")

    # Gift budget + company block
    out.append("[ ค่าของแจกลูกค้า (Extra item) ]")
    if s.gift_budget_per_pax_thb is not None:
        budget = s.gift_budget_per_pax_thb
        out.append(f"- เบิกได้ {budget:g} บาท/ท่าน")
        out.append(f"- กรุ๊ปนี้ {len(nl.passengers)} ท่าน = {budget * len(nl.passengers):,.0f} บาท")
    else:
        out.append("- เบิกได้ [ระบุ] บาท/ท่าน")
    out.append("- ขอใบเสร็จ/ใบกำกับภาษี ออกในนามบริษัทตามนี้ แล้วแนบมากับเอกสารเคลียร์ค่าใช้จ่ายหลังจบทริป")
    out.append("")
    out += company.lines
    out.append("")

    # Watch-outs: each bullet comes from a finding.
    out.append("[ จุดที่ต้องระวังกรุ๊ปนี้ ]")
    out += [f"- {b}" for b in _watch_outs(by_rule, nl, pl)]
    out.append("")

    out.append("[ ขอเชิญเข้ากลุ่ม LINE ทริป ]")
    out.append(f"รบกวนคุณ{tl_call}เพิ่มผม {s.sender_line_name} เข้ากลุ่ม LINE ที่จะใช้สื่อสารระหว่างทริปด้วยนะ{k}"
               if k == "ครับ" else
               f"รบกวนคุณ{tl_call}เพิ่ม {s.sender_line_name} เข้ากลุ่ม LINE ที่จะใช้สื่อสารระหว่างทริปด้วยนะ{k}")
    out.append("จะได้ติดตามสถานการณ์หน้างานและช่วยแก้ปัญหาได้ทันที")
    out.append(f"ถ้ายังไม่ได้สร้างกลุ่ม รบกวนสร้างแล้วส่งลิงก์หรือ QR มาได้เลย{k}")
    out.append("")
    out.append("[ ระหว่างทริป ]")
    out.append("รายงานสั้น ๆ ในกลุ่มทุกวัน: ออกเดินทาง / ถึงที่พัก / ปัญหาที่พบ (ถ้ามี)")
    out.append(f"กลับถึงไทยแล้วส่งสรุปทัวร์ภายใน {s.report_days_after_trip} วันนะ{k}")
    out.append("")
    out.append(f"ขอบคุณมาก{k}")
    return "\n".join(out)


# ---------------------------------------------------------------- helpers

def _program_line(name: Optional[str], pl) -> str:
    name = (name or "").strip()
    cut = name.find(" โดยสาย")
    if cut > 0:
        name = name[:cut].strip()
    name = " ".join(name.split())
    airline = ""
    if pl and pl.airline:
        latin = " ".join(w for w in pl.airline.replace("(", " ").replace(")", " ").split() if w.isascii())
        airline = f" ({latin})" if latin else ""
    if pl and pl.duration and pl.duration not in name:
        name += f" {pl.duration}"
    return name + airline


def _city(h) -> Optional[str]:
    text = f"{h.hotel_name} {h.address or ''}".upper()
    return next((th for key, th in HOTEL_CITIES if key in text), None)


def _hotel_moves(pl) -> Optional[str]:
    if not pl or len(pl.hotels) < 2:
        return None
    cities = [_city(h) or h.hotel_name for h in pl.hotels]
    names = [h.hotel_name for h in pl.hotels]
    every_night = all(a != z for a, z in zip(names, names[1:])) and all(h.nights == 1 for h in pl.hotels)
    path = []
    for c in cities:
        if not path or path[-1] != c:
            path.append(c)
    return ("ย้ายที่พักทุกคืน " if every_night else "ที่พัก ") + " - ".join(path)


def _th_source(line: str) -> str:
    label, rest = line.split(":", 1)
    names = {"name list": "ใบรายชื่อ", "PNR": "PNR", "preparation letter": "ใบเตรียมตัว"}
    parts = rest.split()
    flight, times = parts[0], parts[-1]
    a, z = times.split("-")
    return f"{names.get(label.strip(), label.strip())} ระบุ {flight} {dot_time(a)}-{dot_time(z)}"


def _agencies_to_call(passengers: list[Passenger]) -> list[str]:
    out = []
    for p in passengers:
        if p.remark and "โทร" in p.remark and p.agency:
            agency = p.agency
            size = sum(1 for q in passengers if q.booking_no == p.booking_no)
            short = agency.split(" TRAVEL")[0].split(" TOUR")[0].split()[0]
            out.append(f"กรุ๊ป {short} {size} ท่าน")
    return out


def _watch_outs(by_rule: dict[str, list[Finding]], nl, pl) -> list[str]:
    bullets = []
    for f in by_rule.get("hotels.match", []):
        if f.verdict == Verdict.INCORRECT:
            bullets.append(f"ที่พักคืน {f.title.split(':')[0].replace('Night ', '')} ไม่ตรงกัน: "
                           + f.detail.replace("\n", " / ").replace("Preparation letter", "ใบเตรียมตัว")
                           .replace("Land confirmation", "ใบคอนเฟิร์มแลนด์").replace(" (used)", "")
                           + " ต้องเคลียร์ก่อนออกเดินทาง")
        elif f.verdict == Verdict.UNRESOLVED:
            bullets.append("ที่พักยังไม่ได้เทียบกับใบคอนเฟิร์มแลนด์ (" +
                           ", ".join(f"{th_date(h.date)} {h.hotel_name}" for h in (pl.hotels if pl else [])) + ")")
    for f in by_rule.get("passengers.extra_tickets", []):
        names = [ln.split(" (")[0] for ln in f.detail.split("\n") if "(record" in ln]
        bullets.append(f"ไฟล์ตั๋วมีชื่อที่ไม่อยู่ในรายชื่อกรุ๊ป {len(names)} ท่าน ({', '.join(names)}) "
                       "เช็กกับออฟฟิศว่าเดินทางกับกรุ๊ปนี้หรือไม่")
    for f in by_rule.get("people.minor", []):
        p = _person_from(f, nl)
        if p:
            with_ = [q for q in nl.passengers if q.booking_no == p.booking_no and q is not p]
            rel = " และ".join(person(q) for q in with_)
            bullets.append(f"{person(p)} อายุ {p.age} ปี เดินทางกับ{rel} ไม่ได้มากับพ่อแม่ "
                           "ต้องมีหนังสือยินยอมจากบิดา-มารดา + สูติบัตร เช็กให้ครบก่อนเช็กอิน")
    for f in by_rule.get("people.elderly", []):
        p = _person_from(f, nl)
        if p:
            bullets.append(f"{person(p)} อายุ {p.age} ปี รบกวนดูแลเป็นพิเศษ")
    for f in by_rule.get("people.remark", []):
        p = _person_from(f, nl)
        if p and "โทร" not in (p.remark or ""):
            text = " ".join(p.remark.split())
            extra = " ยืนยันกับโรงแรมทุกคืน" if "ห้อง" in text else ""
            bullets.append(f"{person(p)} {text}{extra}")
    for f in by_rule.get("options.price", []):
        if f.verdict == Verdict.UNRESOLVED:
            tours = (pl.optional_tours if pl else [])
            desc = ", ".join(f"{t.name} {t.price:g} หยวน" for t in tours) or "ทัวร์เสริม"
            bullets.append(f"ทัวร์เสริม ({desc}) ราคายังไม่ได้ยืนยันกับแลนด์ เช็กราคาจริงกับไกด์ก่อนเสนอลูกค้า")
    country = None
    if nl.flights:
        country = AIRPORTS.get(nl.flights[0].destination, (None, None))[0]
    bullets += COUNTRY_NOTES.get(country, [])
    return bullets


def _person_from(f: Finding, nl) -> Optional[Passenger]:
    """Findings point at the passenger's row; find that passenger again."""
    for e in f.evidence:
        for p in nl.passengers:
            if p.source.location == e.location and p.source.file == e.file:
                return p
    return None
