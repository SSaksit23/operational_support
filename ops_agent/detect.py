"""Work out what kind of document an upload is from its content, not its name.

Uploads often arrive with names like "E-TICKET ขากลับ.pdf" or an id with no
extension, so detection opens the file and looks for markers.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

from .models import DocType


def sniff_format(path: Path) -> str:
    head = path.read_bytes()[:8]
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"\xff\xd8") or head.startswith(b"\x89PNG"):
        return "image"
    if head.startswith(b"PK"):
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
        if any(n.startswith("word/") for n in names):
            return "docx"
        if any(n.startswith("xl/") for n in names):
            return "xlsx"
    return "text"


def detect(path: str | Path) -> tuple[DocType, str]:
    path = Path(path)
    fmt = sniff_format(path)
    if fmt == "image":
        return DocType.PASSPORT, fmt  # photos are passport scans in this workflow
    if fmt == "pdf":
        from .parsers.eticket import looks_like_eticket, page_texts
        first = page_texts(path)[0]
        if not first.strip():
            return DocType.PASSPORT, fmt  # no text layer: a scan, most often the passports
        if looks_like_eticket(first):
            return DocType.ETICKET, fmt
        from .parsers.prep_letter import looks_like_prep_letter
        from .normalize import clean_thai
        if looks_like_prep_letter(clean_thai(first)):
            return DocType.PREP_LETTER, fmt
        if "确认" in first or "地接" in first:
            return DocType.LAND_CONFIRMATION, fmt
        return DocType.UNKNOWN, fmt
    if fmt == "xlsx":
        import openpyxl
        with open(path, "rb") as fh:
            wb = openpyxl.load_workbook(fh)
        names = [ws.title for ws in wb.worksheets]
        text = " ".join(str(v) for ws in wb.worksheets[:1] for r in ws.iter_rows(max_row=30, values_only=True)
                        for v in r if v is not None)
        if any("เบิก" in n for n in names) or "ใบจ่ายเงิน" in text:
            return DocType.EXPENSE_TEMPLATE, fmt
        if "passport" in text.lower() and "No." in text:
            return DocType.NAME_LIST, fmt
        if "确认" in text or "地接" in text:
            return DocType.LAND_CONFIRMATION, fmt
        return DocType.UNKNOWN, fmt
    if fmt in ("docx", "text"):
        from .parsers.pnr import looks_like_pnr, read_lines
        lines, _ = read_lines(path, fmt)
        if looks_like_pnr(lines):
            return DocType.PNR, fmt
        joined = "\n".join(lines)
        from .parsers.prep_letter import looks_like_prep_letter
        from .normalize import clean_thai
        if looks_like_prep_letter(clean_thai(joined[:3000])):
            return DocType.PREP_LETTER, fmt
        if "确认" in joined or "地接" in joined or "导游" in joined:
            return DocType.LAND_CONFIRMATION, fmt
        if fmt == "docx" and len(joined.strip()) < 300 and _docx_images(path):
            return DocType.PASSPORT, fmt  # a Word file of pasted scans: the passport bundle
    return DocType.UNKNOWN, fmt


def _docx_images(path: Path) -> int:
    with zipfile.ZipFile(path) as z:
        return sum(n.startswith("word/media/") for n in z.namelist())


# "PASS TTCKG…", "รวมพาส 2UCKG…", "Passport scan.pdf"
PASSPORT_NAME = re.compile(r"(?<![A-Z])PASS(?:PORT)?(?![A-Z])|พาส|หนังสือเดินทาง", re.I)


def detect_named(path: str | Path, display_name: str) -> tuple[DocType, str]:
    """detect(), plus the office's naming rule: a file whose name starts with "CFM" is the land confirmation."""
    dtype, fmt = detect(path)
    upper = Path(display_name).name.upper()
    if fmt in ("pdf", "image", "docx") and dtype in (DocType.UNKNOWN, DocType.PASSPORT) and PASSPORT_NAME.search(upper):
        return DocType.PASSPORT, fmt
    if Path(display_name).name.upper().startswith("CFM") and dtype not in (DocType.NAME_LIST, DocType.EXPENSE_TEMPLATE):
        return DocType.LAND_CONFIRMATION, fmt
    return dtype, fmt
