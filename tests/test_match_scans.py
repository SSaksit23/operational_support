"""Passport scans go to one traveller each (made-up data)."""
from ops_agent.models import DocType, Passenger, PassportScan, SourceRef
from ops_agent.verify import match_scans


SRC = SourceRef(file="n.xlsx", doc_type=DocType.NAME_LIST, location="row 1")


def _pax(**kw):
    return Passenger(source=SRC, **kw)


def _scan(sur, given, no=None):
    return PassportScan(surname=sur, given_name=given, passport_no=no, checks_ok=True,
                        source=SourceRef(file="p.pdf", doc_type=DocType.PASSPORT, location="page 1"))


def test_sisters_do_not_share_a_passport():
    a = _pax(given_name="WANNEE", surname="TESTSURNAME")
    b = _pax(given_name="SUWANNA", surname="TESTSURNAME")
    only_a = _scan("TESTSURNAME", "WANNEE")
    assert match_scans([b, a], [only_a]) == {id(a): only_a}
    both = [_scan("TESTSURNAME", "SUWANNA"), only_a]
    m = match_scans([a, b], both)
    assert m[id(a)] is only_a and m[id(b)] is both[0]


def test_passport_number_wins():
    a = _pax(given_name="ONE", surname="TESTER", passport_no="AA0000001")
    sc = _scan("TESTER", "ONX", "AA0000001")
    assert match_scans([a], [sc]) == {id(a): sc}
