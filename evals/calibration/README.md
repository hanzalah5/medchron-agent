# Verifier calibration

The verifier is an LLM judge. It reads one analyst claim with only its cited evidence and returns `supported`, `partial` or `unsupported`. Before its "supported" rate can be trusted, its agreement with independent labels has to be measured. This folder holds the tooling, the labelling protocol and the files.

**Status: no human labels exist yet, and the live judge has not been run.** Judge-human agreement is therefore *not measured*, and no calibration number is published.

## Files

| File | What it is |
|---|---|
| `gold-seed.jsonl` | Claims whose labels are **known by construction** from the synthetic gold (`label_source: "gold-derived"`). A planted issue's description shown with evidence from its own pages is labelled `supported`. The same description shown with evidence from an unrelated page is labelled `unsupported`. These labels are **not human judgements**. They contain no `partial` examples. A small number may be wrong, for example when unrelated evidence happens to bear on the claim. |
| `selfcheck.md` | The scorer run on `gold-seed.jsonl` with the **oracle judge**, which always answers `supported`. It only shows that the scorer works. It says nothing about the real verifier. |

## Commands

```bash
make calib-seed                                     # rebuild gold-seed.jsonl from data/synthetic
make calib-selfcheck                                # oracle judge + scorer (tooling self-check)

# Real calibration (needs ANTHROPIC_API_KEY):
make eval-live                                      # produces runs/evals/live-smoke/*/issues.json
.venv/bin/medchron calib sample --runs runs/evals/live-smoke --n 100 --out evals/calibration/human-batch-1.jsonl
#   ... label the file by hand (protocol below) ...
.venv/bin/medchron calib judge evals/calibration/gold-seed.jsonl --provider anthropic --out runs/calib/live-judged.jsonl
.venv/bin/medchron calib score evals/calibration/human-batch-1.jsonl runs/calib/live-judged.jsonl
```

`calib score` reports accuracy, per-class precision and recall, a confusion matrix and Cohen's kappa **separately for each label source**. Human agreement is never mixed with gold-derived agreement.

## Hand-labelling protocol

1. `calib sample` writes claims with `label`, `label_source` and `labeler` set to `null`. Sampling is stratified across the judge's verdicts, so disagreements are not under-sampled.
2. The labeller reads **only** the claim and its `evidence`, the same view the verifier had. The judge's verdict is in the file but should be hidden or ignored while labelling.
3. Set `label` to one of these values:
   - `supported`: the evidence states what the claim says
   - `partial`: the evidence supports part of it, or the claim overstates it
   - `unsupported`: the evidence does not support the claim
4. Set `label_source` to `"human"`, set `labeler` to the labeller's initials, and add `notes` for borderline cases.
5. Two labellers on an overlapping subset (for example 20 claims) give an inter-annotator baseline. If people disagree often, the judge's ceiling is lower than 100%.
6. Commit the labelled file and the `calib score` output. Report the numbers with *n*, and never round small samples into broad claims.
