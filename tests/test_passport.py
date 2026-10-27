"""Passport scans: MRZ reading and the check against the name list."""
from datetime import date
from pathlib import Path

import pytest

from ops_agent.models import DocType, GroupBundle, NameListDoc, Passenger, PassportScan, SourceRef
from ops_agent.parsers.passport import check_digit, parse_mrz
from ops_agent.verify import Settings, Verdict, check_passport_scans

FIX = Path(__file__).parent / "fixtures" / "passport-specimen.png"


def _line2(no, dob, sex, exp):
    body = f"{no}{check_digit(no)}THA{dob}{check_digit(dob)}{sex}{exp}{check_digit(exp)}" + "<" * 14
    body += str(check_digit("<" * 14))
    comp = body[0:10] + body[13:20] + body[21:43]
    return body + str(check_digit(comp))


def _src(f, dt=DocType.PASSPORT):
    return SourceRef(file=f, doc_type=dt, location="page 1")


L1 = "P<THASAMPLE<<SOMCHAI<<<<<<<<<<<<<<<<<<<<<<<<"
L2 = _line2("AA1234567", "850412", "M", "310101")


def test_check_digit():
    # ICAO 9303 specimen values
    assert check_digit("L898902C3") == 6
    assert check_digit("740812") == 2
    assert check_digit("120415") == 9


def test_parse_mrz_line_pair():
    sc = parse_mrz(L1, L2, _src("p.png"))
    assert (sc.surname, sc.given_name, sc.passport_no) == ("SAMPLE", "SOMCHAI", "AA1234567")
    assert sc.dob == date(1985, 4, 12) and sc.expiry == date(2031, 1, 1) and sc.sex == "M"


def _bundle(scans):
    src = _src("NAMELIST.xlsx", DocType.NAME_LIST)
    pax = [Passenger(given_name="SOMCHAI", surname="SAMPLE", passport_no="AA1234567", dob=date(1985, 4, 12), source=src),
           Passenger(given_name="MALI", surname="TESTER", passport_no="AB7654321", source=src),
           Passenger(given_name="NOSCAN", surname="PERSON", passport_no="AC0000001", source=src)]
    nl = NameListDoc(tour_code="T1", passengers=pax, source=src)
    return GroupBundle(name_list=nl, passports=scans)


def test_scan_vs_name_list():
    l1b = "P<THATESTER<<MALEE<<<<<<<<<<<<<<<<<<<<<<<<<<"
    s1 = parse_mrz(L1, L2, _src("pp1.png"))
    s2 = parse_mrz(l1b, _line2("AB7654321", "900230", "F", "270815"), _src("pp2.png"))
    out = check_passport_scans(_bundle([s1, s2]), Settings())
    bad = [f for f in out if f.verdict == Verdict.INCORRECT]
    assert len(bad) == 1 and "name" in bad[0].title and bad[0].compare[0].value.startswith("TESTER/MALEE")
    assert any(f.rule == "passports.missing" and "PERSON/NOSCAN" in f.detail for f in out)
    assert any(f.verdict == Verdict.VERIFIED for f in out)


def test_ocr_reads_specimen_image():
    pytest.importorskip("rapidocr_onnxruntime")
    from ops_agent.parsers.passport import read_passports
    scans = read_passports(FIX, "passport-specimen.png", "image")
    nos = {s.passport_no for s in scans}
    assert {"AA1234567", "AB7654321"} <= nos


def test_internal_rows():
    from datetime import date as d
    from ops_agent.internal_check import build_rows
    l1b = "P<THATESTER<<MALEE<<<<<<<<<<<<<<<<<<<<<<<<<<"
    s1 = parse_mrz(L1, L2, _src("pp1.png"))
    s2 = parse_mrz(l1b, _line2("AB7654321", "900230", "F", "270115"), _src("pp2.png"))
    b = _bundle([s1, s2])
    b.name_list.end_date = d(2026, 10, 30)
    rows = {r.name_en: r for r in build_rows(b)}
    ok = rows["SAMPLE/SOMCHAI"]
    assert ok.problems == 0 and ok.expiry_status == "ok"
    bad = rows["TESTER/MALI"]
    assert [c.status for c in bad.cells if c.field == "name"] == ["differ"]
    assert bad.expiry_status == "short"  # Jan 2027 is under 6 months after 30 Oct 2026
    assert rows["PERSON/NOSCAN"].passport_file is None and rows["PERSON/NOSCAN"].problems


def test_internal_endpoint():
    from fastapi.testclient import TestClient
    from ops_agent.web.app import app
    c = TestClient(app)
    s = c.post("/api/groups/check", files=[("files", ("passport.png", FIX.read_bytes(), "image/png"))]).json()
    d = c.get(f"/api/sessions/{s['session']}/internal").json()
    assert d["rows"] == [] and len(d["extra_scans"]) == 2 and d["has"]["passports"] == 2


def test_mrz_ocr_slips():
    # dropped '<' after the type letter, single '<' between the names
    s = parse_mrz("PTHASOMSRI<TESTNAME<<<<", L2, _src("x"))
    assert (s.surname, s.given_name) == ("SOMSRI", "TESTNAME")
    # '<' fillers read as K, and a line 2 with one '<' missing (birth date unknown, year only)
    l2 = _line2("AD1234567", "44<<<<", "F", "281102")
    s = parse_mrz("PKTHATESTER<<YEARONLY<KKKKK<<<<", l2.replace("<<<<", "<<<", 1), _src("x"))
    assert (s.surname, s.given_name, s.passport_no, s.dob) == ("TESTER", "YEARONLY", "AD1234567", None) and s.checks_ok


def test_vision_backup_reads_a_blank_page(tmp_path, monkeypatch):
    """A page the local OCR cannot read goes to Cloud Vision when OPS_VISION is on."""
    from PIL import Image
    from ops_agent.parsers import passport as P
    blank = tmp_path / "blank.png"
    Image.new("RGB", (1200, 900), "white").save(blank)
    monkeypatch.setattr(P, "_vision_lines", lambda img: ["noise", L1, L2.replace("<", "< ", 1)])
    monkeypatch.delenv("OPS_VISION", raising=False)
    assert P._read_passports(blank, "blank.png", "image") == []
    monkeypatch.setenv("OPS_VISION", "1")
    scans = P._read_passports(blank, "blank.png", "image")
    assert [(s.passport_no, s.checks_ok) for s in scans] == [("AA1234567", True)]


def test_line2_end_hidden_by_a_signature():
    """Only the first 28 characters of line 2 are legible: those fields still count (own check digits)."""
    sc = parse_mrz(L1, L2[:28] + "9938号XX", _src("p.pdf"))
    assert (sc.passport_no, sc.dob, sc.expiry, sc.checks_ok) == ("AA1234567", date(1985, 4, 12), date(2031, 1, 1), True)


def test_separator_read_as_k():
    from ops_agent.verify import match_scans
    src = _src("NAMELIST.xlsx", DocType.NAME_LIST)
    p = Passenger(given_name="MALI", surname="TESTSURNAME", passport_no="AB7654321", source=src)
    sc = PassportScan(surname="TESTSURNAMEKMALI", given_name="", passport_no="AB7654321", checks_ok=True, source=_src("p.pdf"))
    match_scans([p], [sc])
    assert (sc.surname, sc.given_name) == ("TESTSURNAME", "MALI")
