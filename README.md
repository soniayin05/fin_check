# FinCheck

CIS 1990 final project: check financial claims against filing evidence.

**November 27 implementation:** capability-scoped planning, evidence work and read-only review now verify financial claims with a shared budget and bounded repairs. A local upload interface connects PDF extraction, claim review and JSON/HTML/annotated-PDF results. Single-agent and fixed-baseline references remain available. See `docs/WORKFLOW.md` and `docs/IMPLEMENTATION_STATUS.md`.

## Quick start

Python 3.10+; Python 3.11 recommended. Core claim checking uses the standard
library; PDF commands require the pinned dependency in `requirements.txt`.

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
cp .env.example .env  # Only on a fresh clone; preserve an existing .env.
```

Set `GEMINI_API_KEY` in `.env`, then run:

```bash
python -m fincheck prepare-data
python -m fincheck check "Microsoft revenue grew faster than operating cash flow in FY2024."
```

The expected numerical answer is **contradicted**: revenue grew about 15.67%, versus 35.36% for operating cash flow. Python calculates this from four reviewed annual-report observations. The model chooses tools and explains the result; it does not supply the numbers.

After `prepare-data --include-sec`, `check` and `ratio` use the local SEC observations
when present. `--facts <path>` selects a specific snapshot. The original `demo`,
`eval-dev` and `eval-agent-dev` commands keep using the sample unless given `--facts`.
Keep `.env` in this repository root (the folder containing this README).

```bash
python -m fincheck check 'Microsoft revenue was $245.1B in FY2024.'
python -m fincheck check 'Microsoft revenue was $250B in FY2024.'
python -m fincheck check 'Microsoft operating profit margin was 44.6% in FY2024.'
```

For a calculation without an API call:

```bash
python -m fincheck verify-number --company MSFT --year 2024 --measure revenue --value 245.1 --unit USD_billions
python -m fincheck verify-number --company MSFT --year 2024 --operation ratio --measure operating_profit_margin --value 44.6 --unit %
```

Values are compared at the precision supplied. `245.1 USD_billions` has a rounding
tolerance of 0.05 billion. A ratio requiring assumed zero inputs or approximate D&A
returns insufficient evidence until those inputs are verified. Model interpretation
of the company, measure and claim qualifiers still needs development evaluation.

To search the downloaded report:

```bash
python -m fincheck search "Azure AI services growth"
```

## Verification

```bash
python -m unittest discover -s tests -v
python -m fincheck eval-dev
python -m fincheck eval-agent-dev
```

The first two commands run offline. The third calls Gemini and may incur API usage. Gold answers stay in the scorer; the agent receives only each claim. Development results are not held-out evaluation. Scripted test models are explicitly test-only.

Without an API key, run `python -m fincheck demo` for the deterministic calculation. Live commands never silently substitute a fake model.

Compare the fixed baseline and agent (see `docs/BASELINE.md`):

```bash
python3 -m fincheck compare-systems --dry-run --method keyword
python3 -m fincheck compare-systems --only s4-001,s4-010 --method keyword
python3 -m fincheck compare-systems --method keyword
```

The first command validates inputs without API calls. The next two make live
model calls. Twelve draft development claims are in `evals/dev/claims_stage4.jsonl`;
their new claim variants need review before a frozen evaluation. Results retain
errors and incomplete runs, repeated runs separately, data/code hashes and traces.

The PDF workflow is documented in `docs/MEMO.md`. Generate a five-page example
offline with `python3 -m fincheck demo-memo`. `ingest-memo <pdf>` saves page
text/images; `extract-memo <pdf>` calls Gemini to propose quotations and metadata;
`check-memo <claims.json>` checks approved entries and prints an HTML report path.
Review the extracted quotes before using `--approve-extracted`. Forecasts and
special accounting qualifiers remain flagged for review.

Start the local upload interface:

```bash
source .venv/bin/activate
python3 -m fincheck serve
```

Open http://127.0.0.1:8765. Optional supplementary PDFs are explicitly marked
user-supplied. Official numeric coverage can be expanded with explicit SEC
tickers, subject to tag coverage and source reconciliation. See `docs/WORKFLOW.md`.

```bash
python3 -m fincheck check-workflow 'Microsoft revenue was $245.1B in FY2024.'
python3 -m fincheck compare-systems --systems baseline,agent,multi --method keyword
python3 -m fincheck eval-faults --dry-run
python3 -m fincheck traces --trace-scope review
```

## Implementation map

| File | Responsibility |
| --- | --- |
| `fincheck/tools.py` | Observations, annual-period/unit checks, growth arithmetic |
| `fincheck/calculations.py` | Fact, growth and ratio equality checks, unit conversion, rounding tolerance |
| `fincheck/sec.py` | SEC client: declared User-Agent, throttling, retries |
| `fincheck/facts.py` | Normalize SEC Company Facts into annual observations with tag, accession and filing date |
| `fincheck/ratios.py` | 41 CFA-list ratios plus EBITDA, free cash flow and net debt, with formula, inputs and assumptions |
| `fincheck/filings.py` | Select 10-K filings and earnings releases to archive |
| `fincheck/ingest.py` | Download/cache sources, record hashes, build facts and the passage index |
| `fincheck/retrieval.py` | Parse filings, chunk passages, five retrieval methods (keyword, TF-IDF, BM25, embedding, hybrid) |
| `fincheck/embeddings.py` | Gemini embeddings with an on-disk vector cache and token/cost accounting |
| `fincheck/retrieval_eval.py` | Retrieval labeling tool, top-5 hit-rate evaluation, answer experiment |
| `fincheck/stats.py` | Wilson intervals, paired bootstrap, p95 |
| `fincheck/model.py` | Gemini REST requests, local configuration, bounded retries |
| `fincheck/agent.py` | Plan/tool loop, argument checks, evidence IDs, budgets, trace saving |
| `fincheck/baseline.py` | Fixed evidence selection and answer pipeline using the same numeric controller |
| `fincheck/evaluation.py` | Shared scoring, paired comparisons, repeated runs and usage reporting |
| `fincheck/memo.py` | PDF pages, exact-quote extraction, approval gates, bounded checking and HTML report |
| `fincheck/workflow.py` | Enforced capability scopes, independent original-evidence reads and bounded repairs |
| `fincheck/faults.py`, `fincheck/fault_eval.py` | Fifteen fault scenarios and planned/no-plan policy comparison |
| `fincheck/trace_tools.py` | Trace filtering and content-free diagnostic exports |
| `fincheck/web.py`, `fincheck/uploads.py` | Local PDF upload/review UI and supplementary-source provenance |
| `fincheck/__main__.py` | CLI and separate tool/agent development evaluations |
| `evals/dev/claims.jsonl` | Three claims and predefined grading rules |
| `docs/CHECK_IN.md` | Wednesday demonstration and submission checklist |
| `docs/START_HERE.md` | What to understand and implement next |
| `docs/EVALUATION.md` | Final evaluation design |
| `docs/DATA.md` | SEC data pipeline, ratio rules, and what to verify by hand |
| `docs/RETRIEVAL.md` | Chunking, retrieval methods, labeling rules and evaluation |
| `evals/retrieval/questions.jsonl` | 23 reviewed evidence labels; two source/question issues remain |

## Current boundaries

- Numeric coverage follows the loaded snapshot. The committed sample has MSFT revenue and operating cash flow for FY2023/2024. Locally prepared SEC data includes Microsoft, Alphabet and Apple. The agent can check reported values, growth comparisons and numeric ratio claims.
- Retrieval offers keyword, TF-IDF, BM25, embedding and hybrid ranking over chunked filings (`docs/RETRIEVAL.md`). The agent still uses `keyword` by default. Reviewed AI-assisted labels support a preliminary three-method comparison; embedding/hybrid pooling and the answer experiment remain pending.
- A citation must have been retrieved, and returned growth rates must match the calculation tool. These checks do not prove that the model interpreted every qualifier correctly. Disclosure judgments still need labeled evaluation.
- `prepare-data --include-sec` archives SEC Company Facts, 10-K filings and earnings releases for Microsoft, Alphabet and Apple and normalizes the numbers (see `docs/DATA.md`). Local SEC observations have been exercised through the numeric tools and a small live smoke check. Broader source reconciliation and earnings-release validation remain pending.
- Reviewer scopes and a local upload UI are implemented. Final held-out comparison and completed feature credit are not established. Extraction coverage, qualifier preservation and full experiments still need evaluation. Scans/charts need visual/OCR review; optional memory and multimodal chart extraction remain pending.
- Traces record model usage when supplied and leave dollar cost unavailable until pricing is configured. Raw traces include user claims and filing text; review them before sharing.

The planned F1/F3/F4/F7 features are still the later project scope. See the docs for the staged implementation plan.

## Credentials and artifacts

`.env`, downloaded sources, indexes, and local traces are ignored by Git. Never commit API keys or private documents. Commands load `.env` automatically without overriding existing environment variables. Keep reviewed final evaluation evidence in `evals/results/` for submission. Model access and quota depend on the account; configure `GEMINI_MODEL` if needed.

The adapter follows Google's [generateContent API](https://ai.google.dev/api/generate-content). It uses Python's standard library so the first implementation is easy to inspect. No open-source license is selected for this course project. Write course reports in your own words and understand the submitted code.
