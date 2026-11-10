"""Generate synthetic benchmark cases (PDFs + gold JSON).

Seeds are split so prompt tuning never sees reported cases: ``dev`` seeds are
1-999 and ``test`` seeds are 1000 and up. A case's scenario and hazards are a
pure function of its seed.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from pathlib import Path

from medchron.synth.builder import CaseBuilder
from medchron.synth.layout import assemble
from medchron.synth.models import CaseGold
from medchron.synth.render import render_pdf
from medchron.synth.scenarios import SCENARIOS, ScenarioOptions

SPLITS: dict[str, range] = {"dev": range(1, 1000), "test": range(1000, 1_000_000)}


def split_of(seed: int) -> str:
    return next(name for name, seeds in SPLITS.items() if seed in seeds)


@dataclass(frozen=True)
class GeneratedCase:
    gold: CaseGold
    pdfs: list[bytes]
    hazards: dict[str, object] = field(default_factory=dict)

    @property
    def pdf(self) -> bytes:
        return self.pdfs[0]

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.pdfs[0]).hexdigest()

    @property
    def sha256s(self) -> list[str]:
        return [hashlib.sha256(p).hexdigest() for p in self.pdfs]

    @property
    def filenames(self) -> list[str]:
        cid = self.gold.case_id
        if len(self.pdfs) == 1:
            return [f"{cid}.pdf"]
        return [f"{cid}.p{n}.pdf" for n in range(1, len(self.pdfs) + 1)]


def generate_case(
    seed: int,
    scenario: str | None = None,
    case_id: str | None = None,
    productions: int | None = None,
) -> GeneratedCase:
    """Build one case. The same arguments always give the same output."""
    rng = random.Random(seed)
    names = sorted(SCENARIOS)
    chosen = scenario or names[seed % len(names)]
    if chosen not in SCENARIOS:
        raise ValueError(f"unknown scenario {chosen!r}; choose from {names}")
    options = ScenarioOptions(date_conflict=rng.random() < 0.5, missing_record=rng.random() < 0.6)
    prompt_injection = rng.random() < 0.4
    # Hazards added in generator v0.2.
    copy_forward = rng.random() < 0.6
    misfiled = rng.random() < 0.4
    n_productions = productions or (2 if rng.random() < 0.3 else 1)

    builder = CaseBuilder(rng)
    SCENARIOS[chosen](builder, options)
    copied = builder.copy_forward() if copy_forward else False
    pages, gold = assemble(
        builder,
        rng,
        case_id=case_id or f"case_{seed:04d}",
        seed=seed,
        scenario=chosen,
        prompt_injection=prompt_injection,
        misfiled=misfiled,
        productions=n_productions,
    )
    pdfs = [
        render_pdf([p for p in pages if p.production == n], seed + 7919 * (n - 1))
        for n in range(1, gold.productions + 1)
    ]
    hazards: dict[str, object] = {
        "tables": any(p.preformatted for d in builder.docs for p in d.paras),
        "copy_forward": copied,
        "misfiled_page": any(p.misfiled for p in gold.pages),
        "productions": gold.productions,
        "scans": sorted({p.scan for p in gold.pages if p.scan}),
    }
    return GeneratedCase(gold=gold, pdfs=pdfs, hazards=hazards)


def write_case(case: GeneratedCase, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, data in zip(case.filenames, case.pdfs, strict=True):
        (out_dir / name).write_bytes(data)
        paths.append(out_dir / name)
    gold_path = out_dir / f"{case.gold.case_id}.gold.json"
    gold_path.write_text(case.gold.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return [*paths, gold_path]


def manifest_entry(case: GeneratedCase) -> dict[str, object]:
    g = case.gold
    return {
        "case_id": g.case_id,
        "seed": g.seed,
        "split": split_of(g.seed),
        "scenario": g.scenario,
        "pages": len(g.pages),
        "documents": len(g.documents),
        "events": len(g.events),
        "issues": sorted(i.kind.value for i in g.issues),
        "scanned_pages": sum(not p.text_layer for p in g.pages),
        "duplicate_pages": sum(p.duplicate_of is not None for p in g.pages),
        "productions": g.productions,
        "pdfs": case.filenames,
        "pdf_sha256": case.sha256,
        "pdf_sha256s": case.sha256s,
        "hazards": case.hazards,
    }


def generate_dataset(seeds: list[int], out_dir: Path) -> list[GeneratedCase]:
    cases = [generate_case(s) for s in seeds]
    for case in cases:
        write_case(case, out_dir)
    manifest = [manifest_entry(c) for c in cases]
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return cases
