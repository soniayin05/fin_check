# Wednesday, November 4 check-in

This is an implementation checklist and demonstration guide. Write the course progress update in your own words.

## What exists

- A CLI accepts one natural-language claim.
- One Gemini agent records a plan, chooses tools, and submits a structured verdict.
- Python checks annual periods and units, computes growth, and returns the original observations and source locators.
- A downloaded Microsoft FY2024 annual report is split into 697 passages and searched by keywords, with company/year filters.
- The controller validates arguments, rejects unknown citations and incorrect returned growth rates, and caps execution at 12 tool calls / 10 model turns.
- Every run saves model/tool events, timings, available token usage, source/code hashes, and completion status locally. Missing cost data is marked unavailable.
- Three committed development claims have numerical/verdict grading rules. The live evaluation receives only the claims, not the expected answers.

This is a single-agent check-in prototype. F1 embeddings/hybrid ranking, F3 reviewer coordination, F4 fault-injection evaluation, and F7 measured optimization remain future work.

## What to demonstrate in 30 seconds to 2 minutes

From the project folder, run:

```bash
python3 -m fincheck check "Microsoft revenue grew faster than operating cash flow in FY2024."
```

Explain: the model selects the growth tool; Python retrieves the four observations and calculates 15.67% and 35.36%; the final verdict is contradicted. Show the printed evidence and the trace file's sequence of tool calls. Do not display `.env`.

Then show a missing-period case if time permits:

```bash
python3 -m fincheck check "Microsoft revenue grew faster than operating cash flow in FY2025."
```

Expected behavior: insufficient evidence **in the loaded FY2023/2024 numeric extract**. It does not mean FY2025 filings do not exist.

If the API is unavailable, show `python3 -m fincheck demo` and the saved live result. Explain which is a deterministic fallback and which is an actual prior agent run.

## Submission items to prepare yourself

1. Link the repository at the commit you demonstrate.
2. Record the short demonstration.
3. Write a one-page update: implemented work, next steps, scope changes, and two concrete risks.
4. Point to `evals/dev/claims.jsonl` for the three cases and intended checks.
5. State that the first live development evaluation ran November 1. Cite `evals/results/check_in_development.json` for actual outcomes; it includes unsuccessful runs.

Two risks you can discuss: SEC access currently returns 403, and Gemini rate limits interrupt live runs. Other limitations include company/period coverage and verifying that model interpretations preserve claim qualifiers.

## First implementation session after check-in

1. Read `tools.py`, then `Agent.run` in `agent.py`. Explain each branch before changing it.
2. Add a disclosure claim with a manually verified acceptable passage, and evaluate its retrieved evidence rather than only its verdict.
3. Implement SEC normalization with accession, filing date, exact start/end dates, and explicit duplicate handling.
4. Add the fixed retrieval-and-answer baseline before adding a reviewer agent, so you can measure whether coordination helps.

Do not claim the two-source requirement or completed feature points from this prototype. Two table locators in one annual report remain one source.
