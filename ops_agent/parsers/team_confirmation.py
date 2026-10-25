"""Rule-based reader for the Chinese ground handler's group confirmation (团队确认件).

The layout is the common one Chinese land operators send as PDF:
a header block (TO / FROM / 贵社团号 / 抵离日期 / 人数 / 我社团号 / 导游 / 领队 ...),
a day table (date, weekday, 早/午/晚 meals, hotel + phone), and a free-text
itinerary with GDS flight lines above it. Every value keeps the line it came from.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Optional

from ..models import Contact, DocType, FlightLeg, HotelNight, LandConfirmationDoc, Meals, SourceRef
from ..normalize import hhmm, parse_dowddmon
from .pnr import SEG_RE

MARKERS = ("团队确认件", "团队确认单", "地接确认", "行程确认")
FLIGHT_RE = re.compile(SEG_RE.pattern.lstrip("^").replace(r"\s*(\d{1,3})\.", r"(\d{1,3})\."))
PHONE_RE = re.compile(r"(0\d{2,3}-?\s*\d{7,8}|1[3-9]\d{9})")
DATE_RANGE_RE = re.compile(r"抵离日期[:：]\s*(\d{4}-\d{2}-\d{2})\s*-+\s*(\d{4}-\d{2}-\d{2})")
PAX_RE = re.compile(r"人\s*数[:：]\s*(\d+)\s*大(?:\s*(\d+)\s*小)?(?:\s*(\d+)\s*陪)?")
DAY_RE = re.compile(r"^(\d{1,2})-(\d{1,2})$")
MEAL_RE = re.compile(r"^(早|午|晚)[:：](.*)$")
ROOMS_RE = re.compile(r"(\d+)\s*(标准间|大床房|双床房|单人间|三人间|家庭房)")
TOP_CONTACT_RE = re.compile(r"^([一-鿿]{2,4})\s*(1[3-9]\d{9})\s*(?:\((.*?)\))?")


def looks_like_team_confirmation(text: str) -> bool:
    return any(m in text for m in MARKERS) and ("抵离日期" in text or "我社团号" in text)


LABELS = ("贵社团号", "抵离日期", "人 数", "人数", "我社团号", "导 游", "导游", "领 队", "领队", "收件人", "发件人",
          "电话", "传真", "客人类型", "客 源地", "客源地", "结算币种")
NEXT_LABEL = r"(?=\s*(?:" + "|".join(map(re.escape, LABELS)) + r")\s*[:：]|$)"


def _after(label: str, line: str) -> Optional[str]:
    """Value after 'label:' up to the next known label on the same line."""
    m = re.search(re.escape(label) + r"\s*[:：]\s*(.*?)" + NEXT_LABEL, line)
    v = m.group(1).strip() if m else None
    return v or None


def parse_team_confirmation(text: str, fname: str, page_of_line=None) -> Optional[LandConfirmationDoc]:
    if not looks_like_team_confirmation(text):
        return None
    lines = [ln.strip() for ln in text.replace("\r", "").split("\n")]
    where = page_of_line or (lambda i: f"line {i + 1}")

    def ref(i: int, raw: Optional[str] = None) -> SourceRef:
        return SourceRef(file=fname, doc_type=DocType.LAND_CONFIRMATION, location=where(i), raw=raw or lines[i])

    doc = LandConfirmationDoc(source=SourceRef(file=fname, doc_type=DocType.LAND_CONFIRMATION, location="whole document"))
    joined = "\n".join(lines)

    m = DATE_RANGE_RE.search(joined)
    if m:
        doc.start_date, doc.end_date = date.fromisoformat(m.group(1)), date.fromisoformat(m.group(2))
    ref_date = doc.start_date or date.today()

    for i, ln in enumerate(lines):
        if not ln:
            continue
        if ln.startswith("FROM") and doc.land_operator is None:
            name = re.sub(r"^FROM\s*[:：]\s*", "", ln).split("发件人")[0].strip()
            phone = _after("电话", ln.split("发件人")[-1]) if "发件人" in ln else None
            doc.land_operator = Contact(role="land_operator", name=name, nickname=_after("发件人", ln),
                                        phone=phone if phone and PHONE_RE.search(phone) else None, source=ref(i))
        elif "我社团号" in ln:
            doc.tour_code = _after("我社团号", ln)
            guide = _after("导 游", ln) or _after("导游", ln)
            if guide:
                ph = PHONE_RE.search(guide)
                doc.guides.append(Contact(role="guide", name=PHONE_RE.sub("", guide).strip(),
                                          phone=ph.group(1) if ph else None, source=ref(i)))
            leader = _after("领 队", ln) or _after("领队", ln)
            if not leader and i + 1 < len(lines) and re.fullmatch(r"[A-Z][A-Z \-/￾]+", lines[i + 1]):
                leader = lines[i + 1]
            if leader:
                doc.tour_leader = re.sub(r"[￾­]", "", leader).strip()
        elif "贵社团号" in ln:
            doc.agent_tour_code = _after("贵社团号", ln)
        if "人 数" in ln or "人数" in ln:
            pm = PAX_RE.search(ln)
            if pm:
                doc.adults = int(pm.group(1)) + int(pm.group(2) or 0)
                doc.escorts = int(pm.group(3) or 0)
                doc.pax_total = doc.adults + doc.escorts
        if ln.startswith("用 餐") or ln.startswith("用餐"):
            doc.meal_plan = re.sub(r"^用\s*餐\s*[:：]\s*", "", ln) or None
        if ln.startswith("用房标准"):
            doc.hotel_standard = re.sub(r"^用房标准\s*[:：]\s*", "", ln) or None
        if ln.startswith("旅游用车"):
            rest = re.sub(r"^旅游用车\s*[:：]\s*", "", ln)
            j = i + 1
            while not rest and j < len(lines) and lines[j]:
                rest = lines[j]; j += 1  # noqa: E702
            # the coach description can wrap onto the next short line ("…四排" / "椅")
            if j < len(lines) and len(lines[j]) <= 2 and not lines[j].startswith("特"):
                rest += lines[j]
            doc.coach = rest or None
        if ln.startswith("确认人") or ln.startswith("日 期") and "日 期" in ln:
            dm = re.search(r"(\d{4}-\d{2}-\d{2})", ln)
            if dm and ln.startswith("日 期"):
                doc.confirmed_on = date.fromisoformat(dm.group(1))
        rm = ROOMS_RE.findall(ln)
        if rm and not doc.rooms:
            doc.rooms = {k: int(n) for n, k in rm}
        fm = FLIGHT_RE.search(ln)
        if fm:
            doc.flights.append(FlightLeg(
                flight_no=fm.group(2) + fm.group(3), date=parse_dowddmon(fm.group(5), ref_date),
                origin=fm.group(6), destination=fm.group(7), status=fm.group(8),
                dep_time=hhmm(fm.group(9)), arr_time=hhmm(fm.group(10)), source=ref(i)))
        if i < 3:
            cm = TOP_CONTACT_RE.match(ln)
            if cm:
                doc.other_contacts.append(Contact(role="unlabelled", name=cm.group(1), phone=cm.group(2), source=ref(i)))
                if not doc.route and i + 1 < len(lines):
                    doc.route = lines[i + 1] or None

    _day_table(lines, doc, ref)
    start = next((i for i, ln in enumerate(lines) if FLIGHT_RE.search(ln)), None)
    end = next((i for i, ln in enumerate(lines) if any(m in ln for m in MARKERS)), len(lines))
    if start is not None:
        doc.itinerary = [ln for ln in lines[start:end] if ln and not FLIGHT_RE.search(ln)]
    return doc


def _day_table(lines: list[str], doc: LandConfirmationDoc, ref) -> None:
    """Rows: MM-DD, 周X, 早/午/晚 lines (values may wrap), then hotel name + phone lines."""
    year = (doc.start_date or date.today()).year
    starts = [i for i, ln in enumerate(lines) if DAY_RE.match(ln)]
    if not starts:
        return
    stop = len(lines)
    for i in range(starts[-1] + 1, len(lines)):
        if doc.land_operator and lines[i] == doc.land_operator.name or lines[i].startswith("请签字"):
            stop = i
            break
    bounds = starts[1:] + [stop]
    for s, e in zip(starts, bounds):
        mm, dd = map(int, DAY_RE.match(lines[s]).groups())
        d = date(year + (1 if doc.start_date and mm < doc.start_date.month else 0), mm, dd)
        meals: dict[str, str] = {}
        last = None
        rest: list[int] = []
        for j in range(s + 1, e):
            ln = lines[j]
            if not ln or re.fullmatch(r"周[一二三四五六日天]", ln):
                continue
            mm_ = MEAL_RE.match(ln)
            if mm_:
                last = {"早": "breakfast", "午": "lunch", "晚": "dinner"}[mm_.group(1)]
                meals[last] = mm_.group(2).strip()
            elif last == "dinner" or last is None:
                rest.append(j)
            elif re.fullmatch(r"\d+", ln):
                meals[last] += ln  # wrapped price, e.g. "肉夹馍风味" / "50"
            else:
                rest.append(j)
                last = "dinner"
        doc.meals.append(Meals(date=d, source=ref(s, " ".join(lines[s:e]).strip()),
                               **{k: (v if v not in ("/", "") else None) for k, v in meals.items()}))
        hotel_text = "".join(lines[j] for j in rest)
        if not hotel_text:
            continue
        ph = PHONE_RE.search(hotel_text)
        name = hotel_text[:ph.start()] if ph else hotel_text
        name = name.rstrip("-（( ")
        if name:
            doc.hotels.append(HotelNight(date=d, hotel_name=name, phone=ph.group(1).replace(" ", "") if ph else None,
                                         source=ref(rest[0], " ".join(lines[j] for j in rest))))
