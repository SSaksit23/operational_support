"""Build a made-up passport page image with a valid MRZ (no real person), for the passport reader test."""
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ops_agent.parsers.passport import check_digit  # noqa: E402


def mrz(surname, given, pno, nat, dob, sex, exp):
    l1 = f"P<{nat}{surname}<<{given.replace(' ', '<')}".ljust(44, "<")[:44]
    pn = pno.ljust(9, "<")
    personal = "<" * 14
    body = pn + str(check_digit(pn)) + nat + dob + str(check_digit(dob)) + sex + exp + str(check_digit(exp)) + personal + str(check_digit(personal))
    composite = pn + str(check_digit(pn)) + dob + str(check_digit(dob)) + exp + str(check_digit(exp)) + personal + str(check_digit(personal))
    return l1, body + str(check_digit(composite))


def page(path, people):
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 30)
    small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 26)
    img = Image.new("RGB", (1300, 900 * len(people)), "#f4efe6")
    d = ImageDraw.Draw(img)
    for k, p in enumerate(people):
        y0 = 900 * k
        d.rectangle((40, y0 + 40, 330, y0 + 420), outline="#999", width=3)
        d.text((380, y0 + 60), "PASSPORT  KINGDOM OF THAILAND (SPECIMEN)", fill="#333", font=small)
        d.text((380, y0 + 120), f"Surname  {p[0]}", fill="#333", font=small)
        d.text((380, y0 + 170), f"Given name  {p[1]}", fill="#333", font=small)
        l1, l2 = mrz(*p)
        d.text((60, y0 + 700), l1, fill="#111", font=font)
        d.text((60, y0 + 760), l2, fill="#111", font=font)
    img.save(path)


if __name__ == "__main__":
    out = Path(__file__).parent / "fixtures"
    page(out / "passport-specimen.png", [("SAMPLE", "SOMCHAI", "AA1234567", "THA", "850412", "M", "310101"),
                                          ("TESTER", "MALEE NA", "AB7654321", "THA", "920230", "F", "270815")])
