"""Reader for land confirmations in layouts without a dedicated reader (any file named "CFM ...").

Chinese land operators each use their own layout, often mixing Chinese, Thai and
English. This reader looks for patterns that hold across them:

  header   operator name (…旅行社 / Travel Service), contact lines (经理 / OP / 联系人 + mobile),
           Tel / WeChat, head count ("12+1", "22大1陪"), rooms ("03TWN+02TRP+1T/L", "7标准间"),
           trip dates ("2026.10.26-10.30", or the file name "CFM 20261026-1030 …"), guide label
  days     "D1" + "10.26", "10-26", "10月26日", "第1天"; each day's hotel (Hotel name lines,
           "Hotel Phone:", Chinese hotel name), meals (B / L50 / D60), optional tours with prices
  flights  GDS lines ("1. CZ8158 G FR30OCT BKKSZX HK25 0240 0700") and plain ones
           ("PN6430 BKK-CKG 1755-2210", "曼谷 ✈重庆 PN6430 19:35-23:40")

Everything keeps the line it came from, and the checks ask the officer to confirm
values read this way.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Optional

from ..models import Contact, DocType, FlightLeg, HotelNight, LandConfirmationDoc, Meals, OptionalTour, SourceRef
from ..normalize import clean_thai, hhmm, parse_dowddmon
from .team_confirmation import FLIGHT_RE, ROOMS_RE

NAME_DATES_RE = re.compile(r"(20\d{2})(\d{2})(\d{2})\s*-\s*(?:(\d{2})(\d{2})|(\d{2}))(?!\d)")
TEXT_RANGE_RE = re.compile(r"(20\d{2})[.\-/年](\d{1,2})[.\-/月](\d{1,2})日?\s*[-–~至]+\s*(?:(20\d{2})[.\-/年])?(\d{1,2})[.\-/月](\d{1,2})")
ISO_RE = re.compile(r"(20\d{2})[-./年](\d{1,2})[-./月](\d{1,2})")
PAX_RE = re.compile(r"(?<![\d.])(\d{1,3})\s*\+\s*(\d)(?![\d.])")
PAX_CN_RE = re.compile(r"(\d+)\s*大(?!床)(?:\s*(\d+)\s*小)?(?:\s*(\d+)\s*陪)?")
ROOMS_EN_RE = re.compile(r"(\d{1,2})\s*(SGL|SNG|TWN|DBL|TRP|TPL|QUAD|T/L)\b", re.I)
DAY_D_RE = re.compile(r"^(?:D|DAY)\s*(\d{1,2})\b", re.I)
DATE_LINE_RE = re.compile(r"^(\d{1,2})[./\-月](\d{1,2})日?(?:\s|$)")
DAY_CN_RE = re.compile(r"^第\s*([一二三四五六七八九十\d]{1,3})\s*天")
MEAL_RE = re.compile(r"^([BLD])(\d{0,3})$")
PHONE_RE = re.compile(r"(0\d{2,3}-?\s?\d{7,8}|1[3-9]\d{9})")
TEL_RE = re.compile(r"(?:Tel|TEL|电话|座机)\s*[:：]\s*(0\d{2,3}-?\d{7,8})")
WECHAT_RE = re.compile(r"(?:WECHAT|WeChat|微信)(?:/QQ)?\s*[:：]\s*([A-Za-z0-9_-]{5,})")
CONTACT_RE = re.compile(r"(经理|总经理|OP|联系人|负责人|计调|操作|销售|Manager|MANAGER)\s*[:：]\s*([^\d:：]{1,20}?)\s*(1[3-9]\d{9})")
OPERATOR_RE = re.compile(r"([一-鿿]{2,}(?:旅行社|国旅|旅游)(?:有限公司|有限责任公司)?)")
GUIDE_RE = re.compile(r"(?:导\s*游|地\s*陪|全\s*陪|ไกด์|GUIDE|Guide)\s*[:：]?\s*(?:คุณ)?\s*([一-鿿]{2,4}|[฀-๿]{2,}|[A-Z][a-z]+)?\s*(1[3-9]\d{9})?")
LATIN_HOTEL_RE = re.compile(r"\b(HOTEL|Hotel|RESORT|Resort|INN|Inn)\b")
CN_HOTEL_RE = re.compile(r"([一-鿿（）()A-Za-z0-9]{2,30}(?:酒店|宾馆|饭店)(?:[（(][^)）]{1,12}[)）])?)")
NOT_A_HOTEL = ("入住", "接机", "送机", "返回", "前往", "早餐", "Address", "Phone")
OPTION_RE = re.compile(r"(OPTIONAL TOUR|OPTION\s*\d*|自费\s*\d*|ออฟชั่น)\s*[:：]?\s*(.*)", re.I)
PRICE_RE = re.compile(r"(\d{2,5})\s*(RMB|CNY|元|หยวน|THB|บาท)", re.I)
CITY_IATA = {"曼谷": "BKK", "重庆": "CKG", "成都": "TFU", "西安": "XIY", "昆明": "KMG", "广州": "CAN", "深圳": "SZX",
             "上海": "PVG", "北京": "PEK", "郑州": "CGO", "长沙": "CSX", "张家界": "DYG", "桂林": "KWL", "南宁": "NNG",
             "贵阳": "KWE", "杭州": "HGH", "南京": "NKG", "武汉": "WUH", "厦门": "XMN", "青岛": "TAO", "大连": "DLC"}
PLAIN_FLIGHT_RE = re.compile(r"\b([A-Z][A-Z0-9]|[0-9][A-Z])\s?(\d{3,4})\s+(?:([A-Z]{3})\s*[-–]\s*([A-Z]{3})\s+)?"
                             r"(\d{1,2}:?\d{2})\s*[-–]\s*(\d{1,2}:?\d{2})")
CN_NUM = {c: i for i, c in enumerate("一二三四五六七八九十", start=1)}


def _cn_int(s: str) -> int:
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    if s.startswith("十"):
        return 10 + CN_NUM.get(s[1:], 0)
    if "十" in s:
        a, b = s.split("十")
        return CN_NUM.get(a, 0) * 10 + CN_NUM.get(b, 0)
    return CN_NUM.get(s, 0)


def dates_from_name(name: str) -> tuple[Optional[date], Optional[date]]:
    m = NAME_DATES_RE.search(name)
    if not m:
        return None, None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        start = date(y, mo, d)
        if m.group(4):
            end = date(y + (1 if int(m.group(4)) < mo else 0), int(m.group(4)), int(m.group(5)))
        else:  # "20261026-30": same month
            end = date(y, mo, int(m.group(6)))
    except ValueError:
        return None, None
    return start, end


def _route(line: str) -> tuple[Optional[str], Optional[str]]:
    cities = [(line.find(c), code) for c, code in CITY_IATA.items() if c in line]
    cities.sort()
    return (cities[0][1], cities[1][1]) if len(cities) >= 2 else (None, None)


def parse_general_confirmation(text: str, fname: str, start_hint: Optional[date] = None,
                               page_of_line=None) -> LandConfirmationDoc:
    lines = [ln.strip() for ln in clean_thai(text.replace("\r", ""), sara_am=False).split("\n")]
    where = page_of_line or (lambda i: f"line {i + 1}")

    def ref(i, raw=None):
        return SourceRef(file=fname, doc_type=DocType.LAND_CONFIRMATION, location=where(i), raw=raw or lines[i])

    doc = LandConfirmationDoc(layout="general", source=SourceRef(
        file=fname, doc_type=DocType.LAND_CONFIRMATION, location="whole document (general rules)"))
    joined = "\n".join(lines)

    # ---- dates
    doc.start_date, doc.end_date = dates_from_name(fname)
    if not doc.start_date:
        m = TEXT_RANGE_RE.search(joined)
        if m:
            y = int(m.group(1))
            try:
                doc.start_date = date(y, int(m.group(2)), int(m.group(3)))
                ey = int(m.group(4)) if m.group(4) else y + (1 if int(m.group(5)) < int(m.group(2)) else 0)
                doc.end_date = date(ey, int(m.group(5)), int(m.group(6)))
            except ValueError:
                doc.start_date = doc.end_date = None
    start = doc.start_date or start_hint
    trip_days = (doc.end_date - doc.start_date).days + 1 if doc.start_date and doc.end_date else 15

    # ---- head count and rooms
    m = PAX_CN_RE.search(joined)
    if m:
        doc.adults, doc.escorts = int(m.group(1)) + int(m.group(2) or 0), int(m.group(3) or 0)
    else:
        m = PAX_RE.search(fname) or PAX_RE.search(joined)
        if m:
            doc.adults, doc.escorts = int(m.group(1)), int(m.group(2))
    if doc.adults is not None:
        doc.pax_total = doc.adults + (doc.escorts or 0)
    for i, ln in enumerate(lines):
        en = ROOMS_EN_RE.findall(ln)
        if len(en) >= 1 and ("+" in ln or len(en) > 1):
            doc.rooms = {k.upper().replace("SNG", "SGL").replace("TPL", "TRP"): int(n) for n, k in en}
            break
        cn = ROOMS_RE.findall(ln)
        if cn:
            doc.rooms = {k: int(n) for n, k in cn}
            break

    # ---- day blocks
    day_of: list[Optional[date]] = [None] * len(lines)
    current: Optional[date] = None
    first_day_line = None
    for i, ln in enumerate(lines):
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        d_m, cn_m, dl_m = DAY_D_RE.match(ln), DAY_CN_RE.match(ln), DATE_LINE_RE.match(ln)
        new = None
        if d_m and start and (DATE_LINE_RE.match(nxt) or len(ln) > 3 or int(d_m.group(1)) <= trip_days) \
                and not MEAL_RE.match(ln.replace(" ", "")) or (d_m and DATE_LINE_RE.match(nxt)):
            n = int(d_m.group(1))
            if start and 1 <= n <= trip_days:
                new = start + timedelta(days=n - 1)
        elif cn_m and start:
            n = _cn_int(cn_m.group(1))
            if 1 <= n <= trip_days:
                new = start + timedelta(days=n - 1)
        elif dl_m and start:
            mo, dd = int(dl_m.group(1)), int(dl_m.group(2))
            if 1 <= mo <= 12 and 1 <= dd <= 31:
                cand = date(start.year + (1 if mo < start.month else 0), mo, dd)
                if 0 <= (cand - start).days < trip_days:
                    new = cand
        if new:
            current = new
            if first_day_line is None:
                first_day_line = i
        day_of[i] = current
    header_end = first_day_line if first_day_line is not None else len(lines)

    # ---- header: operator, contacts, guide
    for i, ln in enumerate(lines[:header_end]):
        om = OPERATOR_RE.search(ln)
        if om and doc.land_operator is None:
            doc.land_operator = Contact(role="land_operator", name=om.group(1), source=ref(i))
        for cm in CONTACT_RE.finditer(ln):
            doc.other_contacts.append(Contact(role=cm.group(1), name=cm.group(2).strip(" ,/"), phone=cm.group(3), source=ref(i)))
        tm = TEL_RE.search(ln)
        if tm and doc.land_operator and not doc.land_operator.phone:
            doc.land_operator.phone = tm.group(1)
        wm = WECHAT_RE.search(ln)
        if wm and doc.land_operator and not doc.land_operator.wechat:
            doc.land_operator.wechat = wm.group(1)
    if doc.land_operator is None:
        for i, ln in enumerate(lines[:header_end]):
            if re.search(r"Travel|TRAVEL|旅行社", ln):
                doc.land_operator = Contact(role="land_operator", name=ln.strip(), source=ref(i))
                break
    for i, ln in enumerate(lines):
        gm = GUIDE_RE.search(ln)
        if not gm:
            continue
        if gm.group(1) or gm.group(2):
            name = gm.group(1) or "?"
            if name not in ("ท้องถิ่น",):
                doc.guides.append(Contact(role="guide", name=name, phone=gm.group(2), source=ref(i)))
        elif ln[gm.end():].strip() == "" and i < header_end:
            doc.notes.append(ref(i, f"guide name left blank: {ln}"))

    # ---- flights
    seen = set()
    route_of: dict[str, tuple[str, str]] = {}
    for i, ln in enumerate(lines):
        fm = FLIGHT_RE.search(ln)
        if fm:
            leg = FlightLeg(flight_no=fm.group(2) + fm.group(3), date=parse_dowddmon(fm.group(5), start or date.today()),
                            origin=fm.group(6), destination=fm.group(7), status=fm.group(8),
                            dep_time=hhmm(fm.group(9)), arr_time=hhmm(fm.group(10)), source=ref(i))
            doc.flights.append(leg)
            route_of[leg.flight_no] = (leg.origin, leg.destination)
            continue
        for pm in PLAIN_FLIGHT_RE.finditer(ln):
            fno = pm.group(1) + pm.group(2)
            o, d = pm.group(3), pm.group(4)
            if not o:
                o, d = _route(ln)
            if not o and fno in route_of:
                o, d = route_of[fno]
            when = day_of[i]
            if when is None and doc.start_date:
                when = doc.start_date if o == "BKK" else doc.end_date if d == "BKK" else None
            key = (fno, when, hhmm(pm.group(5)), hhmm(pm.group(6)), o, d)
            if key in seen:
                continue
            seen.add(key)
            leg = FlightLeg(flight_no=fno, date=when, origin=o, destination=d, dep_time=hhmm(pm.group(5)),
                            arr_time=hhmm(pm.group(6)), source=ref(i, pm.group(0)))
            doc.flights.append(leg)
            if o:
                route_of.setdefault(fno, (o, d))

    # ---- hotels, meals, optional tours per day
    meals: dict[date, dict] = {}
    seen_option: dict[int, date] = {}
    for i, ln in enumerate(lines):
        d = day_of[i]
        if d is None or i < header_end:
            continue
        mm = MEAL_RE.match(ln.replace(" ", ""))
        if mm and not (mm.group(1) == "D" and DATE_LINE_RE.match(lines[i + 1] if i + 1 < len(lines) else "")):
            key = {"B": "breakfast", "L": "lunch", "D": "dinner"}[mm.group(1)]
            meals.setdefault(d, {})[key] = (mm.group(2) and f"{mm.group(2)} RMB") or "รวม"
            continue
        if any(h.date == d for h in doc.hotels):
            hotel_today = True
        else:
            hotel_today = False
        ln = re.sub(r"^(入住|住宿|住|酒店|Hotel)\s*[:：]\s*|^入住\s+", "", ln)
        is_latin = bool(LATIN_HOTEL_RE.search(ln)) and not any(w in ln for w in NOT_A_HOTEL)
        cn = CN_HOTEL_RE.search(ln)
        is_cn = bool(cn) and not re.search(r"(入住|接机|送机|返回|前往|早餐)\S{0,3}(酒店|宾馆|饭店)", cn.group(1))
        if (is_latin or is_cn) and not hotel_today:
            first = cn.group(1) if is_cn and not is_latin else ln
            same_line_phone = PHONE_RE.search(ln[cn.end():]) if is_cn and not is_latin else None
            name_parts, addr, cn_name = [first], [], None
            phone = same_line_phone.group(1).replace(" ", "") if same_line_phone else None
            j = i + 1
            while j < len(lines) and j < i + 8 and day_of[j] == d and not DAY_D_RE.match(lines[j]) \
                    and not OPTION_RE.match(lines[j]):
                nx = lines[j]
                if re.match(r"(Hotel\s*)?(Address|地址)\s*[:：]", nx, re.I):
                    addr.append(re.sub(r"^(Hotel\s*)?(Address|地址)\s*[:：]\s*", "", nx, flags=re.I))
                elif re.match(r"(Hotel\s*)?(Phone|Tel|电话)\s*[:：]", nx, re.I) or PHONE_RE.search(nx):
                    ph = PHONE_RE.search(nx)
                    phone = phone or (ph.group(1).replace(" ", "") if ph else None)
                    tail = nx[ph.end():].strip() if ph else ""
                    if tail and not cn_name:
                        cn_name = tail + (lines[j + 1] if j + 1 < len(lines) and "酒店" in lines[j + 1] else "")
                elif addr and not phone:
                    addr.append(nx)
                elif not addr and not phone and len(nx) < 40 and not MEAL_RE.match(nx) and nx != "---":
                    name_parts.append(nx)
                else:
                    break
                j += 1
            name = " ".join(" ".join(name_parts).split())
            name = re.sub(r"\(\s+", "(", re.sub(r"\s+\)", ")", name))
            if cn_name and cn_name not in name:
                name = f"{name} {cn_name}"
            doc.hotels.append(HotelNight(date=d, hotel_name=name, phone=phone, address=" ".join(addr) or None,
                                         source=ref(i, " / ".join(lines[i:j]))))
            continue
        om = OPTION_RE.match(ln)
        if om:
            window = " ".join(lines[i:i + 4])
            pm = PRICE_RE.search(window)
            if pm:
                price = float(pm.group(1))
                unit = pm.group(2)
                cur = {"元": "CNY", "หยวน": "CNY", "บาท": "THB"}.get(unit, unit.upper().replace("RMB", "CNY"))
                # the same tour is often written twice (Thai/English, then Chinese) on the same day
                if not any(o.price == price and seen_option.get(id(o)) == d for o in doc.optional_tours):
                    name = window[len(om.group(1)):pm.start()].strip(" :：")
                    tour = OptionalTour(name=" ".join(name.split()) or om.group(1), price=price, currency=cur,
                                        source=ref(i, window[:pm.end()]))
                    doc.optional_tours.append(tour)
                    seen_option[id(tour)] = d
    for d, m in sorted(meals.items()):
        doc.meals.append(Meals(date=d, source=doc.source, **m))
    if doc.end_date:
        doc.hotels = [h for h in doc.hotels if h.date < doc.end_date]
    return doc
