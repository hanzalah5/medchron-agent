# Replay fixtures (recorded from the oracle, not from a model)

These files are **recorded responses of the gold-label oracle**, the test double in `src/medchron/evals/oracle.py`. They are **not** Claude output, and nothing in them is a model result.

They let CI run the whole pipeline through the replay provider and the batch extraction code path, with no API key. The case is `data/synthetic/dev/case_0005`.

Each file is keyed by the SHA-256 fingerprint of the request (`<route>/<fingerprint>.json`). Any change to a prompt, a tool schema or the request content changes the fingerprints. When that happens, re-record with:

```bash
make fixtures
```

To record real model responses instead (for example for offline demos), run `medchron run ... --provider anthropic --record --fixtures <dir>`. That needs `ANTHROPIC_API_KEY`. Keep those files out of this folder, so recorded model output is never mixed up with oracle output.
