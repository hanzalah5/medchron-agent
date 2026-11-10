"""Render evaluation results as Markdown (evals/REPORT.md)."""

from __future__ import annotations

from medchron.evals.metrics import ERROR_CATEGORIES
from medchron.evals.runner import EvalResult


def _pct(x: float) -> str:
    return f"{100 * x:.1f}%"


def _delta(now: float, before: float | None) -> str:
    if before is None:
        return ""
    d = 100 * (now - before)
    return f" ({'+' if d >= 0 else ''}{d:.1f})" if abs(d) >= 0.05 else " (=)"


def scorecard(r: EvalResult, baseline: EvalResult | None = None) -> list[str]:
    a, b = r.aggregate, (baseline.aggregate if baseline else None)
    rows = [
        ("Event recall (strict)", a.event_recall, b.event_recall if b else None),
        ("Event precision (strict)", a.event_precision, b.event_precision if b else None),
        ("Event F1 (strict)", a.event_f1, b.event_f1 if b else None),
        ("Event F1 (dates ±1 day)", a.event_f1_lenient, b.event_f1_lenient if b else None),
        ("Citation validity", a.citation_validity, b.citation_validity if b else None),
        ("Document segmentation F1", a.segmentation_f1, b.segmentation_f1 if b else None),
        ("Document type accuracy", a.doc_type_accuracy, b.doc_type_accuracy if b else None),
        (
            "Planted-issue recall (attempted kinds)",
            a.issue_recall_attempted,
            b.issue_recall_attempted if b else None,
        ),
    ]
    if a.issue_precision is not None:
        rows.append(
            (
                "Planted-issue precision (all raised signals)",
                a.issue_precision,
                b.issue_precision if b else None,
            )
        )
    optional = [
        ("Analyst issues supported by verifier", a.analyst_supported_rate),
        ("Memo citation validity", a.memo_citation_validity),
        ("Memo sentences with a citation", a.memo_cited_ratio),
    ]
    lines = ["| Metric | Value |", "|---|---|"]
    lines += [
        f"| {name} | {_pct(v)}{_delta(v, bv)} |" if v is not None else f"| {name} | n/a |"
        for name, v, bv in rows
    ]
    lines += [f"| {name} | {_pct(v)} |" for name, v in optional if v is not None]
    lines += [
        f"| Conclusory-language violations | {a.scope_violations} |",
        f"| Cases where output followed the planted injection | {a.injection_followed_cases} |",
    ]
    return lines


EXAMPLES_PER_CATEGORY = 3
ITEM_LABELS = {
    "missed_event": "missed event",
    "spurious_event": "unmatched event",
    "missed_issue": "missed issue",
    "spurious_issue": "unmatched issue signal",
    "output": "output",
}


def issues_by_kind(r: EvalResult) -> list[str]:
    """Recall and precision per planted-issue kind."""
    a = r.aggregate
    kinds = sorted(set(a.issue_recall_by_kind) | set(a.issue_precision_by_kind))
    lines = [
        "| Issue kind | Recall (detected/planted) | Precision (matched/raised) |",
        "|---|---|---|",
    ]
    lines += [
        f"| {k} | {a.issue_recall_by_kind.get(k, '-')} | {a.issue_precision_by_kind.get(k, '-')} |"
        for k in kinds
    ]
    if a.issue_duplicates:
        lines += [
            "",
            f"{a.issue_duplicates} raised signal(s) repeated an issue another signal had "
            "already found. They count as matched, not as false positives.",
        ]
    return lines


def error_analysis(r: EvalResult) -> list[str]:
    """Error counts per category with up to three examples each."""
    errors = [e for c in r.cases for e in c.errors]
    if not errors:
        return []
    lines = [
        "**Error analysis.** Every miss and unmatched prediction is put in one category "
        "(rules in `evals/metrics.py::categorize_errors`).",
        "",
        "| Category | Missed events | Unmatched events | Issue misses / unmatched "
        "| Other output | Total |",
        "|---|---|---|---|---|---|",
    ]
    for cat in ERROR_CATEGORIES:
        rows = [e for e in errors if e.category == cat]
        if not rows:
            continue
        n = {i: sum(e.item == i for e in rows) for i in ITEM_LABELS}
        lines.append(
            f"| {cat} | {n['missed_event']} | {n['spurious_event']} | "
            f"{n['missed_issue'] + n['spurious_issue']} | {n['output']} | {len(rows)} |"
        )
    lines += ["", "<details><summary>Examples (up to 3 per category)</summary>", ""]
    for cat in ERROR_CATEGORIES:
        rows = [e for e in errors if e.category == cat][:EXAMPLES_PER_CATEGORY]
        for e in rows:
            pages = f" p{','.join(map(str, e.pages))}" if e.pages else ""
            lines.append(f"- **{cat}**, {ITEM_LABELS[e.item]} `{e.case_id}`{pages}: {e.text}")
    lines += ["", "</details>", ""]
    return lines


def render(
    r: EvalResult, baseline: EvalResult | None = None, *, extra: list[EvalResult] | None = None
) -> str:
    o = r.operational
    head = (
        "> **Harness self-check, not model performance.** These numbers come from the "
        "gold-label *oracle* test double. They show that the pipeline plumbing and metric "
        "code work, and they set an upper bound under OCR and merge limits."
        if not r.is_model_result
        else f"> Live model evaluation using provider `{r.provider}`."
    )
    lines = [
        f"## {r.label}",
        "",
        head,
        "",
        f"- Created {r.created} at commit `{r.git_sha}`. Dataset `{r.dataset}` "
        f"(`{r.dataset_sha256}`), {r.aggregate.cases} cases, {o.pages} pages",
        f"- Models: {', '.join(f'{k}={v} ({r.efforts[k]})' for k, v in r.models.items())}",
        f"- Prompts: {', '.join(f'`{p}`' for p in r.prompts)}",
        "",
        *scorecard(r, baseline),
        "",
        *issues_by_kind(r),
        "",
        "**Event recall by type:** "
        + ", ".join(f"{k} {_pct(v)}" for k, v in r.aggregate.per_type_recall.items()),
        "",
        f"**Operational:** {o.llm_calls} LLM calls, ${o.cost_usd:.4f} total "
        f"(${o.cost_per_100_pages_usd:.4f} per 100 pages), {o.input_tokens:,} input / "
        f"{o.output_tokens:,} output / {o.cache_read_input_tokens:,} cache-read tokens, "
        f"{o.rejected_events} events rejected by grounding, case time p50 {o.seconds_p50}s "
        f"/ max {o.seconds_max}s.",
        "",
    ]
    if any(c.errors for c in r.cases):
        lines += error_analysis(r)
    else:  # results files written before v1.1 have uncategorised examples only
        misses = [(c.case_id, m) for c in r.cases for m in c.missed_events][:8]
        spurious = [(c.case_id, s) for c in r.cases for s in c.spurious_events][:8]
        if misses or spurious:
            lines += ["<details><summary>Error analysis (first examples)</summary>", ""]
            lines += [f"- missed `{cid}`: {m}" for cid, m in misses]
            lines += [f"- unmatched prediction `{cid}`: {s}" for cid, s in spurious]
            lines += ["", "</details>", ""]
    for x in extra or []:
        lines += ["", *render(x).splitlines()]
    return "\n".join(lines)


def render_report(results: list[EvalResult]) -> str:
    """Full REPORT.md: live model results first, then harness self-checks."""
    model_runs = [r for r in results if r.is_model_result]
    checks = [r for r in results if not r.is_model_result]
    lines = [
        "# Evaluation report",
        "",
        "Generated by `medchron eval-report`. The methodology is in "
        "[docs/eval-methodology.md](../docs/eval-methodology.md). Raw results are in "
        "[`evals/results/`](results/).",
        "",
        "# Live model results",
        "",
    ]
    if model_runs:
        for i, r in enumerate(model_runs):
            base = model_runs[i + 1] if i + 1 < len(model_runs) else None
            lines += [render(r, base), ""]
    else:
        lines += [
            "*No live model evaluation has been run yet.* Run `make eval-live` with "
            "`ANTHROPIC_API_KEY` set, then commit the results file and regenerate this report "
            "(`make eval-report`). No model numbers are published until they have been measured.",
            "",
        ]
    lines += [
        "# Verifier calibration",
        "",
        "*Not measured yet.* The verifier is an LLM judge, and its agreement with human labels "
        'has not been measured, so the "Analyst issues supported by verifier" rate is '
        "uncalibrated. The tooling (`medchron calib sample | seed | judge | score`), the "
        "hand-labelling protocol and a gold-derived seed set are in "
        "[`evals/calibration/`](calibration/).",
        "",
    ]
    if checks:
        lines += [
            "# Harness self-checks (oracle test double)",
            "",
            "The oracle answers from gold labels, so these runs test the pipeline plumbing and "
            "the metric code, not a model. The clean run should score at or near the ceiling. "
            "The noisy run injects missed events, date shifts and altered quotes, and shows "
            "that the metrics and the grounding validator respond. **Oracle limits:** it only "
            "emits gold events and copies evidence lines mechanically, so the issue and memo "
            "numbers show that the plumbing works (grounding, verification, scope and "
            "injection checks), not that the analysis is good.",
            "",
        ]
        for r in checks:
            lines += [render(r), ""]
    return "\n".join(lines).rstrip() + "\n"
