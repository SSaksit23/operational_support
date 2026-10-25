"""Findings as a Markdown review table (claim, verdict, source, correction)."""

from __future__ import annotations

from .verify import Finding, Verdict

ORDER = [Verdict.INCORRECT, Verdict.UNRESOLVED, Verdict.INFO, Verdict.VERIFIED]
HEADINGS = {
    Verdict.INCORRECT: "Incorrect: must be fixed before drafting",
    Verdict.UNRESOLVED: "Unresolved: printed in the TL message until settled",
    Verdict.INFO: "For the TL's attention",
    Verdict.VERIFIED: "Verified",
}


def _cell(text: str) -> str:
    return " ".join(text.replace("|", "/").split("\n")) if text else ""


def to_markdown(findings: list[Finding], title: str) -> str:
    counts = {v: sum(1 for f in findings if f.verdict == v) for v in ORDER}
    lines = [f"# {title}", "",
             " · ".join(f"{counts[v]} {v.value}" for v in ORDER), ""]
    for v in ORDER:
        group = [f for f in findings if f.verdict == v]
        if not group:
            continue
        lines += [f"## {HEADINGS[v]}", "", "| Check | Finding | Detail | Source |", "| --- | --- | --- | --- |"]
        for f in group:
            src = "; ".join(sorted({f"{e.file} {e.location}" for e in f.evidence}))[:300]
            detail = f.detail.replace("\n", "<br>").replace("|", "/")
            lines.append(f"| {f.rule} | {_cell(f.title)} | {detail} | {_cell(src)} |")
        lines.append("")
    return "\n".join(lines)
