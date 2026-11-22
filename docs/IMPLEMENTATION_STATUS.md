# Implementation stages

Status as of November 21, 2026. The active GitHub checkout on Sonia's Mac is
`/Users/soniayin/Desktop/CIS 1990 Final Project/fin_check`. The containing folder
has an older checkout with separate Git state and configuration.

| Stage | Status | Remaining acceptance work |
| --- | --- | --- |
| SEC data | Pipeline merged; local observations contain 546 rows across MSFT, GOOGL and AAPL | Record reconciliation against primary filings for each company; verify annual earnings-release selection |
| Retrieval | Chunking, keyword, TF-IDF, BM25, embedding and hybrid implementations merged | Label the 25 draft questions, compare methods, run answer experiment; the committed questions currently have no relevance labels |
| Numeric agent tools | Implemented in `codex/stage-3-numeric-tools` | Broader claim interpretation evaluation and reviewed development cases |
| Baseline and evaluation runner | Next implementation stage | Fixed retrieval/answer baseline, common verdict/evidence scoring, repeated runs and comparable budgets |
| Memo workflow | Pending | PDF ingestion, claim extraction with source locations and qualifier checks |
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

The newer checkout's `.env` lacks `GEMINI_API_KEY`. Live smoke calls explicitly
loaded the existing configuration from the containing folder. To run `check`
normally, configure the key in the active repository's `.env`. `verify-number`
works without model access.

## Next stage

Implement a fixed baseline and a shared claim evaluation runner before adding
reviewer coordination. Keep gold labels out of model/tool inputs. Preserve the
original development cases and record source-checked numeric/disclosure cases
separately. An exhausted execution budget produces `incomplete` with no factual
verdict. A completed check with missing evidence can produce `insufficient_evidence`.
