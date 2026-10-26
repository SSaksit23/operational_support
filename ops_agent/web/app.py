"""Web app: upload a group's documents, review the checks, then draft the TL message and the expense workbook.

  uvicorn ops_agent.web.app:app --port 8000
  (optional) OPS_DB_DSN (or Neon's DATABASE_URL) + OPS_AGENT_KEY to save groups and search them later;
             .env.ops / .env.local next to the app are read at start-up
  (optional) OPS_FIREBASE_PROJECT etc. to require Google sign-in, see auth.py
  (optional) OPS_FONT_CSS = an Adobe Fonts kit URL (https://use.typekit.net/xxxxxxx.css) for Aktiv Grotesk Thai

Sessions live in memory and in a temp folder, one per uploaded group. Nothing
is kept after the server stops unless the officer presses Save.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel

from ..bundle import read_group
from ..companies import load_companies
from ..detect import detect, detect_named
from ..expense_fill import ROLES, TemplateError, fill_expense_workbook
from ..message import FIXED_BY_LATER_STEP, DraftBlocked, MessageSettings, draft_tl_message
from ..models import DocType, GroupBundle
from ..review import review_message
from ..verify import Finding, Verdict, verify
from . import auth, envfiles

envfiles.load()

STATIC = Path(__file__).with_name("static")
MAX_FILE_BYTES = 25 * 1024 * 1024
DOC_LABELS = {
    DocType.NAME_LIST: "รายชื่อลูกค้า (Name list)",
    DocType.PREP_LETTER: "ใบนัดหมาย / ใบเตรียมตัว",
    DocType.ETICKET: "E-ticket",
    DocType.PNR: "PNR / รายการตั๋ว",
    DocType.LAND_CONFIRMATION: "Land confirmation",
    DocType.EXPENSE_TEMPLATE: "ใบเบิกค่าใช้จ่าย TL",
    DocType.PASSPORT: "พาสปอร์ต (Passport)",
}


class Session:
    def __init__(self, folder: Path, files: list[tuple[Path, str]], bundle: GroupBundle, findings: list[Finding]):
        self.id = uuid.uuid4().hex
        self.folder, self.files, self.bundle, self.findings = folder, files, bundle, findings
        self.approvals: dict[int, dict] = {}  # finding index -> {by, at, note, value}
        self.types: dict[str, DocType] = {}  # display name -> type chosen by the officer
        self.created = datetime.now()


SESSIONS: dict[str, Session] = {}
JOBS: dict[str, dict] = {}
_lock = threading.Lock()
app = FastAPI(title="Ops support agent")


@app.middleware("http")
async def require_sign_in(request: Request, call_next):
    if request.url.path.startswith("/api/") and request.url.path not in auth.OPEN_PATHS:
        header = request.headers.get("authorization", "")
        status, claims, reason = auth.check(header[7:] if header.lower().startswith("bearer ") else None)
        if status != 200:
            return JSONResponse({"detail": reason}, status_code=status)
        request.state.user = (claims or {}).get("email")
    return await call_next(request)


def _session(sid: str) -> Session:
    s = SESSIONS.get(sid)
    if not s:
        raise HTTPException(404, "This check has expired. Upload the documents again.")
    return s


def _finding_json(i: int, f: Finding, s: Session) -> dict:
    return {
        "id": i, "rule": f.rule, "verdict": f.verdict.value, "title": f.title, "detail": f.detail,
        "accepted_value": f.accepted_value,
        "evidence": [{"file": e.file, "location": e.location, "raw": e.raw} for e in f.evidence],
        "compare": [c.model_dump() for c in f.compare],
        "blocking": f.verdict == Verdict.INCORRECT and f.rule not in FIXED_BY_LATER_STEP,
        "fixed_by_workbook": f.rule in FIXED_BY_LATER_STEP,
        "approval": s.approvals.get(i),
        "fixable_in_namelist": f.rule.startswith("flights.") or f.rule == "passengers.spelling_pnr",
    }


def _open_blocking(s: Session) -> list[int]:
    return [i for i, f in enumerate(s.findings) if f.verdict == Verdict.INCORRECT
            and f.rule not in FIXED_BY_LATER_STEP and i not in s.approvals]


def _acknowledged(s: Session) -> set[str]:
    """Rules whose every incorrect finding has been approved (the message builder works per rule)."""
    rules = {f.rule for f in s.findings if f.verdict == Verdict.INCORRECT}
    return {r for r in rules if all(i in s.approvals for i, f in enumerate(s.findings)
                                    if f.rule == r and f.verdict == Verdict.INCORRECT)}


def _summary(s: Session) -> dict:
    b = s.bundle
    nl, pl = b.name_list, b.prep_letter
    unread = {u.file: u.location for u in b.unread}
    docs = []
    for path, name in s.files:
        try:
            dtype = s.types.get(name) or detect_named(path, name)[0]
        except Exception:
            dtype = None
        docs.append({"name": name, "type": dtype.value if dtype else None, "forced": name in s.types,
                     "label": DOC_LABELS.get(dtype, "ไม่รู้จักไฟล์นี้ (Unknown)") if dtype else "ไม่รู้จักไฟล์นี้ (Unknown)",
                     "read": name not in unread, "note": unread.get(name)})
    group = None
    if nl:
        group = {"tour_code": nl.tour_code, "program": nl.program_name, "program_code": nl.program_code,
                 "start": nl.start_date.isoformat() if nl.start_date else None,
                 "end": nl.end_date.isoformat() if nl.end_date else None,
                 "pax": len(nl.passengers), "customers": sum(not p.is_tour_leader for p in nl.passengers),
                 "rooms": nl.rooms_total, "room_mix": nl.room_mix, "tour_leader": nl.tour_leader,
                 "hotels": [{"date": h.date.isoformat(), "name": h.hotel_name} for h in (pl.hotels if pl else [])]}
    missing = [DOC_LABELS[t] for t, have in (
        (DocType.NAME_LIST, nl), (DocType.PREP_LETTER, pl), (DocType.ETICKET, b.etickets or b.pnrs),
        (DocType.LAND_CONFIRMATION, b.land_confirmation), (DocType.EXPENSE_TEMPLATE, b.expense_template)) if not have]
    return {"session": s.id, "group": group, "land": _land_json(b.land_confirmation), "documents": docs, "missing": missing,
            "findings": [_finding_json(i, f, s) for i, f in enumerate(s.findings)],
            "blocking": _open_blocking(s),
            "types": [{"value": t.value, "label": DOC_LABELS[t]} for t in DOC_LABELS]}


def _land_json(lc) -> Optional[dict]:
    if not lc:
        return None
    iso = lambda d: d.isoformat() if d else None  # noqa: E731
    return {
        "file": lc.source.file, "layout": lc.layout, "operator": lc.land_operator.name if lc.land_operator else None,
        "sender": lc.land_operator.nickname if lc.land_operator else None,
        "tour_code": lc.tour_code, "start": iso(lc.start_date), "end": iso(lc.end_date), "route": lc.route,
        "adults": lc.adults, "escorts": lc.escorts, "tour_leader": lc.tour_leader,
        "guides": [f"{g.name} {g.phone or ''}".strip() for g in lc.guides],
        "contacts": [f"{c.name} {c.phone or ''}".strip() for c in lc.other_contacts],
        "rooms": lc.rooms, "hotel_standard": lc.hotel_standard, "meal_plan": lc.meal_plan, "coach": lc.coach,
        "confirmed_on": iso(lc.confirmed_on),
        "flights": [f"{f.flight_no} {f.date:%d %b} {f.origin}-{f.destination} {f.dep_time}-{f.arr_time}" for f in lc.flights],
        "days": [{"date": iso(m.date), "breakfast": m.breakfast, "lunch": m.lunch, "dinner": m.dinner,
                  "hotel": next((f"{h.hotel_name}" + (f" ({h.phone})" if h.phone else "") for h in lc.hotels
                                 if h.date == m.date), None)} for m in lc.meals],
        "itinerary": lc.itinerary,
    }


@app.get("/api/companies")
def companies():
    return [{"id": c.id, "label": c.label, "tax_id": c.tax_id, "lines": c.lines} for c in load_companies().values()]


@app.get("/api/config")
def config():
    return {"store": bool(os.environ.get("OPS_DB_DSN")), "font_css": os.environ.get("OPS_FONT_CSS"),
            "roles": {k: v[0] for k, v in ROLES.items()}, "firebase": auth.web_config()}


@app.post("/api/groups/check")
async def check(files: list[UploadFile] = File(...), background: bool = False):
    """Read and check a group. With ?background=1 it answers at once with a job id to poll
    (GET /api/jobs/{id}): passport OCR can outlast the 60 s that Firebase Hosting waits."""
    if not files:
        raise HTTPException(400, "Upload at least the name list.")
    folder = Path(tempfile.mkdtemp(prefix="opsgroup-"))
    saved = []
    for i, up in enumerate(files):
        data = await up.read()
        if len(data) > MAX_FILE_BYTES:
            shutil.rmtree(folder, ignore_errors=True)
            raise HTTPException(413, f"{up.filename} is larger than 25 MB.")
        p = folder / f"{i:02d}"
        p.write_bytes(data)
        saved.append((p, Path(up.filename or f"file{i}").name))
    if not background:
        # Off the event loop: OCR can take a while, and other requests must still be answered meanwhile.
        return await run_in_threadpool(_check_saved, folder, saved)
    job = uuid.uuid4().hex
    with _lock:
        JOBS[job] = {"status": "running", "started": datetime.now().isoformat(timespec="seconds")}

    def run():
        try:
            result = {"status": "done", "result": _check_saved(folder, saved)}
        except HTTPException as e:
            result = {"status": "error", "code": e.status_code, "detail": e.detail}
        except Exception as e:  # pragma: no cover
            result = {"status": "error", "code": 500, "detail": f"Could not read the documents: {e}"}
        with _lock:
            JOBS[job] = result
    threading.Thread(target=run, daemon=True).start()
    return JSONResponse({"job": job, "status": "running"}, status_code=202)


@app.get("/api/jobs/{job}")
def job_status(job: str):
    with _lock:
        j = JOBS.get(job)
    if not j:
        raise HTTPException(404, "This check is no longer on the server. Upload the files again.")
    return j


def _check_saved(folder: Path, saved: list[tuple[Path, str]]) -> dict:
    try:
        bundle = read_group(saved)
    except Exception as e:  # a reader failed on a file it claimed
        shutil.rmtree(folder, ignore_errors=True)
        raise HTTPException(422, f"Could not read the documents: {e}")
    if not (bundle.name_list or bundle.prep_letter or bundle.land_confirmation or bundle.etickets or bundle.pnrs or bundle.passports):
        shutil.rmtree(folder, ignore_errors=True)
        raise HTTPException(422, "None of these files could be read as a group document. Add the name list (xlsx).")
    s = Session(folder, saved, bundle, verify(bundle))
    with _lock:
        SESSIONS[s.id] = s
    return _summary(s)


class Approval(BaseModel):
    finding: int
    by: str
    value: Optional[str] = None  # a value picked from the side-by-side rows; None = keep the system's choice
    note: str = ""


@app.get("/api/sessions/{sid}/internal")
def internal(sid: str):
    """Internal check: one row per traveller (name list x passport x trip dates), plus the related findings."""
    from ..internal_check import RULES, build_rows, extra_scans, extra_tickets, tickets, trip_end
    s = _session(sid)
    b = s.bundle
    return {
        "session": sid,
        "trip_end": trip_end(b),
        "has": {"name_list": bool(b.name_list), "prep_letter": bool(b.prep_letter), "passports": len(b.passports),
                "tickets": len(tickets(b))},
        "rows": [r.model_dump() for r in build_rows(b)],
        "extra_scans": [{"name": x.airline_name, "passport_no": x.passport_no, "file": f"{x.source.file} {x.source.location}"}
                        for x in extra_scans(b)],
        "extra_tickets": [{"name": t.name, "ticket_no": t.ticket_no, "file": f"{t.source.file} {t.source.location or ''}".strip()}
                          for t in extra_tickets(b)],
        "finding_ids": [i for i, f in enumerate(s.findings) if f.rule.startswith(RULES)],
    }


@app.post("/api/sessions/{sid}/approve")
def approve(sid: str, a: Approval):
    s = _session(sid)
    if not a.by.strip():
        raise HTTPException(400, "Type your name to approve.")
    if not 0 <= a.finding < len(s.findings) or s.findings[a.finding].verdict not in (Verdict.INCORRECT, Verdict.UNRESOLVED, Verdict.INFO):
        raise HTTPException(400, "Nothing to approve for this check.")
    f = s.findings[a.finding]
    if a.value is not None and a.value not in [c.value for c in f.compare]:
        raise HTTPException(400, "Pick one of the values shown for this check.")
    if a.value is not None and f.rule.startswith("flights."):
        f.accepted_value = a.value  # the TL message uses the officer's flight
    s.approvals[a.finding] = {"by": a.by.strip(), "at": datetime.now().isoformat(timespec="seconds"),
                              "note": a.note, "value": a.value}
    return _summary(s)


@app.delete("/api/sessions/{sid}/approve/{finding}")
def unapprove(sid: str, finding: int):
    s = _session(sid)
    s.approvals.pop(finding, None)
    return _summary(s)


class Retype(BaseModel):
    name: str
    type: str


@app.post("/api/sessions/{sid}/retype")
def retype(sid: str, r: Retype):
    """The officer says what a file is; everything is read and checked again (approvals start over)."""
    s = _session(sid)
    if r.name not in [n for _, n in s.files]:
        raise HTTPException(400, f"No file named {r.name} in this check.")
    try:
        s.types[r.name] = DocType(r.type)
    except ValueError:
        raise HTTPException(400, f"Unknown document type {r.type}.")
    s.bundle = read_group(s.files, types=s.types)
    s.findings = verify(s.bundle)
    s.approvals = {}
    return _summary(s)


@app.get("/api/sessions/{sid}/namelist")
def corrected_namelist(sid: str):
    """A copy of the name list with the officer's picks written in (highlighted, with a comment per cell)."""
    from ..fix_namelist import Fix, apply_fixes
    s = _session(sid)
    nl = s.bundle.name_list
    src = next((p for p, n in s.files if nl and n == nl.source.file), None)
    if not src:
        raise HTTPException(400, "No name list in this check.")
    fixes = []
    for i, ap in s.approvals.items():
        f, v = s.findings[i], ap.get("value")
        if not v:
            continue
        if f.rule.startswith("flights."):
            fixes.append(Fix(kind="flight", value=v, by=ap["by"]))
        elif f.rule == "passengers.spelling_pnr":
            name = v.split(" (ticket")[0]
            p = next((p for p in nl.passengers if p.airline_name == f.compare[0].value), None)
            if p:
                fixes.append(Fix(kind="name", value=name, by=ap["by"], passport_no=p.passport_no))
    if not fixes:
        raise HTTPException(400, "Nothing to change yet: pick the correct value on a flight or name check first.")
    out = s.folder / f"{nl.tour_code}-NAMELIST-corrected.xlsx"
    changes = apply_fixes(src, fixes, out)
    if not changes:
        raise HTTPException(400, "The name list already has the values you picked.")
    return FileResponse(out, filename=out.name, headers={"X-Changes": str(len(changes))},
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


class MessageRequest(BaseModel):
    company_id: str
    today: Optional[date] = None
    tip_per_pax_thb: Optional[int] = None
    gift_budget_per_pax_thb: Optional[float] = 35
    sender_line_name: str = "[ชื่อ LINE ของเรา]"
    sender_particle: str = "ครับ"


@app.post("/api/sessions/{sid}/message")
def message(sid: str, r: MessageRequest):
    s = _session(sid)
    company = _company(r.company_id)
    settings = MessageSettings(tip_per_pax_thb=r.tip_per_pax_thb, gift_budget_per_pax_thb=r.gift_budget_per_pax_thb,
                               sender_line_name=r.sender_line_name, sender_particle=r.sender_particle)
    if not s.bundle.name_list:
        raise HTTPException(400, "The TL message needs the customer name list. Upload it with the other files.")
    try:
        text = draft_tl_message(s.bundle, s.findings, company, today=r.today or date.today(),
                                acknowledged=_acknowledged(s), settings=settings)
    except DraftBlocked as e:
        return JSONResponse(status_code=409, content={
            "detail": "Approve or fix these checks first.", "blocking": [f.title for f in e.findings]})
    claims = review_message(text, s.bundle, company, gift_budget_per_pax=r.gift_budget_per_pax_thb)
    return {"text": text, "claims": [c.model_dump() for c in claims]}


class ReviewRequest(BaseModel):
    text: str
    company_id: Optional[str] = None
    gift_budget_per_pax_thb: Optional[float] = 35


@app.post("/api/sessions/{sid}/review")
def review(sid: str, r: ReviewRequest):
    s = _session(sid)
    company = _company(r.company_id) if r.company_id else None
    return {"claims": [c.model_dump() for c in review_message(r.text, s.bundle, company,
                                                               gift_budget_per_pax=r.gift_budget_per_pax_thb)]}


class WorkbookRequest(BaseModel):
    selection: dict[str, str]
    gift_budget_per_pax_thb: Optional[float] = 35


@app.post("/api/sessions/{sid}/workbook")
def workbook(sid: str, r: WorkbookRequest):
    s = _session(sid)
    if not s.bundle.name_list:
        raise HTTPException(400, "The workbook needs the customer name list. Upload it with the other files.")
    template = next((p for p, _ in s.files if detect(p)[0] == DocType.EXPENSE_TEMPLATE), None)
    if not template:
        raise HTTPException(400, "Upload the TL expense template (xlsx) to make the workbook.")
    for role in ROLES:
        if role not in r.selection:
            raise HTTPException(400, f"Choose a company for {ROLES[role][0]}.")
        _company(r.selection[role])
    code = s.bundle.name_list.tour_code
    out = s.folder / f"{code}-TL-expense.xlsx"
    try:
        fill_expense_workbook(template, s.bundle, load_companies(), r.selection, out,
                              gift_budget_per_pax=r.gift_budget_per_pax_thb)
    except TemplateError as e:
        raise HTTPException(422, str(e))
    return FileResponse(out, filename=out.name,
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


class SaveRequest(BaseModel):
    company_id: Optional[str] = None


_schema_ready = False


def _store():
    dsn = os.environ.get("OPS_DB_DSN")
    if not dsn:
        raise HTTPException(503, "The group database is not set up on this server (OPS_DB_DSN).")
    from ..store.crypto import IdVault
    from ..store.embed import HashingEmbedder, OpenAIEmbedder
    from ..store.repo import GroupStore
    embedder = OpenAIEmbedder() if os.environ.get("OPS_EMBEDDER") == "openai" else HashingEmbedder()
    global _schema_ready
    if not _schema_ready:
        # Create the tables once per server start, over the direct (non-pooled) connection when there is one.
        direct = GroupStore(os.environ.get("DATABASE_URL_UNPOOLED") or dsn, embedder, IdVault())
        try:
            direct.init_schema()
        finally:
            direct.close()
        _schema_ready = True
    return GroupStore(dsn, embedder, IdVault())


@app.post("/api/sessions/{sid}/save")
def save(sid: str, r: SaveRequest):
    s = _session(sid)
    if not s.bundle.name_list:
        raise HTTPException(400, "Saving a group needs the customer name list.")
    store = _store()
    try:
        gid = store.save_group(s.bundle, s.findings, s.files, company_id=r.company_id)
    finally:
        store.close()
    return {"group_id": gid, "tour_code": s.bundle.name_list.tour_code}


@app.get("/api/search")
def search(q: str, kind: Optional[str] = None, group: Optional[str] = None, k: int = 10):
    if not q.strip():
        raise HTTPException(400, "Type something to search for.")
    store = _store()
    try:
        hits = store.search(q, k=min(k, 50), tour_code=group or None, kind=kind or None)
    finally:
        store.close()
    return [h.model_dump() for h in hits]


def _company(cid: str):
    c = load_companies().get(cid)
    if not c:
        raise HTTPException(400, f"Unknown company {cid}.")
    return c


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
