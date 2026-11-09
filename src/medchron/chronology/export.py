"""Human-readable exports of a chronology (Markdown and CSV)."""

from __future__ import annotations

import csv
import io

from medchron.chronology.models import Chronology


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def to_markdown(chron: Chronology) -> str:
    lines = [
        f"# Medical chronology: {chron.case_id}",
        "",
        "> DRAFT. Machine-generated from synthetic records for attorney review. "
        "Every entry cites its source page; verify before relying on it.",
        "",
        "| Date | Type | Event | Facility / author | Source | Confidence | Flags |",
        "|---|---|---|---|---|---|---|",
    ]
    for e in chron.events:
        sources = "; ".join(f'p{s.page_no}: "{_cell(s.quote)}"' for s in e.sources)
        lines.append(
            f"| {e.date.isoformat() if e.date else '(undated)'} | {e.type.value} "
            f"| {_cell(e.summary)} | {_cell(e.facility)} / {_cell(e.provider)} "
            f"| {sources} | {e.confidence.value} | {', '.join(e.flags)} |"
        )
    if chron.conflicts or chron.gaps or chron.flags:
        lines += ["", "## For review", ""]
        lines += [f"- **Possible date conflict**: {c.description}" for c in chron.conflicts]
        lines += [f"- **Possible missing records**: {g.description}" for g in chron.gaps]
        lines += [
            f"- **{f.kind.replace('_', ' ').capitalize()}**"
            f"{f' (p{f.page_no})' if f.page_no else ''}: {f.note}"
            for f in chron.flags
        ]
    lines += [
        "",
        "## Document index",
        "",
        "| Pages | Type | Title | Date | Facility |",
        "|---|---|---|---|---|",
    ]
    for d in chron.documents:
        lines.append(
            f"| {d.start_page}-{d.end_page} | {d.doc_type.value} | {_cell(d.title)} "
            f"| {d.date.isoformat() if d.date else ''} | {_cell(d.facility)} |"
        )
    return "\n".join(lines) + "\n"


def to_csv(chron: Chronology) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        ["event_id", "date", "type", "summary", "facility", "provider", "pages", "quotes",
         "span_ids", "confidence", "flags"]
    )  # fmt: skip
    for e in chron.events:
        w.writerow(
            [
                e.event_id,
                e.date.isoformat() if e.date else "",
                e.type.value,
                e.summary,
                e.facility,
                e.provider,
                ";".join(str(s.page_no) for s in e.sources),
                " || ".join(s.quote for s in e.sources),
                ";".join(s.span_id for s in e.sources),
                e.confidence.value,
                ";".join(e.flags),
            ]
        )
    return buf.getvalue()
