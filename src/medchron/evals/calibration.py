"""Verifier calibration: measure how often the LLM judge agrees with labels.

The verifier is an LLM judge. Before its "supported" rate means anything, its
agreement with independent labels has to be measured. This module provides the
tooling for that:

- ``sample_claims``: pull judged claims from pipeline runs into a JSONL file
  with blank labels, for people to label by hand.
- ``gold_seed_claims``: build claims whose labels are known **by construction**
  from the synthetic gold. A planted issue's description shown with evidence
  from its own pages is ``supported``. The same description shown with evidence
  from an unrelated page is ``unsupported``. These labels are tagged
  ``gold-derived``. They are **not** human judgements and are never reported
  as such.
- ``judge_records``: run the verifier over a labelling file.
- ``score``: accuracy, per-class precision and recall, a confusion matrix and
  Cohen's kappa, reported separately for each label source.

Label sources: ``human`` (a person labelled the claim) or ``gold-derived``.
"""

from __future__ import annotations

import json
import random
from collections import Counter
from pathlib import Path

from pydantic import BaseModel, Field

from medchron.agents.verifier import VERDICTS, judge_claims
from medchron.chronology.models import Issue
from medchron.ingestion.models import IngestedCase
from medchron.llm.config import model_for
from medchron.llm.prompts import load_prompt
from medchron.llm.providers import LLMProvider
from medchron.synth.models import CaseGold

LABEL_SOURCES = ("human", "gold-derived")
EVIDENCE_CHARS = 2000


class Evidence(BaseModel):
    page: int
    span_id: str = ""
    text: str


class JudgeVerdict(BaseModel):
    verdict: str
    reason: str = ""
    provider: str = ""
    model: str = ""
    prompt: str = ""


class ClaimRecord(BaseModel):
    claim_id: str
    case_id: str
    origin: str = Field(description="'run:<label>' for sampled claims, 'gold-seed' otherwise")
    claim: str
    evidence: list[Evidence]
    judge: JudgeVerdict | None = None
    label: str | None = Field(default=None, description="supported | partial | unsupported")
    label_source: str | None = Field(default=None, description="human | gold-derived")
    labeler: str | None = None
    notes: str = ""

    def evidence_text(self) -> str:
        """The evidence exactly as the verifier sees it in a pipeline run."""
        return "\n".join(f"[p{e.page}] {e.text}" for e in self.evidence)


# ---------------------------------------------------------------------- files
def read_jsonl(path: Path) -> list[ClaimRecord]:
    return [
        ClaimRecord.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def write_jsonl(records: list[ClaimRecord], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(r.model_dump_json(exclude_none=False) + "\n" for r in records), encoding="utf-8"
    )


# ---------------------------------------------------------------------- sampling
def _load_ingested(case_dir: Path) -> IngestedCase | None:
    found = sorted((case_dir / "checkpoints" / "case").glob("*.json"))
    if not found:
        return None
    return IngestedCase.model_validate_json(found[-1].read_text(encoding="utf-8"))


def _issue_evidence(case: IngestedCase | None, issue: Issue) -> list[Evidence]:
    spans = {s.span_id: s for s in case.spans} if case else {}
    return [
        Evidence(
            page=c.page_no,
            span_id=c.span_id,
            text=spans[c.span_id].text if c.span_id in spans else c.quote,
        )
        for c in issue.citations
    ]


def sample_claims(run_dir: Path, n: int, *, seed: int = 0) -> list[ClaimRecord]:
    """Sample judged analyst issues from a run directory, stratified by verdict.

    ``run_dir`` is an eval work directory (``runs/evals/<label>``) or any folder
    of per-case pipeline outputs with ``issues.json`` and the ingest checkpoint.
    Labels are left blank for a person to fill in.
    """
    by_verdict: dict[str, list[ClaimRecord]] = {}
    for issues_file in sorted(run_dir.glob("*/issues.json")):
        case_dir = issues_file.parent
        case = _load_ingested(case_dir)
        for raw in json.loads(issues_file.read_text(encoding="utf-8")):
            issue = Issue.model_validate(raw)
            rec = ClaimRecord(
                claim_id=f"{case_dir.name}:{issue.issue_id}",
                case_id=case_dir.name,
                origin=f"run:{run_dir.name}",
                claim=issue.description,
                evidence=_issue_evidence(case, issue),
                judge=JudgeVerdict(
                    verdict=issue.verdict, reason=issue.verdict_reason, provider="recorded run"
                ),
            )
            by_verdict.setdefault(issue.verdict, []).append(rec)
    rng = random.Random(seed)
    for group in by_verdict.values():
        rng.shuffle(group)
    out: list[ClaimRecord] = []
    pools = [by_verdict[k] for k in sorted(by_verdict)]
    while len(out) < n and any(pools):  # round-robin across verdicts
        for pool in pools:
            if pool and len(out) < n:
                out.append(pool.pop())
    return out


def _page_body(gold: CaseGold, page: int) -> str:
    """Page text without the letterhead (everything after the rule line), clipped."""
    lines = gold.pages[page - 1].text.splitlines()
    start = next((i + 1 for i, ln in enumerate(lines) if set(ln.strip()) == {"-"}), 0)
    body = "\n".join(ln.rstrip() for ln in lines[start:] if ln.strip())
    return body[:EVIDENCE_CHARS]


def gold_seed_claims(data_dir: Path, *, seed: int = 0) -> list[ClaimRecord]:
    """Claims with labels known by construction from the synthetic gold (``gold-derived``)."""
    manifest = json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))
    rng = random.Random(seed)
    out: list[ClaimRecord] = []
    for m in manifest:
        gold = CaseGold.model_validate_json(
            (data_dir / f"{m['case_id']}.gold.json").read_text(encoding="utf-8")
        )
        issue_pages = {p for i in gold.issues for p in i.pages}
        page_doc = {p.page: p.doc_id for p in gold.pages}
        for issue in gold.issues:
            own_docs = {page_doc[p] for p in issue.pages}
            unrelated = [
                p.page
                for p in gold.pages
                if p.page not in issue_pages
                and p.duplicate_of is None
                and p.doc_id not in own_docs
                and _page_body(gold, p.page)
            ]
            base = f"{gold.case_id}:{issue.issue_id}"
            out.append(
                ClaimRecord(
                    claim_id=f"{base}:own",
                    case_id=gold.case_id,
                    origin="gold-seed",
                    claim=issue.description,
                    evidence=[Evidence(page=p, text=_page_body(gold, p)) for p in issue.pages],
                    label="supported",
                    label_source="gold-derived",
                    notes=f"planted {issue.kind.value}; evidence from the issue's own pages",
                )
            )
            if unrelated:
                page = rng.choice(unrelated)
                out.append(
                    ClaimRecord(
                        claim_id=f"{base}:unrelated",
                        case_id=gold.case_id,
                        origin="gold-seed",
                        claim=issue.description,
                        evidence=[Evidence(page=page, text=_page_body(gold, page))],
                        label="unsupported",
                        label_source="gold-derived",
                        notes=f"planted {issue.kind.value}; evidence from an unrelated page",
                    )
                )
    return out


# ---------------------------------------------------------------------- judging
def judge_records(records: list[ClaimRecord], provider: LLMProvider) -> list[ClaimRecord]:
    """Fill ``judge`` on every record by running the verifier on its claim and evidence."""
    verdicts = judge_claims(provider, [(r.claim, r.evidence_text()) for r in records])
    prompt = load_prompt("verify_claims").id
    model = "oracle" if provider.name == "oracle" else model_for("verify")
    out = []
    for r, v in zip(records, verdicts, strict=True):
        judged = r.model_copy(deep=True)
        judged.judge = (
            JudgeVerdict(
                verdict=v[0], reason=v[1], provider=provider.name, model=model, prompt=prompt
            )
            if v
            else None
        )
        out.append(judged)
    return out


# ---------------------------------------------------------------------- scoring
class ClassStats(BaseModel):
    precision: float | None
    recall: float | None
    support: int


class Agreement(BaseModel):
    n: int
    accuracy: float | None
    kappa: float | None
    per_class: dict[str, ClassStats]
    confusion: dict[str, dict[str, int]] = Field(description="label -> judge verdict -> count")


class CalibrationReport(BaseModel):
    records: int
    labelled: int
    judged: int
    by_source: dict[str, Agreement]


def agreement(pairs: list[tuple[str, str]]) -> Agreement:
    """Agreement between ``(label, judge verdict)`` pairs."""
    n = len(pairs)
    confusion = {lab: dict.fromkeys(VERDICTS, 0) for lab in VERDICTS}
    for lab, jud in pairs:
        confusion.setdefault(lab, dict.fromkeys(VERDICTS, 0))
        confusion[lab][jud] = confusion[lab].get(jud, 0) + 1
    correct = sum(lab == jud for lab, jud in pairs)
    per_class = {}
    for cls in VERDICTS:
        tp = sum(lab == cls and jud == cls for lab, jud in pairs)
        predicted = sum(jud == cls for _, jud in pairs)
        actual = sum(lab == cls for lab, _ in pairs)
        per_class[cls] = ClassStats(
            precision=tp / predicted if predicted else None,
            recall=tp / actual if actual else None,
            support=actual,
        )
    kappa = None
    if n:
        p_o = correct / n
        lab_c = Counter(lab for lab, _ in pairs)
        jud_c = Counter(jud for _, jud in pairs)
        p_e = sum(lab_c[c] * jud_c[c] for c in set(lab_c) | set(jud_c)) / (n * n)
        kappa = (p_o - p_e) / (1 - p_e) if p_e < 1 else None
    return Agreement(
        n=n,
        accuracy=correct / n if n else None,
        kappa=kappa,
        per_class=per_class,
        confusion=confusion,
    )


def score(records: list[ClaimRecord]) -> CalibrationReport:
    by_source: dict[str, list[tuple[str, str]]] = {}
    for r in records:
        if r.label and r.judge and r.label_source:
            by_source.setdefault(r.label_source, []).append((r.label, r.judge.verdict))
    return CalibrationReport(
        records=len(records),
        labelled=sum(r.label is not None for r in records),
        judged=sum(r.judge is not None for r in records),
        by_source={src: agreement(pairs) for src, pairs in sorted(by_source.items())},
    )


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}%"


def render(report: CalibrationReport, *, judges: set[str] | None = None) -> str:
    lines = [
        f"{report.records} claims, {report.labelled} labelled, {report.judged} judged.",
        "",
    ]
    if "human" not in report.by_source:
        lines += [
            "**No human labels yet.** Judge-human agreement has not been measured, so the "
            "verifier's supported rate is not yet calibrated.",
            "",
        ]
    if judges and judges == {"oracle"}:
        lines += [
            "> **Tooling self-check.** The judge here is the oracle test double, which always "
            "answers `supported`. These numbers only show that the scorer works. They say "
            "nothing about the real verifier.",
            "",
        ]
    for src, a in report.by_source.items():
        title = "gold-derived labels (by construction, not human)" if src != "human" else src
        lines += [
            f"### Judge vs {title}",
            "",
            f"n = {a.n}, accuracy {_pct(a.accuracy)}, Cohen's kappa "
            f"{'n/a' if a.kappa is None else f'{a.kappa:.2f}'}",
            "",
            "| Class | Precision | Recall | Support |",
            "|---|---|---|---|",
            *(
                f"| {c} | {_pct(s.precision)} | {_pct(s.recall)} | {s.support} |"
                for c, s in a.per_class.items()
            ),
            "",
            "Confusion matrix (rows = label, columns = judge):",
            "",
            "| label \\ judge | " + " | ".join(VERDICTS) + " |",
            "|---|" + "---|" * len(VERDICTS),
            *(
                f"| {lab} | " + " | ".join(str(row.get(j, 0)) for j in VERDICTS) + " |"
                for lab, row in a.confusion.items()
            ),
            "",
        ]
    return "\n".join(lines).rstrip() + "\n"
