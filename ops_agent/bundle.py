"""Read every upload for one group into a GroupBundle."""

from __future__ import annotations

from pathlib import Path

from .detect import detect_named
from .models import DocType, GroupBundle, SourceRef
from .parsers import parse_eticket, parse_land_confirmation, parse_namelist, parse_pnr, parse_prep_letter
from .parsers.expense_template import parse_expense_template
from .parsers.land_confirmation import LLM, UnknownLayout


def read_group(files: list[tuple[str | Path, str]], llm: LLM | None = None,
               types: dict[str, DocType] | None = None) -> GroupBundle:
    """files: (path on disk, display name shown to the user). types: display name -> type chosen by the officer."""
    bundle = GroupBundle()
    types = types or {}
    typed = []
    for p, name in files:
        dtype, fmt = detect_named(p, name)
        typed.append((Path(p), name, types.get(name, dtype), fmt))
    # Name list first: its dates resolve year-less dates in the other documents.
    typed.sort(key=lambda t: t[2] != DocType.NAME_LIST)
    for path, name, dtype, fmt in typed:
        try:
            _read_one(bundle, path, name, dtype, fmt, llm)
        except UnknownLayout as e:
            bundle.unread.append(SourceRef(file=name, doc_type=dtype, location=f"not read: {e}"))
        except Exception as e:  # a file forced to the wrong type, or damaged
            bundle.unread.append(SourceRef(file=name, doc_type=dtype,
                                           location=f"not read as {dtype.value}: {type(e).__name__}: {e}"[:300]))
    return bundle


def _read_one(bundle: GroupBundle, path: Path, name: str, dtype: DocType, fmt: str, llm) -> None:
    if dtype == DocType.NAME_LIST:
        bundle.name_list = parse_namelist(path, name)
    elif dtype == DocType.PREP_LETTER:
        bundle.prep_letter = parse_prep_letter(path, name)
    elif dtype == DocType.ETICKET:
        bundle.etickets.append(parse_eticket(path, name))
    elif dtype == DocType.PNR:
        ref = bundle.name_list.start_date if bundle.name_list else None
        bundle.pnrs.append(parse_pnr(path, name, ref_date=ref, fmt=fmt))
    elif dtype == DocType.EXPENSE_TEMPLATE:
        bundle.expense_template = parse_expense_template(path, name)
    elif dtype == DocType.LAND_CONFIRMATION:
        start = bundle.name_list.start_date if bundle.name_list else None
        bundle.land_confirmation = parse_land_confirmation(path, llm, start, name, fmt)
    elif dtype == DocType.PASSPORT:
        from .parsers.passport import read_passports
        scans = read_passports(path, name, fmt)
        if not scans:
            bundle.unread.append(SourceRef(file=name, doc_type=dtype,
                                           location="no passport machine-readable zone (<<<) found; scan the photo page clearly"))
        bundle.passports += scans
    else:
        bundle.unread.append(SourceRef(file=name, doc_type=dtype, location=f"not read: {dtype.value}"))
