"""Run the pipeline over a dataset and record reproducible evaluation results."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import subprocess
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, Field

from medchron.evals.metrics import Aggregate, CaseMetrics, aggregate, score_case
from medchron.llm.config import effort_for, model_for
from medchron.llm.prompts import load_prompt
from medchron.llm.providers import LLMProvider
from medchron.orchestrator.pipeline import RunConfig, RunSummary, run_case
from medchron.synth.models import CaseGold

ROUTES = ["classify", "extract"]
PROMPTS = ["system_base", "classify_pages", "extract_events"]


class Operational(BaseModel):
    llm_calls: int
    cost_usd: float
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    rejected_events: int
    seconds_p50: float
    seconds_max: float
    pages: int
    cost_per_100_pages_usd: float


class EvalResult(BaseModel):
    label: str
    created: str
    git_sha: str
    provider: str
    is_model_result: bool = Field(description="False for oracle/test-double runs")
    models: dict[str, str]
    efforts: dict[str, str]
    prompts: list[str]
    dataset: str
    dataset_sha256: str
    aggregate: Aggregate
    operational: Operational
    cases: list[CaseMetrics]
    runs: list[RunSummary]


def _git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        )
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        return out.stdout.strip() + ("-dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def run_eval(
    data_dir: Path,
    provider_for: Callable[[CaseGold], LLMProvider],
    *,
    label: str,
    provider_name: str,
    work_dir: Path,
    budget_per_case: float | None = None,
    case_ids: list[str] | None = None,
) -> EvalResult:
    manifest = json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))
    if case_ids:
        manifest = [m for m in manifest if m["case_id"] in case_ids]
    metrics: list[CaseMetrics] = []
    runs: list[RunSummary] = []
    pages = 0
    for m in manifest:
        cid = m["case_id"]
        gold = CaseGold.model_validate_json((data_dir / f"{cid}.gold.json").read_text())
        out = work_dir / cid
        result = run_case(
            data_dir / f"{cid}.pdf",
            cid,
            provider_for(gold),
            RunConfig(out_dir=out, budget_usd=budget_per_case),
        )
        metrics.append(score_case(result.chronology, gold, result.case))
        runs.append(result.summary)
        pages += len(gold.pages)

    secs = sorted(r.seconds for r in runs)
    cost = sum(r.cost_usd for r in runs)
    return EvalResult(
        label=label,
        created=dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat(),
        git_sha=_git_sha(),
        provider=provider_name,
        is_model_result=provider_name not in {"oracle"},
        models={r: model_for(r) for r in ROUTES},
        efforts={r: effort_for(r) for r in ROUTES},
        prompts=[load_prompt(p).id for p in PROMPTS],
        dataset=str(data_dir),
        dataset_sha256=hashlib.sha256(
            json.dumps([m["pdf_sha256"] for m in manifest]).encode()
        ).hexdigest()[:16],
        aggregate=aggregate(metrics),
        operational=Operational(
            llm_calls=sum(r.llm_calls for r in runs),
            cost_usd=round(cost, 4),
            input_tokens=sum(r.input_tokens for r in runs),
            output_tokens=sum(r.output_tokens for r in runs),
            cache_read_input_tokens=sum(r.cache_read_input_tokens for r in runs),
            rejected_events=sum(r.rejected_events for r in runs),
            seconds_p50=secs[len(secs) // 2] if secs else 0.0,
            seconds_max=secs[-1] if secs else 0.0,
            pages=pages,
            cost_per_100_pages_usd=round(100 * cost / pages, 4) if pages else 0.0,
        ),
        cases=metrics,
        runs=runs,
    )


GATED = ["event_recall", "event_precision", "citation_validity", "segmentation_f1"]


def regressions(current: EvalResult, baseline: EvalResult, tolerance: float) -> list[str]:
    """Metrics that dropped by more than ``tolerance`` against the baseline."""
    out = []
    for name in GATED:
        now = getattr(current.aggregate, name)
        before = getattr(baseline.aggregate, name)
        if now < before - tolerance:
            out.append(f"{name}: {before:.3f} -> {now:.3f}")
    return out
