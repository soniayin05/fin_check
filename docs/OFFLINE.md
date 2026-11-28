# Offline numeric checks and saved reports

Start from the repository folder:

```bash
source .venv/bin/activate
python3 -m fincheck serve
```

Open http://127.0.0.1:8765 and use **Check a number offline**. Restart an older
server before using the new interface. No Gemini key, model construction or
network service is needed for this form or for saved-report viewing.

Enter a ticker, fiscal year, operation, measure, literal claimed value and unit.
The form uses the same audited Python calculations as the agent. Values remain
strings so written precision is preserved: 245.1 and 245.10000 have different
rounding tolerances. Unit conversion is explicit. Company/year fields are
entered by the user; this mode does not parse or review natural-language meaning.
It checks annual, company-wide reported values, percentage growth and supported
ratios. Segment, forecast, adjusted/non-GAAP and compound statements need separate
review. A malformed or incompatible input is an input error, not a factual verdict.

## Try these cases

| Company | Year | Check type | Measure | Claimed value | Unit | Expected |
| --- | --- | --- | --- | --- | --- | --- |
| MSFT | 2024 | Reported annual value | revenue | 245.1 | USD (billions) | Supported; verified 245.122 |
| MSFT | 2024 | Reported annual value | revenue | 250 | USD (billions) | Contradicted; verified 245.122 |
| MSFT | 2030 | Reported annual value | revenue | 250 | USD (billions) | Insufficient evidence in the loaded snapshot |
| MSFT | 2024 | Year-over-year growth | revenue | 15.7 | Percent | Supported; approximately 15.669962% |
| MSFT | 2024 | Financial ratio | Operating profit margin | 44.6 | Percent | Supported; approximately 44.644300% |

The ratios dropdown uses human-readable labels. Some definitions need inputs
absent from a particular company/year. Missing or approximate inputs retain
their existing insufficient-evidence/assumption rules; a definition being in
the dropdown does not establish complete data coverage.

Each check creates an ignored `artifacts/local/offline-check-<UTC timestamp>/`
folder containing JSON and HTML. The record includes exact inputs, formula,
assumptions, observations, source URLs/IDs, data/coverage hashes, calculation code
hashes, timing and zero model calls. A completed check can legitimately return
insufficient_evidence. It is saved as offline_numeric and is never reported as
an LLM or multi-agent execution. This adds no agent-accuracy or feature-credit claim.

## Saved reports

The **Saved reports** panel lists the latest 100 valid numeric and memo reports
stored in this checkout. Opening one does not extract, verify or rerun it. It
continues to work after restarting the server. If the selected current facts
snapshot differs, the history entry shows a data-change notice. New offline
reports compare both facts and coverage hashes; older memo reports provide the
facts hash only. Saved values remain unchanged. Raw benchmark traces remain
accessible through the existing trace commands rather than this report list.

The history endpoints use the existing local session token. A reopened report
gets its own asset capability. Linked memo page folders must remain inside
`artifacts/local/` and match the original memo PDF hash. Paths outside that
directory and outward symlinks are rejected. CLI reports whose source-page
folders are outside the local artifact tree cannot expose those pages through
this server. The existing 20-session limit still applies; restart the app if it
is reached. Saved files persist across restarts.

The PDF upload/extraction and agent-checking workflow remains available separately
and still requires Gemini. The page does not start those operations automatically.

## Validation

Eight new offline contract/integration tests cover real cached calculations,
growth/ratios/precision, invalid inputs, history after restart, data changes,
original PDF hash boundaries, path/symlink restrictions, HTML escaping and HTTP
authentication. They fail if Gemini is constructed or urlopen is used.

The full suite passes 145 tests; the three original deterministic checks pass.
An isolated headless Chrome exercised supported, contradicted, missing-year and
ratio submissions, then reopened history. All four UI outcomes matched; no
external page requests occurred. No live model or embedding calls were made.
