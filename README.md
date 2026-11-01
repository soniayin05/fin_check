# FinCheck

CIS 1990 final project: check financial claims against filing evidence.

**November 4 check-in prototype:** one Gemini agent can plan a check, call a Python growth-comparison tool, search an archived Microsoft annual report, and return a sourced verdict. It saves model/tool traces and stops after 12 tool calls or 10 model turns. This is an early single-agent implementation, not the completed multi-agent project.

## Quick start

Python 3.10+; Python 3.11 recommended. No third-party packages required.

```bash
python3 -m venv .venv
source .venv/bin/activate
cp .env.example .env  # Only on a fresh clone; preserve an existing .env.
```

Set `GEMINI_API_KEY` in `.env`, then run:

```bash
python -m fincheck prepare-data
python -m fincheck check "Microsoft revenue grew faster than operating cash flow in FY2024."
```

The expected numerical answer is **contradicted**: revenue grew about 15.67%, versus 35.36% for operating cash flow. Python calculates this from four reviewed annual-report observations. The model chooses tools and explains the result; it does not supply the numbers.

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

## Implementation map

| File | Responsibility |
| --- | --- |
| `fincheck/tools.py` | Reviewed financial observations, annual-period/unit checks, arithmetic |
| `fincheck/ingest.py` | Download/cache the annual report and record source hashes |
| `fincheck/retrieval.py` | Parse passages, preserve tables, filter and rank keyword matches |
| `fincheck/model.py` | Gemini REST requests, local configuration, bounded retries |
| `fincheck/agent.py` | Plan/tool loop, argument checks, evidence IDs, budgets, trace saving |
| `fincheck/__main__.py` | CLI and separate tool/agent development evaluations |
| `evals/dev/claims.jsonl` | Three claims and predefined grading rules |
| `docs/CHECK_IN.md` | Wednesday demonstration and submission checklist |
| `docs/START_HERE.md` | What to understand and implement next |
| `docs/EVALUATION.md` | Final evaluation design |

## Current boundaries

- Numeric support is MSFT revenue and operating cash flow for FY2023/2024 only. Figures are a manually reviewed extract from the [2024 annual report](https://www.microsoft.com/investor/reports/ar24/index.html), not automatically extracted XBRL.
- Retrieval uses the full annual report and simple keyword ranking. It is not the final embedding/hybrid retrieval feature.
- A citation must have been retrieved, and returned growth rates must match the calculation tool. These checks do not prove that the model interpreted every qualifier correctly. Disclosure judgments still need labeled evaluation.
- SEC Company Facts download was blocked by HTTP 403 in this environment. `prepare-data --include-sec` attempts the download explicitly; normalization is still pending. There is currently one external report, not two independent sources.
- No reviewer agent, memo segmentation, GUI, baseline comparison, statistical evaluation, or completed feature credit yet.
- Traces record model usage when supplied and leave dollar cost unavailable until pricing is configured. Raw traces include user claims and filing text; review them before sharing.

The planned F1/F3/F4/F7 features are still the later project scope. See the docs for the staged implementation plan.

## Credentials and artifacts

`.env`, downloaded sources, indexes, and local traces are ignored by Git. Never commit API keys or private documents. Commands load `.env` automatically without overriding existing environment variables. Keep reviewed final evaluation evidence in `evals/results/` for submission. Model access and quota depend on the account; configure `GEMINI_MODEL` if needed.

The adapter follows Google's [generateContent API](https://ai.google.dev/api/generate-content). It uses Python's standard library so the first implementation is easy to inspect. No open-source license is selected for this course project. Write course reports in your own words and understand the submitted code.
