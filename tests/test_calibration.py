"""Verifier calibration tooling: gold-derived seed set, sampling, judging and scoring."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from medchron.cli import main
from medchron.evals import calibration as cal
from medchron.evals.oracle import OracleJudge, OracleProvider
from medchron.orchestrator.pipeline import RunConfig, run_case
from medchron.synth.models import CaseGold

SMOKE = Path(__file__).resolve().parents[1] / "data" / "synthetic"


def test_gold_seed_labels_are_marked_gold_derived() -> None:
    records = cal.gold_seed_claims(SMOKE)
    manifest = json.loads((SMOKE / "manifest.json").read_text())
    planted = sum(len(m["issues"]) for m in manifest)
    own = [r for r in records if r.claim_id.endswith(":own")]
    unrelated = [r for r in records if r.claim_id.endswith(":unrelated")]
    assert len(own) == planted and unrelated
    assert all(r.label_source == "gold-derived" for r in records)
    assert {r.label for r in own} == {"supported"}
    assert {r.label for r in unrelated} == {"unsupported"}
    assert all(r.evidence and all(e.text for e in r.evidence) for r in records)
    # Unrelated evidence never comes from a page of any planted issue.
    for r in unrelated:
        gold = CaseGold.model_validate_json((SMOKE / f"{r.case_id}.gold.json").read_text())
        issue_pages = {p for i in gold.issues for p in i.pages}
        assert not {e.page for e in r.evidence} & issue_pages


def test_agreement_math() -> None:
    a = cal.agreement(
        [
            ("supported", "supported"),
            ("supported", "supported"),
            ("unsupported", "unsupported"),
            ("unsupported", "supported"),
        ]
    )
    assert a.accuracy == 0.75
    assert a.kappa == pytest.approx(0.5)  # p_o 0.75, p_e (2*3 + 2*1)/16 = 0.5
    assert a.per_class["supported"].precision == pytest.approx(2 / 3)
    assert a.per_class["unsupported"].recall == 0.5
    assert a.confusion["unsupported"]["supported"] == 1
    assert a.per_class["partial"].precision is None


def test_scores_are_reported_per_label_source() -> None:
    def rec(i: int, label: str, verdict: str, source: str) -> cal.ClaimRecord:
        return cal.ClaimRecord(
            claim_id=str(i),
            case_id="c",
            origin="gold-seed",
            claim="Is it documented?",
            evidence=[cal.Evidence(page=1, text="t")],
            judge=cal.JudgeVerdict(verdict=verdict, provider="anthropic"),
            label=label,
            label_source=source,
        )

    records = [
        rec(1, "supported", "supported", "gold-derived"),
        rec(2, "unsupported", "supported", "gold-derived"),
        rec(3, "partial", "partial", "human"),
    ]
    report = cal.score(records)
    assert set(report.by_source) == {"gold-derived", "human"}
    assert report.by_source["human"].accuracy == 1.0
    assert report.by_source["gold-derived"].accuracy == 0.5
    text = cal.render(report, judges={"anthropic"})
    assert "No human labels yet" not in text and "Tooling self-check" not in text


def test_oracle_judge_self_check_is_labelled(tmp_path: Path) -> None:
    records = cal.judge_records(cal.gold_seed_claims(SMOKE), OracleJudge())
    assert all(r.judge and r.judge.verdict == "supported" for r in records)
    report = cal.score(records)
    assert set(report.by_source) == {"gold-derived"}
    supported = sum(r.label == "supported" for r in records)
    assert report.by_source["gold-derived"].accuracy == pytest.approx(supported / len(records))
    text = cal.render(report, judges={"oracle"})
    assert "No human labels yet" in text and "Tooling self-check" in text


def test_sample_claims_from_a_run(tmp_path: Path) -> None:
    cid = "case_0003"
    gold = CaseGold.model_validate_json((SMOKE / f"{cid}.gold.json").read_text())
    run_dir = tmp_path / "runs"
    run_case(SMOKE / f"{cid}.pdf", cid, OracleProvider(gold), RunConfig(out_dir=run_dir / cid))
    records = cal.sample_claims(run_dir, n=3, seed=1)
    assert len(records) == 3
    assert all(r.label is None and r.label_source is None for r in records)
    assert all(r.judge and r.judge.verdict == "supported" for r in records)
    assert all(r.evidence and r.evidence[0].span_id.startswith(f"{cid}:p") for r in records)


def test_cli_round_trip(tmp_path: Path) -> None:
    seed = tmp_path / "seed.jsonl"
    assert main(["calib", "seed", "--data", str(SMOKE), "--out", str(seed)]) == 0
    judged = tmp_path / "judged.jsonl"
    assert main(["calib", "judge", str(seed), "--provider", "oracle", "--out", str(judged)]) == 0
    md = tmp_path / "score.md"
    assert main(["calib", "score", str(judged), "--out", str(md)]) == 0
    assert "Judge vs gold-derived labels" in md.read_text()
    assert len(cal.read_jsonl(judged)) == len(cal.read_jsonl(seed))
