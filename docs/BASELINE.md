# Baseline and shared claim evaluation

The fixed baseline selects one evidence operation (calculate, compare_growth,
or search_filings), executes it, then makes one final-verdict call. It has two
model stages and no adaptive retrieval or repair loop. Numerical verdicts use
the same Python calculation/citation controller as the agent. Disclosure
checks use five full passages from the configured retriever and model judgment.

The agent retains its plan/tool loop. Both systems receive only the claim,
use the same model, data and retriever, and run under the same maximum model
and tool budgets. The baseline normally uses fewer calls. Equal ceilings do
not mean equal actual spending; reports retain call counts and timings.

## Run

Use the active repository's local `.env`. Configure GEMINI_API_KEY and the
account's available GEMINI_MODEL. Existing source data must be prepared with
`python3 -m fincheck prepare-data --include-sec` on a fresh clone. The numeric
snapshot is committed; the passage index is local and ignored.

```bash
python3 -m fincheck compare-systems --dry-run --method keyword
python3 -m fincheck compare-systems --only s4-001,s4-010 --method keyword
python3 -m fincheck compare-systems --method keyword
```

The dry run validates case IDs, gold structure and source availability without
model calls. It reports scheduled system runs, not a forecast of API requests.
The two-case smoke uses four system runs. The full default batch uses 24.
Model access/quota are needed for live runs; no scripted substitute is used.

Twelve draft development claims live in `evals/dev/claims_stage4.jsonl`.
They cover fact equality, rounded claims, growth, margin, missing years,
segment disclosures, employee counts and dividend declarations. They derive
from reviewed source observations/passages, but these new claim variants and
their grading rules still require user review before a frozen evaluation.
The original three sample-based cases remain unchanged. `compare-systems`
uses SEC facts by default when available; `--facts` overrides that choice.
Do not run the old sample-specific FY2025 missing-data gold on SEC data.

To choose repetitions before running:

```bash
python3 -m fincheck compare-systems --method keyword --repeats 3 --repeat-ids s4-001,s4-004,s4-007,s4-009,s4-010
```

One primary run per system/case contributes to primary accuracy. Additional
runs for the chosen IDs appear separately in `repeated_cases`. Runs alternate
system order by case/repeat, and each system receives a fresh claim conversation.
Neither system receives expected labels, evidence groups or input hints.

## Scoring and evidence

Gold must specify a verdict; supported/contradicted cases need required source
IDs or `evidence_groups`. Each group lists alternative acceptable citations;
every group must have a cited match. Additional independently valid evidence
may be manually adjudicated under documented rules, rather than silently
counted correct or used to tune a held-out set.

When supplied, company, fiscal year, numerical values, units and tolerance
are checked too. Returning an uncited source does not satisfy evidence credit.
Disclosure scope comes from cited source metadata. Numerical scope comes
from the returned calculation. API errors and exhausted budgets count as
failed executions, never factual `insufficient_evidence` verdicts.

The batch preserves every completed attempt, including errors, and returns zero
when the scheduled batch finishes. Quota-stopped or interrupted batches return
two and save a partial summary. Read accuracy/error counts and run_status;
process exit success is not an accuracy threshold. Three consecutive HTTP 429
failures stop further calls. Ctrl+C records the interrupted case/system without
inventing its missing outcome or token usage.

## Artifacts and uncertainty

`artifacts/local/comparison-<timestamp>/` contains:

- `metadata.json`: configuration, case/data/index/code hashes, Git commit,
  preselected repeat IDs and measurement boundaries.
- `results.jsonl`: case/system/repeat, status, failure categories, usage and
  a link to its separately saved full trace. Rows append after each attempt.
- `summary.json`: primary pass counts, Wilson 95% intervals, paired bootstrap
  difference (1,000 seeded resamples), paired outcome counts, mean/p95 latency,
  execution failures and separate repeated-run outcomes.

Latency covers each system's model/tool work, retries and query embedding;
it excludes loading the dataset and writing the trace. Existing caches may
be warm. The baseline and agent may therefore benefit differently from caches;
report the boundary rather than interpreting tiny timings as general speed.

Token usage is collected where the provider supplies it. Actual billed spend
is unavailable. Optional list-price estimates require both input/output prices
per million tokens and their date:

```bash
python3 -m fincheck compare-systems --input-price-per-million INPUT_PRICE --output-price-per-million OUTPUT_PRICE --pricing-date YYYY-MM-DD
```

Replace placeholders with verified prices for the configured model. Missing
usage, retries, cached tokens or thinking tokens leave the estimate unavailable
instead of inventing a zero or ignoring unmeasured billable usage. Embedding
costs are not included in the text-model estimate.

## Remaining evaluation work

The capability-scoped workflow can be included with
`--systems baseline,agent,multi`; this schedules 36 primary development runs.
All scopes share the same per-claim ceilings. See `docs/WORKFLOW.md` for the
permission matrix and the separate fault/optimization experiments.

This implements the evaluation infrastructure; it does not complete held-out
testing or independent memo evaluation. Reserve a separate final
claim set only after freezing sources and grading rules. Retrieval question
labels now include the user's confirmation of AI-assisted review; rq-07 and
rq-21 still require wording/source decisions. Embedding/hybrid pooling and the
retrieval answer experiment remain pending.

Source IDs are tied to the frozen local corpus. On another machine a newer
download may produce a different index. The runner rejects missing expected
sources; it does not silently replace them or label them model failures.

## Development validation on November 24

- 98 offline unit/protocol tests passed; the original three tool checks passed.
- A 23-question retrieval development comparison returned hit@5 counts of
  12/23 for keyword and 16/23 each for TF-IDF and BM25. rq-07/rq-21 were excluded.
  Embedding and hybrid were not run, so this is a partial F1 experiment.
- Live smoke attempts used gemini-3.5-flash-lite. Numeric and disclosure paths
  were exercised successfully after development fixes. Initial attempts exposed
  a model substituting a remembered value for the claim literal, and irrelevant
  growth/calc-ID fields in final responses. The literal guard rejected the
  substitution; clearer literal instructions and a stage-specific final schema
  addressed the interface issues. Failed attempts remain in local traces.
- The final disclosure-only smoke passed for both systems. The preceding
  two-case smoke passed the numeric case for both. These small, inspected,
  iterated development runs are not a frozen accuracy comparison and must not
  be pooled into a claimed final pass rate.

Local comparison traces and partial retrieval results are kept under
`artifacts/local/`. Review and export appropriate redacted evidence before a
final submission; this stage does not automatically publish raw traces.
