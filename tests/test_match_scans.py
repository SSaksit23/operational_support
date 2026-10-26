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


def test_ticket_column():
    from ops_agent.internal_check import build_rows, extra_tickets
    from ops_agent.models import ETicketDoc, GroupBundle, NameListDoc, Ticket
    tsrc = SourceRef(file="t.pdf", doc_type=DocType.ETICKET, location="page 1")
    a, b = _pax(given_name="ONE", surname="TESTER"), _pax(given_name="TWO", surname="TESTER")
    bundle = GroupBundle(
        name_list=NameListDoc(tour_code="TEST1", passengers=[a, b], source=SRC),
        etickets=[ETicketDoc(tickets=[Ticket(passenger_name="TESTER/ONE", ticket_no="000111", source=tsrc),
                                      Ticket(passenger_name="OTHER/PERSON", ticket_no="000222", source=tsrc)],
                             source=tsrc)])
    rows = build_rows(bundle)
    assert [r.cells[-1].status for r in rows] == ["ok", "missing"]
    assert rows[1].problems >= 1
    assert [t.name for t in extra_tickets(bundle)] == ["OTHER/PERSON"]
