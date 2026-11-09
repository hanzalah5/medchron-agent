"""Generate synthetic benchmark cases (PDF + gold JSON)."""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from pathlib import Path

from medchron.synth.builder import CaseBuilder
from medchron.synth.layout import assemble
from medchron.synth.models import CaseGold
from medchron.synth.render import render_pdf
from medchron.synth.scenarios import SCENARIOS, ScenarioOptions


@dataclass(frozen=True)
class GeneratedCase:
    gold: CaseGold
    pdf: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.pdf).hexdigest()


def generate_case(
    seed: int, scenario: str | None = None, case_id: str | None = None
) -> GeneratedCase:
    """Build one case. The same ``seed`` and ``scenario`` always give the same output."""
    rng = random.Random(seed)
    names = sorted(SCENARIOS)
    chosen = scenario or names[seed % len(names)]
    if chosen not in SCENARIOS:
        raise ValueError(f"unknown scenario {chosen!r}; choose from {names}")
    options = ScenarioOptions(date_conflict=rng.random() < 0.5, missing_record=rng.random() < 0.6)
    prompt_injection = rng.random() < 0.4

    builder = CaseBuilder(rng)
    SCENARIOS[chosen](builder, options)
    pages, gold = assemble(
        builder,
        rng,
        case_id=case_id or f"case_{seed:04d}",
        seed=seed,
        scenario=chosen,
        prompt_injection=prompt_injection,
    )
    return GeneratedCase(gold=gold, pdf=render_pdf(pages, seed))


def write_case(case: GeneratedCase, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = out_dir / f"{case.gold.case_id}.pdf"
    gold_path = out_dir / f"{case.gold.case_id}.gold.json"
    pdf_path.write_bytes(case.pdf)
    gold_path.write_text(case.gold.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return pdf_path, gold_path


def generate_dataset(seeds: list[int], out_dir: Path) -> list[GeneratedCase]:
    cases = [generate_case(s) for s in seeds]
    manifest = []
    for case in cases:
        write_case(case, out_dir)
        g = case.gold
        manifest.append(
            {
                "case_id": g.case_id,
                "seed": g.seed,
                "scenario": g.scenario,
                "pages": len(g.pages),
                "documents": len(g.documents),
                "events": len(g.events),
                "issues": sorted(i.kind.value for i in g.issues),
                "scanned_pages": sum(not p.text_layer for p in g.pages),
                "duplicate_pages": sum(p.duplicate_of is not None for p in g.pages),
                "pdf_sha256": case.sha256,
            }
        )
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return cases
