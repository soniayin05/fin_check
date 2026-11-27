# Implementation stages

Status as of November 27, 2026. The active GitHub checkout on Sonia's Mac is
`/Users/soniayin/Desktop/CIS 1990 Final Project/fin_check`. The containing folder
has an older checkout with separate Git state and configuration.

| Stage | Status | Remaining acceptance work |
| --- | --- | --- |
| SEC data | Pipeline and 546-row snapshot merged; 15 FY2024 core observations manually reconciled | Broader reconciliation; verify annual earnings-release selection |
| Retrieval | Five methods implemented; 23 AI-assisted labels confirmed by Sonia; preliminary keyword/TF-IDF/BM25 comparison run | Resolve rq-07/rq-21, review rq-23 wording, pool embedding/hybrid candidates and run answer experiment |
| Numeric agent tools | Merged into main | Broader claim interpretation evaluation |
| Baseline and evaluation runner | Implemented: fixed two-stage baseline, shared scope/evidence/numeric scoring, repetitions, traces and statistics | Review 12 new dev claim variants; run full comparison and preselected repetitions; final held-out set later |
| Memo workflow | Implemented: PDF text/page images, exact-quote extraction, approval/qualifier gates, shared checking budgets, JSON/HTML report | Independently annotated multi-page memos, extraction coverage/atomicity/qualifier evaluation, scans/charts/OCR |
| Reviewer workflow | Pending | Explicit tool/state permissions, shared budget, bounded repairs and worker failure handling |
| Recovery and observability experiments | Pending | Fault cases, recovery comparison, trace search and measured optimization |
| Final evaluation and delivery | Pending | Freeze sources/configuration/scoring, held-out run, interface/demo and write-up |

## Numeric tool stage

- `get_financial_fact` returns the observation, reporting period and source.
- `calculate` checks numeric equalities using `fact`, `growth` or `ratio` and
  reuses the existing ratio functions. A numeric literal must occur in the original
  claim. Units are explicit and rounding tolerance comes from written precision.
- `compare_growth` retains the original comparison interface. Metric names accept
  capitalization/spacing aliases. Unknown names are tool errors.
- Each numeric check returns a run-local `calc_id`. `submit_verdict` validates that
  ID, its verdict and cited sources. Final values and explanation are copied from
  the Python result. Original growth-comparison clients remain compatible.
- Optional ratio inputs catch missing observations specifically. Conflicts and
  invalid observations cannot become assumed zeros. Ratio inputs must have matching
  fiscal periods and expected units. USD_millions is converted before calculation.
- Checks relying on substituted zero inputs or approximate D&A yield insufficient
  evidence. The underlying ratio CLI still exposes its assumptions for inspection.
- `check`, `ratio` and `verify-number` use SEC facts when the snapshot exists.
  Sample-based demos/development checks keep their fixed default. Every selection
  can be overridden with `--facts`.

## Validation recorded during this stage

The 68 tests present at the start passed. The stage adds regression checks for
numeric equality, unit conversion, precision, calc IDs, citations, metric errors,
ratio input conflicts, and the normal CLI entry point. Scripted model checks are
protocol tests.

Live development smoke attempts against the existing local SEC snapshot:

| Claim | Observed result |
| --- | --- |
| Microsoft FY2024 revenue was $245.1B | supported; verified 245.122B |
| Microsoft FY2024 revenue was $250B | contradicted; verified 245.122B |
| Apple FY2024 diluted EPS was $6.08 | supported; verified 6.08 USD/share |
| Microsoft FY2024 operating margin was 44.6% | initial six-turn smoke budget exhausted after a correct calculation; passed on rerun with explicit ratio guidance and the normal ten-turn budget, verified 44.64429957% |

The first network attempt failed inside the sandbox. Live traces, including the
failed/incomplete attempts, are retained under ignored `artifacts/local/`.
These few development attempts do not establish a final accuracy rate.

The earlier numeric smoke used configuration from the containing folder. Keep
model configuration in the active checkout's `.env` for ordinary CLI runs.
`verify-number` and comparison dry runs work without model access.

## Next stage

Exercise the PDF workflow in `docs/MEMO.md`, review extracted claims against page
images, and evaluate coverage on independently annotated memos. Then implement
the permission-separated reviewer workflow. The full shared comparison and
embedding/hybrid retrieval experiment also remain pending. Keep gold out of
model/tool inputs; original sample development cases stay separate.
Budget exhaustion yields `incomplete`; completed missing-evidence checks can
yield `insufficient_evidence`.
