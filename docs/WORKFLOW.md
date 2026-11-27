# Access-scoped verification and local interface

Agents are defined by access and context, not names or personalities. They can
reuse the same Gemini model. Python enforces permissions on every tool call.

| Scope | Allowed operations | Context available |
| --- | --- | --- |
| plan | set_plan | Claim, previous plan and review issues |
| evidence | calculate, compare_growth, search_filings, read_source, propose_verdict | Claim, plan, issues and already-retrieved evidence |
| review | read_evidence, record_review | Claim, draft and identifiers of required original evidence |
| controller | State transitions and finalization | Shared state and deterministic validation |

The review conversation does not receive the evidence scope's messages or
private reasoning. An approval is accepted only after every original source
and numeric observation used by the draft has been read from the archive or
fact store. The model cannot claim it read a source: the controller tracks reads.
Review cannot search, calculate, modify evidence or submit a final verdict.
Only the controller releases an approved draft. Numeric arithmetic is replayed
against the original observations before approval is accepted.

## Commands

```bash
python3 -m fincheck check-workflow 'Microsoft revenue was $245.1B in FY2024.'
python3 -m fincheck compare-systems --systems baseline,agent,multi --dry-run
python3 -m fincheck compare-systems --systems baseline,agent,multi --method keyword
```

The default shared cap is 12 tool calls and 10 model turns per claim, across
all scopes. Denied calls and retries count. A rejected review can trigger at
most two repair rounds, within the same budget. Exhaustion, unrepaired issues
and worker/API failures produce incomplete with no final factual verdict.
Completed checks lacking evidence can return insufficient_evidence.

The evidence scope normally omits get_financial_fact because calculate already
returns the observations. A live development trace showed an unnecessary fact
prefetch adding one model round trip before calculate. The pre-optimization
configuration remains available for an explicit comparison:

```bash
python3 -m fincheck compare-systems --systems multi_prefetch,multi --method keyword
```

Both configurations retain the same numeric and review guards. Measure actual
quality, model/tool calls and latency; do not assume the optimization always helps.

## Local upload interface

```bash
source .venv/bin/activate
python3 -m fincheck serve
```

Open http://127.0.0.1:8765. The server binds only to loopback, checks Host and
Origin, requires a per-server token for mutations, and serves only artifacts
from the selected session. It uses the existing .env for model configuration.

1. Choose a memo PDF and click Extract claims. This uses the Gemini API.
2. Review quotation, page, company, period and units. Select reviewed claims.
3. Choose the access-scoped workflow or the single-agent reference and check.
4. Open the report, original page images, source links and annotated PDF copy.

The interface processes one task at a time. Uploads are capped at 20 MiB per
PDF, ten pages per document, and twelve extracted claims. Verification has
shared caps of 40 model turns and 60 tool calls, with per-claim caps of twelve
each. These are logical requests; bounded provider retries can add HTTP attempts.
Forecasts, special accounting qualifiers and unresolved scope remain flagged.
PDF text is sent to Gemini; stored pages, reports and traces stay local.
Scans, chart-only facts and ambiguous table extraction need human review.

Optionally attach a supplementary source PDF with an explicit company/ticker
and fiscal year. Its passages are session-local and marked user_supplied.
They do not become trusted Company Facts or independent official corroboration.
Companies outside the built-in numeric snapshot can be checked against supplied
text where adequate evidence exists; structured ratio verification still needs
normalized observations. Original PDF files are preserved. A separate annotated
copy highlights checked claims and records verdict/source details in annotations.

## Expand official company coverage explicitly

```bash
python3 -m fincheck prepare-data --include-sec --tickers NVDA,AMZN
```

Additional tickers are resolved through the SEC ticker map, keeping the three
default companies. No additional company has been downloaded or manually
reconciled merely by adding this option. Normalization assumes supported US-GAAP
tags; foreign/private issuers and missing/custom tags may not be supported.
Review coverage and reconcile source figures for each added issuer. Pass the
same additional tickers when rebuilding. Changing the universe changes the
dataset/index and requires revalidation; do not do it after a final freeze.

## Fault injection and observability

```bash
python3 -m fincheck eval-faults --dry-run
python3 -m fincheck eval-faults --method keyword
python3 -m fincheck traces --trace-scope review
python3 -m fincheck traces --trace-tool calculate --trace-status incomplete
python3 -m fincheck export-traces
```

Fifteen distinct development inputs cover five transient failures, five
timeouts and five bad-data variants (unit, period, duplicate, value, source).
Each injection is recorded. With explicit planning, read-only transient tools
get one bounded retry/backoff; bad data requires a new plan. The no-plan
ablation disables explicit planning and automatic retry/replan together, so
this measures the combined policy, not the isolated effect of writing a plan.
Runs where the target tool was never called are reported as not injected.
Execution recovery and scored task success are different outcomes.

Trace search returns scope/tool timings, status and provider usage where
available. Export removes raw claims, prompts, model answers, source contents,
URLs and local filenames, retaining hashes and allowlisted diagnostic fields.
Actual billing is not read; text-token estimates require user-supplied prices.

GEMINI_MIN_INTERVAL_SECONDS sets request spacing (default one second). Increase
it for your account's rate limit. Comparison summaries are checkpointed after
each completed run. Three consecutive HTTP 429 failures stop the batch;
run_status and completed_runs distinguish partial batches from completed ones.
Ctrl+C during a system run also saves an interrupted summary. Previously
completed failures remain in the results. A partial batch is not a full-system
accuracy comparison, and a successful retry does not erase an earlier run.

These commands implement development experiments. Their results are not final
held-out evidence. Human review of gold, independently annotated memos, final
freeze and final evaluation remain required. Optional memory, OCR and multimodal
chart extraction are not implemented by the scoped workflow.

## Freeze before held-out evaluation

After development and gold-review decisions, commit the final code, then run:

```bash
python3 -m fincheck freeze-eval --systems baseline,agent,multi --method keyword
```

This makes no model calls. It records code/scorer hashes, facts/index/archive
manifest hashes, model alias, retriever, embedding settings, request spacing,
systems and budgets. It stores no credentials or expected answers. Keep the
printed freeze.json. Documentation/result-only commits may follow; changed
runtime code, data or settings invalidate the freeze. Provider model aliases,
service availability and quota are external conditions, so retain run dates.

Independently author and check the held-out cases without tuning the system.
Replace FREEZE_PATH below with the printed path. Preselect five representative
case IDs for three total runs each before examining results:

```bash
python3 -m fincheck compare-systems --split heldout --cases evals/heldout/claims.jsonl --freeze FREEZE_PATH --systems baseline,agent,multi --method keyword --dry-run
python3 -m fincheck compare-systems --split heldout --cases evals/heldout/claims.jsonl --freeze FREEZE_PATH --systems baseline,agent,multi --method keyword --repeats 3 --repeat-ids YOUR_FIVE_IDS
```

Held-out mode requires a matching freeze and rejects the development case
directory. The scorer alone reads gold. The command cannot establish human
authorship or stop an operator inspecting a case file; record those decisions
honestly. Final memo extraction coverage and source independence still require
separate human review.
