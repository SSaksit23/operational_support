"""Normalisers shared by the readers: dates, times, names, Thai text."""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime

MONTHS_EN = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}

MONTHS_TH = {
    "มกราคม": 1, "กุมภาพันธ์": 2, "มีนาคม": 3, "เมษายน": 4, "พฤษภาคม": 5, "มิถุนายน": 6,
    "กรกฎาคม": 7, "สิงหาคม": 8, "กันยายน": 9, "ตุลาคม": 10, "พฤศจิกายน": 11, "ธันวาคม": 12,
    "ม.ค.": 1, "ก.พ.": 2, "มี.ค.": 3, "เม.ย.": 4, "พ.ค.": 5, "มิ.ย.": 6,
    "ก.ค.": 7, "ส.ค.": 8, "ก.ย.": 9, "ต.ค.": 10, "พ.ย.": 11, "ธ.ค.": 12,
}

WEEKDAYS_EN = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}

_TH_MONTH_RE = "|".join(re.escape(m) for m in sorted(MONTHS_TH, key=len, reverse=True))
TH_DATE_RE = re.compile(rf"(\d{{1,2}})\s*({_TH_MONTH_RE})\s*(\d{{2,4}})")


# Words where the PDF font drops SARA AM entirely ("น้ำ" comes out as "น้า").
# Only words that cannot be read the other way ("น้า" = aunt) are listed.
KNOWN_SARA_AM_LOSSES = {"แม่น้า": "แม่น้ำ", "น้าหนัก": "น้ำหนัก", "สายน้า": "สายน้ำ", "ผิวน้า": "ผิวน้ำ"}


def clean_thai(text: str, sara_am: bool = True) -> str:
    """Repair the common PDF extraction damage in Thai text.

    PDF text layers often split SARA AM into NIKHAHIT + SARA AA with a space
    ("น ากระเป๋า" for "นำกระเป๋า") and break lines before combining marks.
    """
    text = unicodedata.normalize("NFC", text)
    text = re.sub("\n([\u0e31\u0e34-\u0e3a\u0e47-\u0e4e]+)\n", r"\1", text)  # a mark alone on its line
    text = re.sub("\n(?=[\u0e31\u0e34-\u0e3a\u0e47-\u0e4e])", "", text)  # stray line break before a mark
    if not sara_am:
        # Other fonts put a stray space after an upper mark instead ("ฟ้ า", "ป้ าย"): just drop it.
        return re.sub("([\u0e31\u0e34-\u0e37\u0e47-\u0e4e]) (?=[\u0e01-\u0e4e])", r"\1", text)
    text = re.sub("(?<=[\u0e01-\u0e2e])([\u0e48-\u0e4b]?) \u0e32", "\\1\u0e33", text)  # "น า", "น้ า" -> "นำ", "น้ำ"
    text = text.replace("ํา", "ำ")
    for broken, fixed in KNOWN_SARA_AM_LOSSES.items():
        text = text.replace(broken, fixed)
    return text


def be_to_ce(year: int) -> int:
    """Buddhist-era year (2-digit or 4-digit) to Common Era."""
    if year < 100:
        year += 2500
    if year > 2400:
        year -= 543
    return year


def parse_th_date(text: str) -> date | None:
    """'24 ตุลาคม 2569', '24 ต.ค. 69' -> date(2026, 10, 24)."""
    m = TH_DATE_RE.search(text)
    if not m:
        return None
    return date(be_to_ce(int(m.group(3))), MONTHS_TH[m.group(2)], int(m.group(1)))


def parse_ddmon(text: str, ref: date) -> date | None:
    """'28OCT' (no year) -> the date closest to ref."""
    m = re.fullmatch(r"(\d{1,2})([A-Z]{3})", text.strip().upper())
    if not m:
        return None
    day, mon = int(m.group(1)), MONTHS_EN[m.group(2)]
    candidates = [date(y, mon, day) for y in (ref.year - 1, ref.year, ref.year + 1)]
    return min(candidates, key=lambda d: abs((d - ref).days))


def parse_dowddmon(text: str, ref: date) -> date | None:
    """'SA24OCT' -> the date near ref whose weekday matches."""
    m = re.fullmatch(r"([A-Z]{2})(\d{1,2})([A-Z]{3})", text.strip().upper())
    if not m:
        return None
    wd, day, mon = WEEKDAYS_EN[m.group(1)], int(m.group(2)), MONTHS_EN[m.group(3)]
    for y in sorted((ref.year, ref.year + 1, ref.year - 1), key=lambda y: abs(y - ref.year)):
        d = date(y, mon, day)
        if d.weekday() == wd:
            return d
    return parse_ddmon(f"{day}{m.group(3)}", ref)


def parse_ddmonyy(text: str) -> date | None:
    """'23Oct26' -> date(2026, 10, 23)."""
    m = re.fullmatch(r"(\d{1,2})([A-Za-z]{3})(\d{2})", text.strip())
    if not m:
        return None
    return date(2000 + int(m.group(3)), MONTHS_EN[m.group(2).upper()], int(m.group(1)))


def to_date(value) -> date | None:
    """Spreadsheet cell (datetime, 'YYYY-MM-DD', 'DD/MM/YYYY') -> date."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def hhmm(text: str) -> str | None:
    """'0805', '08.05', '8:05 น.' -> '08:05'."""
    m = re.search(r"(\d{1,2})[:.]?(\d{2})", text)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    return f"{h:02d}:{mi:02d}"


def airline_name(surname: str, given: str) -> str:
    return f"{surname.strip()}/{given.strip()}".upper()


def norm_phone(text: str | None) -> str | None:
    if not text:
        return None
    s = str(text).strip()
    return s or None


def norm_id(value) -> str | None:
    """Citizen IDs arrive as ints from Excel; keep them as 13-char strings."""
    if value is None or value == "":
        return None
    s = re.sub(r"\D", "", str(value))
    return s or None
