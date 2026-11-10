56 claims, 56 labelled, 56 judged.

**No human labels yet.** Judge-human agreement has not been measured, so the verifier's supported rate is not yet calibrated.

> **Tooling self-check.** The judge here is the oracle test double, which always answers `supported`. These numbers only show that the scorer works. They say nothing about the real verifier.

### Judge vs gold-derived labels (by construction, not human)

n = 56, accuracy 50.0%, Cohen's kappa 0.00

| Class | Precision | Recall | Support |
|---|---|---|---|
| supported | 50.0% | 100.0% | 28 |
| partial | n/a | n/a | 0 |
| unsupported | n/a | 0.0% | 28 |

Confusion matrix (rows = label, columns = judge):

| label \ judge | supported | partial | unsupported |
|---|---|---|---|
| supported | 28 | 0 | 0 |
| partial | 0 | 0 | 0 |
| unsupported | 28 | 0 | 0 |
