"""Passport scans: read the machine-readable zone (the two lines of <<< at the bottom of the photo page).

The MRZ carries the name exactly as the airline must print it, the passport number,
date of birth, sex and expiry, each protected by a check digit. A value is used only
when its check digit agrees, so an OCR misread shows up as "unreadable" instead of
as a wrong value.

OCR runs locally with RapidOCR (pip install rapidocr_onnxruntime); nothing is sent out.
Images (jpg/png) and PDFs (each page rendered) are accepted; one file may hold many passports.
"""

from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path
from typing import Optional

from ..models import DocType, PassportScan, SourceRef

WEIGHTS = (7, 3, 1)
LINE1_RE = re.compile(r"P[A-Z0-9<]([A-Z<]{3})([A-Z<]{8,41})")
# Birth date may be partly unknown on Thai passports ("44<<<<": year only), so it allows "<".
LINE2_RE = re.compile(r"([A-Z0-9<]{9})([0-9<])([A-Z<]{3})([0-9<]{6})([0-9<])([MF<X])(\d{6})(\d)([A-Z0-9<]{14})([0-9<])(\d)")


def check_digit(s: str) -> int:
    total = 0
    for i, ch in enumerate(s):
        v = int(ch) if ch.isdigit() else (ord(ch) - 55 if ch.isalpha() else 0)
        total += v * WEIGHTS[i % 3]
    return total % 10


def _yymmdd(s: str, future: bool) -> Optional[date]:
    try:
        yy, mm, dd = int(s[:2]), int(s[2:4]), int(s[4:6])
        century = 2000 if (future or yy <= date.today().year % 100) else 1900
        return date(century + yy, mm, dd)
    except ValueError:
        return None


def _fix_digits(s: str) -> str:
    return s.translate(str.maketrans("OQDIZSB", "0001258"))


def _line2_variants(l2: str) -> list[str]:
    """OCR often drops or doubles one '<' (or a character at the cut edge): try the 44-character repairs."""
    out = [l2]
    if len(l2) in (42, 43):
        for i in range(10, len(l2) + 1):
            out.append(l2[:i] + "<" * (44 - len(l2)) + l2[i:])
    elif len(l2) in (45, 46):
        for i in range(len(l2)):
            if l2[i] == "<":
                out.append(l2[:i] + l2[i + len(l2) - 44:])
    return out


def _score(s: PassportScan) -> int:
    return (s.passport_no is not None) + (s.dob is not None) + (s.expiry is not None) + 2 * s.checks_ok


def parse_mrz(line1: str, line2: str, source: SourceRef) -> Optional[PassportScan]:
    best = None
    for l2 in _line2_variants(line2.replace(" ", "").upper().replace("«", "<")):
        s = _parse_mrz(line1, l2, source)
        if s and (best is None or _score(s) > _score(best)):
            best = s
    return best


def _parse_mrz(line1: str, line2: str, source: SourceRef) -> Optional[PassportScan]:
    l1 = line1.replace(" ", "").upper().replace("«", "<")
    l1 = l1[:5] + l1[5:].translate(str.maketrans("01582", "OISBZ"))  # names have no digits
    l2 = line2.replace(" ", "").upper().replace("«", "<")
    m1 = LINE1_RE.search(l1)
    if not m1:
        return None
    # numeric fields: OCR often reads 0 as O, 1 as I
    l2 = l2[:9] + _fix_digits(l2[9:10]) + l2[10:13] + _fix_digits(l2[13:27]) + l2[27:42] + _fix_digits(l2[42:44])
    m2 = LINE2_RE.search(l2)
    if not m2:
        return None
    # The '<' after the type letter dropped ("PTHASUMA..."): realign on the nationality from line 2.
    nat2 = m2.group(3)
    if m1.group(1) != nat2 and l1[m1.start() + 1:m1.start() + 4] == nat2:
        m1 = LINE1_RE.search(l1[:m1.start() + 1] + "<" + l1[m1.start() + 1:]) or m1
    # '<' fillers misread as K: drop a tail made only of '<' and K once it starts with '<'
    names = re.sub(r"<[<K]*$", "", m1.group(2)).rstrip("<")
    if "<<" not in names and "<" in names:
        names = names.replace("<", "<<", 1)  # the name separator read as a single '<'
    surname, _, given = names.partition("<<")
    pno, pno_cd, nat, dob, dob_cd, sex, exp, exp_cd, personal, pers_cd, final_cd = m2.groups()
    ok_no = check_digit(pno) == int(pno_cd) if pno_cd.isdigit() else False
    ok_dob = dob_cd.isdigit() and check_digit(dob) == int(dob_cd)
    ok_exp = check_digit(exp) == int(exp_cd)
    composite = pno + pno_cd + dob + dob_cd + exp + exp_cd + personal + pers_cd
    ok_all = check_digit(composite) == int(final_cd)
    if "<" in dob and ok_all:
        ok_dob = True  # an unknown birth date on the passport itself, not an OCR fault
    return PassportScan(
        surname=surname.replace("<", " ").strip(), given_name=given.replace("<", " ").strip(),
        passport_no=pno.replace("<", "") if ok_no else None, nationality=nat.replace("<", ""),
        dob=_yymmdd(dob, future=False) if ok_dob and "<" not in dob else None, sex=sex if sex in "MF" else None,
        expiry=_yymmdd(exp, future=True) if ok_exp else None, checks_ok=ok_no and ok_dob and ok_exp and ok_all,
        source=source)


def find_mrz(lines: list[str], source: SourceRef) -> list[PassportScan]:
    """Every TD3 MRZ in a list of OCR text lines (one passport per pair of lines)."""
    clean = [re.sub(r"[^A-Z0-9<«]", "", ln.upper().replace(" ", "")) for ln in lines]
    out = []
    for i in range(len(clean) - 1):
        if clean[i].startswith("P") and "<<" in clean[i] and len(clean[i]) >= 15 and len(clean[i + 1]) >= 40:
            scan = parse_mrz(clean[i], clean[i + 1], source)
            if scan:
                out.append(scan)
    return out


_ocr = None


def _engine():
    global _ocr
    if _ocr is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("Passport reading needs the OCR package: pip install rapidocr_onnxruntime") from e
        threads = int(os.environ.get("OPS_OCR_THREADS") or 0)
        if threads:
            # Leave CPU free for other requests: on Cloud Run a fully busy instance is
            # treated as overloaded and new requests are refused (429).
            from rapidocr_onnxruntime import utils as ocr_utils
            base = ocr_utils.SessionOptions

            def limited():
                o = base()
                o.intra_op_num_threads = threads
                o.inter_op_num_threads = 1
                return o
            ocr_utils.SessionOptions = limited
        _ocr = RapidOCR()
    return _ocr


def _ocr_rows(image) -> list[tuple[float, float, str]]:
    """OCR one image: (top y, bottom y, text) per row, top to bottom (boxes on the same row joined)."""
    import numpy as np
    result, _ = _engine()(np.asarray(image.convert("RGB")))
    boxes = sorted(((min(p[1] for p in b), max(p[1] for p in b), min(p[0] for p in b), t) for b, t, _ in (result or [])))
    rows: list[list] = []
    for y, y2, x, t in boxes:
        if rows and abs(rows[-1][0][0] - y) < 12:
            rows[-1].append((y, y2, x, t))
        else:
            rows.append([(y, y2, x, t)])
    return [(r[0][0], max(b[1] for b in r), "".join(b[3] for b in sorted(r, key=lambda b: b[2]))) for r in rows]


def _ocr_lines(image) -> list[str]:
    return [t for _, _, t in _ocr_rows(image)]


def _is_line2(text: str) -> bool:
    t = re.sub(r"[^A-Z0-9<]", "", text.upper())
    return len(t) >= 40 and t.count("<") >= 5 and bool(re.search(r"\d{6}", t)) and not t.startswith("P<")


MAX_PAGES = 40  # a scanned passport bundle; anything longer is not one
MAX_SIDE = 2400  # px; MRZ text stays readable, OCR time stays reasonable on one CPU
_cache: dict[tuple, list[PassportScan]] = {}


def read_passports(path: str | Path, display_name: str, fmt: str) -> list[PassportScan]:
    """Cached per file, so changing another file's type does not OCR the passports again."""
    path = Path(path)
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size, display_name, fmt)
    if key not in _cache:
        _cache[key] = _read_passports(path, display_name, fmt)
    return [s.model_copy() for s in _cache[key]]


MIN_SIDE = 1600  # small phone photos: enlarge so the MRZ letters are big enough for the detector


def _fit(img):
    w, h = img.size
    k = MAX_SIDE / max(w, h) if max(w, h) > MAX_SIDE else MIN_SIDE / max(w, h) if max(w, h) < MIN_SIDE else 1
    return img.resize((int(w * k), int(h * k))) if k != 1 else img


def _docx_pages(path: Path) -> list:
    """Scans pasted into a Word file, in the order they appear in the document."""
    import io
    import re
    import zipfile
    from PIL import Image
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        rels = z.read("word/_rels/document.xml.rels").decode("utf-8", "ignore") if "word/_rels/document.xml.rels" in names else ""
        doc = z.read("word/document.xml").decode("utf-8", "ignore") if "word/document.xml" in names else ""
        target = dict(re.findall(r'Id="([^"]+)"[^>]*Target="(media/[^"]+)"', rels))
        target.update({k: v for v, k in re.findall(r'Target="(media/[^"]+)"[^>]*Id="([^"]+)"', rels)})
        order = [target[r] for r in re.findall(r'r:embed="([^"]+)"', doc) if r in target]
        media = sorted(n[5:] for n in names if n.startswith("word/media/"))
        seen, pages = set(), []
        for m in order + media:
            if m in seen or len(pages) >= MAX_PAGES:
                continue
            seen.add(m)
            try:
                img = Image.open(io.BytesIO(z.read("word/" + m)))
                img.load()
            except Exception:  # EMF/WMF and other formats Pillow cannot open
                continue
            if min(img.size) >= 200:  # skip logos and icons
                pages.append(_fit(img))
    return pages


def _same_person(a: PassportScan, b: PassportScan) -> bool:
    if a.passport_no and a.passport_no == b.passport_no:
        return True
    from difflib import SequenceMatcher
    la, lb = (re.sub("[^A-Z]", "", x.surname + x.given_name) for x in (a, b))
    return SequenceMatcher(None, la, lb).ratio() >= 0.8


def _best_each(scans: list[PassportScan]) -> list[PassportScan]:
    """Several readings of one page: one scan per passport, the reading with the most check digits passing."""
    out: list[PassportScan] = []
    for s in scans:
        k = next((i for i, o in enumerate(out) if _same_person(o, s)), None)
        if k is None:
            out.append(s)
        elif _score(s) > _score(out[k]):
            out[k] = s
    return out


def _read_passports(path: Path, display_name: str, fmt: str) -> list[PassportScan]:
    from PIL import Image, ImageOps
    pages = []
    if fmt == "pdf":
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(str(path))
        try:
            pages = [_fit(doc[i].render(scale=2.5).to_pil()) for i in range(min(len(doc), MAX_PAGES))]
        finally:
            doc.close()
    elif fmt == "docx":
        pages = _docx_pages(path)
    else:
        pages = [_fit(Image.open(path))]
    out = []
    for n, img in enumerate(pages, start=1):
        src = SourceRef(file=display_name, doc_type=DocType.PASSPORT, location=f"page {n}" if fmt == "pdf" else f"picture {n}" if fmt == "docx" else "image")
        img = ImageOps.expand(img.convert("RGB"), border=40, fill="white")
        rows = _ocr_rows(img)
        found = find_mrz([t for *_, t in rows], src)
        # A second line read without its first line: OCR that band again, enlarged.
        w, h = img.size
        for k, (y, y2, text) in enumerate(rows):
            prev = rows[k - 1][2] if k else ""
            if _is_line2(text) and not (prev.replace(" ", "").upper().startswith("P") and "<<" in prev):
                band = img.crop((0, max(0, int(y - (y2 - y) * 3.5)), w, min(h, int(y2 + 10))))
                band = ImageOps.expand(band, border=60, fill="white")  # the text detector needs a margin
                found += [s for s in find_mrz(_ocr_lines(band), src)
                          if s.passport_no is None or s.passport_no not in {f.passport_no for f in found}]
        # A first line read without its second line, or no MRZ at all: OCR the band below and the
        # bottom of the picture again, enlarged, and keep the best reading of each passport.
        if not found or not all(s.checks_ok for s in found):
            heads = [(y, y2) for y, y2, text in rows if text.replace(" ", "").upper().startswith("P") and "<<" in text]
            bands = [(max(0, int(y - (y2 - y))), min(h, int(y2 + (y2 - y) * 4))) for y, y2 in heads] + [(int(h * 0.55), h)]
            for top, bottom in bands:
                band = img.crop((0, top, w, bottom))
                band = ImageOps.expand(band.resize((band.width * 2, band.height * 2)), border=60, fill="white")
                found += find_mrz(_ocr_lines(band), src)
        out += _best_each(found)
    return out
