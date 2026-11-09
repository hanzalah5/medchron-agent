"""Evaluation metrics: compare a pipeline chronology with synthetic gold labels.

Matching rules (also documented in docs/eval-methodology.md):

- **Event match (strict)**: same event type, same calendar date, and at least
  one predicted source page among the gold event's source pages. Each gold
  event matches at most one prediction and vice versa. Pairs are assigned
  greedily by summary similarity, which is adequate here because candidates
  are already narrowed by type, date and page.
- **Event match (lenient)**: the same, but dates may differ by one day.
- **Citation validity**: a predicted source is valid when its quote is found
  at its recorded character offsets in the ingested page text. It is checked
  deterministically, independent of the gold labels.
- **Planted-issue detection**: a gold issue counts as detected when the
  pipeline raises the matching review signal (conflict, gap or flag) that
  points at one of the issue's pages or events.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter

from pydantic import BaseModel, Field

from medchron.chronology.builder import similarity
from medchron.chronology.models import Chronology, ExtractedEvent
from medchron.grounding.quotes import find_quote
from medchron.ingestion.models import IngestedCase
from medchron.synth.models import CaseGold, GoldEvent
from medchron.taxonomy import IssueKind

# Issue kinds the v0.1 pipeline attempts with deterministic signals. The
# others belong to the issue-analyst agent (roadmap phase 3) and are reported
# as "not attempted" rather than counted as misses.
ATTEMPTED_ISSUES = {IssueKind.DATE_CONFLICT, IssueKind.MISSING_RECORD, IssueKind.PROMPT_INJECTION}


class PRF(BaseModel):
    tp: int
    fp: int
    fn: int

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0


class EventMatch(BaseModel):
    gold_id: str
    pred_id: str


class CaseMetrics(BaseModel):
    case_id: str
    events_strict: PRF
    events_lenient: PRF
    per_type_recall: dict[str, float]
    segmentation: PRF  # document start pages
    doc_type_accuracy: float
    citation_validity: float
    citations_checked: int
    issues_detected: dict[str, int] = Field(description="kind -> detected count")
    issues_total: dict[str, int]
    missed_events: list[str] = Field(default_factory=list, description="gold summaries")
    spurious_events: list[str] = Field(default_factory=list, description="predicted summaries")


def _pages(e: ExtractedEvent) -> set[int]:
    return {s.page_no for s in e.sources}


def match_events(
    gold: list[GoldEvent], pred: list[ExtractedEvent], day_tolerance: int = 0
) -> list[EventMatch]:
    candidates: list[tuple[float, str, str]] = []
    for g in gold:
        gpages = {s.page for s in g.sources}
        for p in pred:
            if p.type != g.type or p.date is None:
                continue
            if abs((p.date - g.date).days) > day_tolerance:
                continue
            if not (_pages(p) & gpages):
                continue
            score = similarity(g.summary, p.summary + " " + " ".join(s.quote for s in p.sources))
            candidates.append((score, g.event_id, p.event_id))
    candidates.sort(key=lambda c: (-c[0], c[1], c[2]))
    used_g: set[str] = set()
    used_p: set[str] = set()
    matches: list[EventMatch] = []
    for _, gid, pid in candidates:
        if gid in used_g or pid in used_p:
            continue
        used_g.add(gid)
        used_p.add(pid)
        matches.append(EventMatch(gold_id=gid, pred_id=pid))
    return matches


def citation_validity(chron: Chronology, case: IngestedCase) -> tuple[float, int]:
    checked = valid = 0
    for e in chron.events:
        for s in e.sources:
            checked += 1
            try:
                page = case.page(s.page_no)
            except IndexError:
                continue
            cited = page.text[s.char_start : s.char_end]
            if cited and find_quote(cited, s.quote, threshold=0.85) is not None:
                valid += 1
    return (valid / checked if checked else 1.0), checked


def _issues(
    chron: Chronology, gold: CaseGold, matches: list[EventMatch]
) -> tuple[dict[str, int], dict[str, int]]:
    gold_to_pred = {m.gold_id: m.pred_id for m in matches}
    conflict_pages = {
        s.page_no
        for c in chron.conflicts
        for e in chron.events
        if e.event_id in c.event_ids
        for s in e.sources
    }
    gap_events = {eid for g in chron.gaps for eid in g.event_ids}
    injection_pages = {f.page_no for f in chron.flags if f.kind == "possible_prompt_injection"}
    detected: Counter[str] = Counter()
    total: Counter[str] = Counter()
    for issue in gold.issues:
        total[issue.kind.value] += 1
        pages = set(issue.pages)
        if issue.kind == IssueKind.DATE_CONFLICT:
            hit = bool(conflict_pages & pages)
        elif issue.kind == IssueKind.MISSING_RECORD:
            hit = any(gold_to_pred.get(eid) in gap_events for eid in issue.event_ids)
        elif issue.kind == IssueKind.PROMPT_INJECTION:
            hit = bool(injection_pages & pages)
        else:
            hit = False  # analyst agent territory (not attempted in v0.1)
        detected[issue.kind.value] += int(hit)
    return dict(detected), dict(total)


def score_case(chron: Chronology, gold: CaseGold, case: IngestedCase) -> CaseMetrics:
    strict = match_events(gold.events, chron.events)
    lenient = match_events(gold.events, chron.events, day_tolerance=1)
    matched_gold = {m.gold_id for m in strict}
    matched_pred = {m.pred_id for m in strict}

    by_type: dict[str, list[bool]] = {}
    for g in gold.events:
        by_type.setdefault(g.type.value, []).append(g.event_id in matched_gold)

    gold_starts = {d.start_page: d for d in gold.documents}
    pred_starts = {d.start_page: d for d in chron.documents}
    common = set(gold_starts) & set(pred_starts)
    type_ok = sum(gold_starts[p].doc_type == pred_starts[p].doc_type for p in common)

    validity, checked = citation_validity(chron, case)
    detected, total = _issues(chron, gold, strict)
    return CaseMetrics(
        case_id=gold.case_id,
        events_strict=PRF(
            tp=len(strict), fp=len(chron.events) - len(strict), fn=len(gold.events) - len(strict)
        ),
        events_lenient=PRF(
            tp=len(lenient),
            fp=len(chron.events) - len(lenient),
            fn=len(gold.events) - len(lenient),
        ),
        per_type_recall={t: sum(v) / len(v) for t, v in sorted(by_type.items())},
        segmentation=PRF(
            tp=len(common),
            fp=len(set(pred_starts) - set(gold_starts)),
            fn=len(set(gold_starts) - set(pred_starts)),
        ),
        doc_type_accuracy=type_ok / len(common) if common else 0.0,
        citation_validity=validity,
        citations_checked=checked,
        issues_detected=detected,
        issues_total=total,
        missed_events=[
            f"{g.date.isoformat()} {g.type.value}: {g.summary}"
            for g in gold.events
            if g.event_id not in matched_gold
        ],
        spurious_events=[
            f"{e.date.isoformat() if e.date else 'undated'} {e.type.value}: {e.summary}"
            for e in chron.events
            if e.event_id not in matched_pred
        ],
    )


class Aggregate(BaseModel):
    cases: int
    event_precision: float
    event_recall: float
    event_f1: float
    event_f1_lenient: float
    segmentation_f1: float
    doc_type_accuracy: float
    citation_validity: float
    issue_recall_attempted: float
    issue_recall_by_kind: dict[str, str]
    per_type_recall: dict[str, float]


def aggregate(cases: list[CaseMetrics]) -> Aggregate:
    def total(attr: str) -> PRF:
        prfs = [getattr(c, attr) for c in cases]
        return PRF(
            tp=sum(p.tp for p in prfs), fp=sum(p.fp for p in prfs), fn=sum(p.fn for p in prfs)
        )

    strict, lenient, seg = total("events_strict"), total("events_lenient"), total("segmentation")
    det: Counter[str] = Counter()
    tot: Counter[str] = Counter()
    for c in cases:
        det.update(c.issues_detected)
        tot.update(c.issues_total)
    attempted = [k.value for k in ATTEMPTED_ISSUES]
    att_tot = sum(tot[k] for k in attempted)
    by_kind = {
        k: (f"{det[k]}/{tot[k]}" if k in attempted else f"not attempted (0/{tot[k]})")
        for k in sorted(tot)
    }
    type_counts: dict[str, list[float]] = {}
    for c in cases:
        for t, r in c.per_type_recall.items():
            type_counts.setdefault(t, []).append(r)
    checked = sum(c.citations_checked for c in cases)
    return Aggregate(
        cases=len(cases),
        event_precision=strict.precision,
        event_recall=strict.recall,
        event_f1=strict.f1,
        event_f1_lenient=lenient.f1,
        segmentation_f1=seg.f1,
        doc_type_accuracy=sum(c.doc_type_accuracy for c in cases) / len(cases) if cases else 0.0,
        citation_validity=(
            sum(c.citation_validity * c.citations_checked for c in cases) / checked
            if checked
            else 1.0
        ),
        issue_recall_attempted=(sum(det[k] for k in attempted) / att_tot) if att_tot else 0.0,
        issue_recall_by_kind=by_kind,
        per_type_recall={t: sum(v) / len(v) for t, v in sorted(type_counts.items())},
    )


def iso_today() -> str:
    return dt.date.today().isoformat()
