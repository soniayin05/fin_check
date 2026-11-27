# November 27 development implementation and evidence

FinCheck now has an access-scoped verifier and a local PDF interface. The same
Gemini model can serve each scope; separate conversations and Python permission
checks control access. Names and personalities do not define the boundary.
Planning can write a plan, evidence work can retrieve/calculate and propose a
draft, review can read original dependencies and record issues, and only the
controller can finalize. All scopes share budgets and at most two repairs.

The interface connects upload, exact-quote extraction, user approval, checking,
source/observation details, JSON/HTML output and an annotated PDF copy. Original
PDFs are preserved. Supplementary PDFs stay session-local and user-supplied.
Additional SEC tickers can be requested explicitly, but require coverage and
source reconciliation; the prepared default universe remains MSFT/AAPL/GOOGL.

## Versioned development comparison

The full 12-case comparison ran on commit `ead0f6e`, keyword retrieval,
temperature zero, 12 shared tool calls and 10 model turns per claim. Requests
were spaced by 2.1 seconds. Raw local artifacts are identified by run
`20261127T185821925502Z`. Gold consists of draft claim variants derived from
previously reviewed sources, not an independent final test set.

| System | Strict passes | Mean seconds | p95 seconds | Model calls | Tool calls |
| --- | --- | --- | --- | --- | --- |
| Fixed baseline | 10/12 | 6.56 | 15.40 | 23 | 22 |
| Single agent | 8/12 | 18.85 | 41.60 | 68 | 67 |
| Scoped workflow | 7/12 | 19.09 | 32.81 | 72 | 79 |

All pairwise bootstrap intervals included zero. This small, inspected development
batch does not establish a general accuracy difference. Quota failures and
budget exhaustion remain in the denominator. Latency includes retry/rate waits;
actual billing is not measured and no verified text-model prices were configured.

Three useful cases:

- `s4-006`: the scoped workflow correctly checked one metric's percentage growth.
  The single agent instead compared revenue growth with itself, producing the
  wrong interpretation. This shows a difference in tool selection; it does not
  prove that the review step itself caused the improvement.
- `s4-001`: redundant evidence work exhausted the scoped workflow's shared tool
  budget. It withheld a final verdict while the simpler systems completed.
- `s4-008`: baseline and scoped outputs said insufficient evidence but lacked
  the structured entity/year calculation required by the strict scorer. The
  source searches actually used FY2026 and FY2024, respectively, rather than
  the requested FY2030. The single agent performed a scoped missing-year
  calculation and passed. A correct verdict label alone is not the complete
  audit result.

Commit `ea92234` subsequently clarified single-metric growth versus relative
two-metric growth, directed numeric checks through calculate even for missing
years, and prevented a draft without an evidence-tool lookup. A separate two-case
smoke (`20261127T191055303190Z`) passed both cases for the single agent. All three
systems passed growth; the scoped missing-year run and baseline missing-year run
had execution failures. These reruns are separate development evidence and are
never substituted into the earlier 12-case denominator.

An earlier interrupted batch (`20261127T185155321096Z`) exposed an additional
numeric-citation handoff bug. The controller now preserves those cited passages
for independent original-source reading. Its failed attempts remain local.

## Recovery, optimization and retrieval records

Reviewed aggregate counters, per-case outcomes and original trace hashes are in
`evals/results/development_20261127.json`. Prompts, source text, private PDFs and
raw traces are excluded. The local artifact folders retain the complete runs.

Fifteen fault fixtures test five transient errors, five timeouts and five
bad-data corruptions. The no-plan ablation disables explicit planning and
automatic recovery together. Report injection coverage, execution recovery and
scored task success separately; an API failure before the target tool is not an
injected fault or a recovery. Scripted protocol tests cover all fifteen cases
and are not substitutes for live measurements.

The optimization contrast exposes or omits the redundant get_financial_fact
tool under common current prompts. calculate already returns its observations.
Both modes retain numeric validation, original-evidence reads and budgets. This
isolates tool-catalog availability rather than replaying every part of an older
implementation; actual prefetch use is chosen by the model. Interpret quality,
calls and timings together, including provider errors.

The full contrast (`20261127T191957090349Z`) recorded:

| Configuration | Strict passes | Mean seconds | Model calls | Tool calls |
| --- | --- | --- | --- | --- |
| Fact lookup available | 9/12 | 16.40 | 65 | 75 |
| Redundant lookup omitted | 8/12 | 20.70 | 63 | 65 |

Omitting the tool reduced observed tool calls, but did not demonstrate a quality
or latency improvement. The paired accuracy interval included zero. The original
runner reported quota_stopped on the final row despite all 24 scheduled records
being present; that status-only bug is now fixed. Original results are preserved.

The fault batch (`20261127T191258402996Z`) completed 30 scheduled runs. The
planned/recovery policy passed 11/15, with 14 faults injected and 11 recovered;
the disabled policy passed 0/15, with 13 injected and zero recovered. One and two
cases respectively failed before injection. These are policy-bundle results,
with API availability and subsequent review failures included, not an isolated
causal estimate of planning alone.

The repeated offline retrieval check scored keyword 12/23, TF-IDF 16/23 and
BM25 16/23. rq-07 and rq-21 remain excluded pending source/question decisions.
Embedding/hybrid candidates need additional relevance review; existing labels
were pooled with the three earlier methods. An attempted five-method live run
was blocked by Gemini embedding HTTP 429. No complete embedding/hybrid table is
available; missing results are not zero accuracy. This is not completed F1 evidence.

Final local validation passed 137 tests and the three original deterministic
checks. Case-ID trace lookup covers both newly tagged runs and legacy comparison
artifacts after relocation; diagnostic export hashes case IDs and removes content.

## Remaining acceptance work

Review the draft claim variants, resolve source-independence requirements and
complete retrieval candidate/answer grading. Independently annotate full memos
to measure extraction coverage, qualifiers and correction accuracy. Then create
a final freeze and independently authored held-out set, preselect repetitions,
run the frozen evaluation and prepare the final report/demo. See
`docs/WORKFLOW.md` for freeze and held-out commands.

Optional memory, OCR and multimodal chart extraction remain outside this core
implementation. Interface HTTP/session tests and PDF highlight rendering were
checked; interactive visual acceptance of the upload UI still needs user review.
