"""Exports of a chronology: Markdown, CSV, and the conventional DOCX and XLSX layouts.

The DOCX and XLSX exports follow the layout legal teams use for medical
chronologies: Date | Provider / Facility | Event | Source (document, page,
Bates number), plus the reviewer's decision. Each export carries a DRAFT /
synthetic-records banner and the version and approval it was produced from.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from dataclasses import dataclass

from medchron.chronology.models import Chronology, ExtractedEvent, Issue

BANNER = (
    "DRAFT - attorney work product - generated from SYNTHETIC records for review. "
    "Every entry cites its source page; verify before relying on it."
)
COLUMNS = ["Date", "Provider / Facility", "Event", "Source (document, page, Bates)", "Review"]
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@dataclass(frozen=True)
class ExportMeta:
    matter_id: str
    title: str = ""
    version: int | None = None
    approved_by: str | None = None
    approved_at: dt.datetime | None = None

    def lines(self) -> list[str]:
        out = [f"Matter: {self.title + ' - ' if self.title else ''}{self.matter_id}"]
        if self.version is not None:
            out.append(f"Chronology version: {self.version}")
        if self.approved_by:
            when = self.approved_at.isoformat(timespec="minutes") if self.approved_at else ""
            out.append(f"Approved for export by {self.approved_by} {when}".rstrip())
        return out


def _review(e: ExtractedEvent) -> str:
    status = next((f.split(":", 1)[1] for f in e.flags if f.startswith("review:")), "pending")
    return status


def export_rows(chron: Chronology, bates: dict[int, str] | None = None) -> list[list[str]]:
    """One row per event in the conventional chronology layout."""
    bates = bates or {}
    titles = {d.doc_id: d.title or d.doc_type.value.replace("_", " ") for d in chron.documents}
    rows = []
    for e in chron.events:
        who = " / ".join(x for x in (e.provider, e.facility) if x)
        quotes = " ".join(f'"{s.quote}"' for s in e.sources[:1])
        sources = "; ".join(
            f"{titles.get(e.doc_id, e.doc_id)}, p{s.page_no}"
            + (f", {bates[s.page_no]}" if s.page_no in bates else "")
            for s in e.sources
        )
        rows.append(
            [
                e.date.isoformat() if e.date else "(undated)",
                who,
                f"{e.summary}\n{quotes}" if quotes else e.summary,
                sources,
                _review(e),
            ]
        )
    return rows


def to_docx(chron: Chronology, meta: ExportMeta, bates: dict[int, str] | None = None) -> bytes:
    from docx import Document
    from docx.enum.section import WD_ORIENT
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt

    doc = Document()
    section = doc.sections[0]
    section.orientation = WD_ORIENT.LANDSCAPE
    section.page_width, section.page_height = section.page_height, section.page_width
    doc.add_heading("Medical chronology", level=1)
    doc.add_paragraph(BANNER).runs[0].bold = True
    for line in meta.lines():
        doc.add_paragraph(line)
    rows = export_rows(chron, bates)
    table = doc.add_table(rows=1, cols=len(COLUMNS))
    table.style = "Table Grid"
    header = table.rows[0]
    for cell, name in zip(header.cells, COLUMNS, strict=True):
        cell.text = name
        cell.paragraphs[0].runs[0].bold = True
    repeat = OxmlElement("w:tblHeader")  # repeat the header row on every page
    repeat.set(qn("w:val"), "true")
    header._tr.get_or_add_trPr().append(repeat)
    for row in rows:
        cells = table.add_row().cells
        for cell, value in zip(cells, row, strict=True):
            cell.text = value
    for row in table.rows:
        for cell in row.cells:
            for para in cell.paragraphs:
                for run in para.runs:
                    run.font.size = Pt(9)
    if chron.conflicts or chron.gaps:
        doc.add_heading("For review", level=2)
        for c in chron.conflicts:
            doc.add_paragraph(f"Possible date conflict: {c.description}", style="List Bullet")
        for g in chron.gaps:
            doc.add_paragraph(f"Possible missing records: {g.description}", style="List Bullet")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def to_xlsx(chron: Chronology, meta: ExportMeta, bates: dict[int, str] | None = None) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "Chronology"
    ws.append(COLUMNS)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="DDDDDD")
    for row in export_rows(chron, bates):
        ws.append(row)
    for col, width in zip("ABCDE", (12, 32, 60, 48, 12), strict=True):
        ws.column_dimensions[col].width = width
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    about = wb.create_sheet("About")
    for line in [BANNER, *meta.lines()]:
        about.append([line])
    about.column_dimensions["A"].width = 120
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def to_markdown(
    chron: Chronology, bates: dict[int, str] | None = None, meta: ExportMeta | None = None
) -> str:
    bates = bates or {}
    lines = [
        f"# Medical chronology: {chron.case_id}",
        "",
        "> DRAFT. Machine-generated from synthetic records for attorney review. "
        "Every entry cites its source page; verify before relying on it.",
        *(f"> {line}" for line in (meta.lines() if meta else [])),
        "",
        "| Date | Type | Event | Facility / author | Source | Confidence | Flags |",
        "|---|---|---|---|---|---|---|",
    ]
    for e in chron.events:
        sources = "; ".join(
            f"p{s.page_no}{f' ({bates[s.page_no]})' if s.page_no in bates else ''}: "
            f'"{_cell(s.quote)}"'
            for s in e.sources
        )
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


def issues_markdown(chronology_md: str, issues: list[Issue]) -> str:
    """Append the analyst's questions for expert review to a chronology export."""
    if not issues:
        return chronology_md
    lines = ["", "## Questions for expert review (issue analyst, verified)", ""]
    for i in issues:
        cites = "; ".join(f'p{c.page_no}: "{_cell(c.quote)}"' for c in i.citations)
        lines.append(f"- **[{i.category.replace('_', ' ')}]** {i.description}")
        lines.append(f"  - Evidence: {cites}")
        lines.append(
            f"  - Verifier: {i.verdict}{f' - {i.verdict_reason}' if i.verdict_reason else ''}"
        )
    return chronology_md + "\n".join(lines) + "\n"
