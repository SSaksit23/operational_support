"""Save checked groups and search them (PostgreSQL + pgvector).

Saving a group again (new versions of the documents, a re-check) replaces
that group's facts, findings, bookings and chunks; files already stored are
recognised by their SHA-256 and not duplicated. Customers are matched across
groups by a keyed hash of the passport number.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Iterable, Optional

import psycopg
from pgvector.psycopg import register_vector
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from ..companies import load_companies
from ..detect import detect
from ..models import GroupBundle
from ..verify import Finding
from .chunks import Chunk, document_page_chunks, group_chunks, scrub
from .crypto import IdVault
from .embed import Embedder

SCHEMA = Path(__file__).with_name("schema.sql")


class Hit(BaseModel):
    tour_code: Optional[str]
    kind: str
    location: Optional[str]
    file_name: Optional[str]
    content: str
    score: float


class GroupStore:
    def __init__(self, dsn: str, embedder: Embedder, vault: IdVault):
        self.dsn, self.embedder, self.vault = dsn, embedder, vault
        # No server-side prepared statements, so a transaction-mode pooler (Neon's -pooler host) is safe.
        self.conn = psycopg.connect(dsn, autocommit=False, prepare_threshold=None)
        self.conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        self.conn.commit()
        register_vector(self.conn)

    def close(self):
        self.conn.close()

    def init_schema(self) -> None:
        self.conn.execute(SCHEMA.read_text(encoding="utf-8") % {"dim": self.embedder.dim})
        self.conn.commit()

    # ------------------------------------------------------------ saving

    def save_group(self, bundle: GroupBundle, findings: list[Finding], files: list[tuple[str | Path, str]],
                   company_id: Optional[str] = None, status: str = "checked") -> int:
        nl = bundle.name_list
        if not nl:
            raise ValueError("A name list is required to save a group")
        keep = {c.tax_id for c in load_companies().values()}
        with self.conn.transaction():
            cur = self.conn.cursor()
            group_id = cur.execute(
                """INSERT INTO groups (tour_code, program_name, program_code, start_date, end_date, pax_total,
                                       tour_leader, company_id, status)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (tour_code) DO UPDATE SET program_name=EXCLUDED.program_name,
                     program_code=EXCLUDED.program_code, start_date=EXCLUDED.start_date, end_date=EXCLUDED.end_date,
                     pax_total=EXCLUDED.pax_total, tour_leader=EXCLUDED.tour_leader,
                     company_id=COALESCE(EXCLUDED.company_id, groups.company_id), status=EXCLUDED.status, updated_at=now()
                   RETURNING id""",
                (nl.tour_code, nl.program_name, nl.program_code, nl.start_date, nl.end_date, len(nl.passengers),
                 nl.tour_leader, company_id, status)).fetchone()[0]
            for table in ("facts", "findings", "chunks", "bookings", "group_suppliers"):
                cur.execute(f"DELETE FROM {table} WHERE group_id = %s", (group_id,))

            doc_ids = {}
            for path, name in files:
                data = Path(path).read_bytes()
                sha = hashlib.sha256(data).hexdigest()
                dtype, _ = detect(path)
                doc_ids[name] = cur.execute(
                    """INSERT INTO documents (group_id, sha256, doc_type, file_name, storage_uri)
                       VALUES (%s,%s,%s,%s,%s)
                       ON CONFLICT (sha256) DO UPDATE SET group_id=EXCLUDED.group_id, file_name=EXCLUDED.file_name
                       RETURNING id""", (group_id, sha, dtype.value, name, str(path))).fetchone()[0]

            for p in nl.passengers:
                h = self.vault.lookup_hash(p.passport_no)
                cust = cur.execute(
                    """INSERT INTO customers (passport_hash, passport_enc, citizen_id_enc, given_name, surname,
                                              given_name_th, surname_th, gender, dob, tel, passport_expiry)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (passport_hash) DO UPDATE SET given_name=EXCLUDED.given_name,
                         surname=EXCLUDED.surname, given_name_th=EXCLUDED.given_name_th, surname_th=EXCLUDED.surname_th,
                         tel=COALESCE(EXCLUDED.tel, customers.tel), passport_expiry=EXCLUDED.passport_expiry,
                         updated_at=now()
                       RETURNING id""",
                    (h, self.vault.encrypt(p.passport_no), self.vault.encrypt(p.citizen_id), p.given_name, p.surname,
                     p.given_name_th, p.surname_th, p.gender, p.dob, p.tel, p.passport_expiry)).fetchone()[0]
                cur.execute("""INSERT INTO bookings (group_id, customer_id, room, agency, booking_no, age, remark,
                                                     is_tour_leader) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                            (group_id, cust, p.room, p.agency, p.booking_no, p.age, p.remark, p.is_tour_leader))

            pl, lc = bundle.prep_letter, bundle.land_confirmation
            for h in (pl.hotels if pl else []):
                sid = self._supplier(cur, "hotel", h.hotel_name, address=h.address)
                cur.execute("INSERT INTO group_suppliers VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                            (group_id, sid, h.date, f"{h.nights} night(s)"))
            if lc:
                if lc.land_operator:
                    lo = lc.land_operator
                    sid = self._supplier(cur, "land_operator", lo.name, lo.phone, lo.wechat)
                    cur.execute("INSERT INTO group_suppliers VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                                (group_id, sid, nl.start_date, "land operator"))
                for g in lc.guides:
                    sid = self._supplier(cur, "guide", g.name, g.phone, g.wechat)
                    cur.execute("INSERT INTO group_suppliers VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                                (group_id, sid, nl.start_date, "local guide"))

            for kind, items in self._facts(bundle):
                for value, src in items:
                    cur.execute("INSERT INTO facts (group_id, document_id, kind, value, location, raw) "
                                "VALUES (%s,%s,%s,%s,%s,%s)",
                                (group_id, doc_ids.get(src.file), kind, Jsonb(value), src.location,
                                 scrub(src.raw or "", keep) or None))
            for f in findings:
                cur.execute("INSERT INTO findings (group_id, rule, verdict, title, detail) VALUES (%s,%s,%s,%s,%s)",
                            (group_id, f.rule, f.verdict.value, f.title, scrub(f.detail, keep)))

            chunks = group_chunks(bundle, findings, keep)
            for path, name in files:
                chunks += document_page_chunks(path, name, keep | {c.phone for c in (pl.contacts if pl else [])})
            self._insert_chunks(cur, group_id, chunks, doc_ids)
        return group_id

    def import_settlement(self, json_path: str | Path) -> int:
        """Bring in a settlement statement saved by the existing rag-service (ChromaDB pipeline)."""
        data = json.loads(Path(json_path).read_text(encoding="utf-8"))
        cn, th = data.get("chinese", {}), data.get("thai", {})
        code = th.get("tour_code") or cn.get("tour_code")
        dates = th.get("dates") or cn.get("dates") or {}
        name = data.get("source_file") or Path(json_path).name
        with self.conn.transaction():
            cur = self.conn.cursor()
            group_id = cur.execute(
                """INSERT INTO groups (tour_code, program_name, start_date, end_date, pax_total, status)
                   VALUES (%s,%s,%s,%s,%s,'closed')
                   ON CONFLICT (tour_code) DO UPDATE SET updated_at=now() RETURNING id""",
                (code, f"{th.get('route', '')} {th.get('duration', '')}".strip(), dates.get("departure") or None,
                 dates.get("return") or None, (th.get("adults") or 0) + (th.get("children") or 0) +
                 (th.get("tour_leaders") or 0))).fetchone()[0]
            sha = hashlib.sha256(Path(json_path).read_bytes()).hexdigest()
            doc_id = cur.execute(
                """INSERT INTO documents (group_id, sha256, doc_type, file_name, storage_uri)
                   VALUES (%s,%s,'settlement',%s,%s) ON CONFLICT (sha256) DO UPDATE SET group_id=EXCLUDED.group_id
                   RETURNING id""", (group_id, sha, name, str(json_path))).fetchone()[0]
            cur.execute("DELETE FROM chunks WHERE document_id = %s", (doc_id,))
            cur.execute("DELETE FROM facts WHERE document_id = %s", (doc_id,))
            chunks = []
            for day_th, day_cn in zip(th.get("daily_itinerary", []), cn.get("daily_itinerary", []) or
                                      [{}] * len(th.get("daily_itinerary", []))):
                text = (f"กรุ๊ป {code} วันที่ {day_th.get('day')} ({day_th.get('date')}): "
                        f"{'; '.join(day_th.get('activities', []))}. อาหาร {day_th.get('meals') or '-'}. "
                        f"โรงแรม {day_th.get('hotel') or '-'} / {day_cn.get('hotel') or ''}")
                chunks.append(Chunk("itinerary", f"day {day_th.get('day')}", text, name))
                if day_th.get("hotel"):
                    sid = self._supplier(cur, "hotel", day_cn.get("hotel") or day_th["hotel"])
                    cur.execute("INSERT INTO group_suppliers VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                                (group_id, sid, day_th.get("date") or None, "settlement"))
            cost = th.get("cost_breakdown") or {}
            for i, item in enumerate(cost.get("items", []), start=1):
                cn_item = (cn.get("cost_breakdown") or {}).get("items", [{}] * i)[i - 1] if cn else {}
                text = (f"กรุ๊ป {code} ค่าใช้จ่าย {item.get('item')} / {cn_item.get('item', '')}: "
                        f"{item.get('unit_price')} x {item.get('quantity')} = {item.get('total')} {item.get('currency')}")
                chunks.append(Chunk("cost", f"cost item {i}", text, name))
                cur.execute("INSERT INTO facts (group_id, document_id, kind, value, location) VALUES (%s,%s,'cost',%s,%s)",
                            (group_id, doc_id, Jsonb(item), f"cost item {i}"))
            if th.get("total_cost"):
                chunks.append(Chunk("cost", "total", f"กรุ๊ป {code} ยอดรวมทั้งหมด {th['total_cost']} {th.get('currency', 'CNY')}",
                                    name))
            for i, term in enumerate(th.get("terms_and_conditions", []), start=1):
                chunks.append(Chunk("terms", f"term {i}", f"กรุ๊ป {code} เงื่อนไข: {term}", name))
            self._insert_chunks(cur, group_id, [c._replace(content=scrub(c.content)) for c in chunks], {name: doc_id})
        return group_id

    def _supplier(self, cur, kind, name, phone=None, wechat=None, address=None) -> int:
        return cur.execute(
            """INSERT INTO suppliers (kind, name, phone, wechat, address) VALUES (%s,%s,%s,%s,%s)
               ON CONFLICT (kind, name) DO UPDATE SET phone=COALESCE(EXCLUDED.phone, suppliers.phone),
                 wechat=COALESCE(EXCLUDED.wechat, suppliers.wechat), address=COALESCE(EXCLUDED.address, suppliers.address)
               RETURNING id""", (kind, name.strip(), phone, wechat, address)).fetchone()[0]

    def _insert_chunks(self, cur, group_id, chunks: list[Chunk], doc_ids: dict[str, int]) -> None:
        if not chunks:
            return
        vectors = self.embedder.embed([c.content for c in chunks])
        for c, v in zip(chunks, vectors):
            cur.execute("INSERT INTO chunks (group_id, document_id, kind, location, content, embedding) "
                        "VALUES (%s,%s,%s,%s,%s,%s::vector)",
                        (group_id, doc_ids.get(c.source_file), c.kind, c.location, c.content, v))

    @staticmethod
    def _facts(b: GroupBundle) -> Iterable[tuple[str, list]]:
        nl, pl = b.name_list, b.prep_letter
        legs = (pl.flights if pl else []) + (nl.flights if nl else [])
        for doc in b.pnrs:
            legs += doc.segments
        yield "flight", [(l.model_dump(mode="json", exclude={"source"}), l.source) for l in legs]
        if pl:
            yield "hotel", [(h.model_dump(mode="json", exclude={"source"}), h.source) for h in pl.hotels]
            yield "optional_tour", [(o.model_dump(mode="json", exclude={"source"}), o.source) for o in pl.optional_tours]
            yield "contact", [(c.model_dump(mode="json", exclude={"source"}), c.source) for c in pl.contacts]
            if pl.meeting:
                yield "meeting", [(pl.meeting.model_dump(mode="json", exclude={"source"}), pl.meeting.source)]
        for doc in b.etickets:
            yield "ticket", [({"name": t.passenger_name, "ticket_no": t.ticket_no, "record": t.airline_record,
                               "issued": t.issue_date.isoformat() if t.issue_date else None}, t.source) for t in doc.tickets]

    # ------------------------------------------------------------ reading

    def search(self, query: str, k: int = 5, tour_code: Optional[str] = None, kind: Optional[str] = None,
               date_from: Optional[date] = None, date_to: Optional[date] = None) -> list[Hit]:
        """Text similarity over chunks, narrowed by group, chunk kind and travel dates."""
        vec = self.embedder.embed([query])[0]
        where, args = ["true"], []
        if tour_code:
            where.append("g.tour_code = %s"); args.append(tour_code)  # noqa: E702
        if kind:
            where.append("c.kind = %s"); args.append(kind)  # noqa: E702
        if date_from:
            where.append("g.start_date >= %s"); args.append(date_from)  # noqa: E702
        if date_to:
            where.append("g.start_date <= %s"); args.append(date_to)  # noqa: E702
        rows = self.conn.execute(
            f"""SELECT g.tour_code, c.kind, c.location, d.file_name, c.content, 1 - (c.embedding <=> %s::vector) AS score
                FROM chunks c LEFT JOIN groups g ON g.id = c.group_id LEFT JOIN documents d ON d.id = c.document_id
                WHERE {' AND '.join(where)}
                ORDER BY c.embedding <=> %s::vector LIMIT %s""", [vec, *args, vec, k]).fetchall()
        self.conn.commit()
        return [Hit(tour_code=r[0], kind=r[1], location=r[2], file_name=r[3], content=r[4], score=float(r[5]))
                for r in rows]

    def customer_history(self, passport_no: str) -> list[dict]:
        """Every group a customer travelled with, found by passport without decrypting the table."""
        rows = self.conn.execute(
            """SELECT g.tour_code, g.start_date, c.given_name, c.surname, b.room, b.remark, b.is_tour_leader
               FROM customers c JOIN bookings b ON b.customer_id = c.id JOIN groups g ON g.id = b.group_id
               WHERE c.passport_hash = %s ORDER BY g.start_date""",
            (self.vault.lookup_hash(passport_no),)).fetchall()
        self.conn.commit()
        keys = ["tour_code", "start_date", "given_name", "surname", "room", "remark", "is_tour_leader"]
        return [dict(zip(keys, r)) for r in rows]

    def groups_at_hotel(self, name_part: str) -> list[tuple[str, date, str]]:
        rows = self.conn.execute(
            """SELECT DISTINCT g.tour_code, gs.service_date, s.name FROM group_suppliers gs
               JOIN suppliers s ON s.id = gs.supplier_id AND s.kind = 'hotel' JOIN groups g ON g.id = gs.group_id
               WHERE s.name ILIKE %s ORDER BY gs.service_date""", (f"%{name_part}%",)).fetchall()
        self.conn.commit()
        return rows

    def customers_over(self, age: int, date_from: Optional[date] = None) -> list[tuple]:
        rows = self.conn.execute(
            """SELECT g.tour_code, c.given_name_th, c.surname_th, b.age FROM bookings b
               JOIN customers c ON c.id = b.customer_id JOIN groups g ON g.id = b.group_id
               WHERE b.age >= %s AND (%s::date IS NULL OR g.start_date >= %s::date) ORDER BY b.age DESC""",
            (age, date_from, date_from)).fetchall()
        self.conn.commit()
        return rows

    def open_findings(self, tour_code: str) -> list[tuple[str, str, str]]:
        rows = self.conn.execute(
            """SELECT f.rule, f.verdict, f.title FROM findings f JOIN groups g ON g.id = f.group_id
               WHERE g.tour_code = %s AND f.verdict IN ('incorrect', 'unresolved') ORDER BY f.id""",
            (tour_code,)).fetchall()
        self.conn.commit()
        return rows
