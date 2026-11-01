# How to start building FinCheck

## First session: understand the existing tools

1. Run `python -m fincheck demo`.
2. Open `data/sample/financial_facts.json`. Find the four numbers in the linked annual report. Independently verify them; this extract was prepared as starter material.
3. Read `calculate_growth` in `fincheck/tools.py`. Explain the formula and why periods and units are checked before arithmetic.
4. Run `python -m unittest discover -s tests -v`. Read the nine-month-period test and explain why it must fail.
5. Read `evals/dev/claims.jsonl`, then run `python -m fincheck eval-dev`. The expected answers are used by the scorer, not by the tool being tested. The two reversed claims are correlated development examples, not independent held-out evidence.

Next, run the live claim command in README and inspect its trace. Read `Agent.run`: explain how a model-requested function becomes a Python tool call. Then add a disclosure development case with a manually identified supporting passage.

## Milestone 1: source ingestion and one real question (by November 4 check-in)

The basic implementation now exists in `ingest.py`, `retrieval.py`, `model.py`, and `agent.py`. See `CHECK_IN.md` for tested status. The items below describe the intended milestone and remaining improvements; they are not a claim that every item is complete.

- Download one Microsoft filing/annual report and the SEC Company Facts JSON using a properly identified SEC User-Agent. Follow the [official SEC API documentation](https://www.sec.gov/search-filings/edgar-application-programming-interfaces). Set a timeout and cache downloads. Never silently substitute mocked values if access fails.
- Save raw files under ignored `data/raw/`. Record URL, retrieval time in UTC, SHA-256 of downloaded bytes, and parsing method in a manifest. Preserve reproducible references for evaluation.
- Normalize facts with company, metric, value, unit, start/end dates, filing accession, form, and filing date. Do not trust `fy` alone for comparative prior-year figures or choose the first matching XBRL row. Resolve duplicates and amendments explicitly; fail clearly when ambiguous.
- Parse text into passages with stable IDs and company/year/section metadata. Keep table boundaries when possible. Use keyword search first; add embeddings after this works.
- Add a Gemini adapter in `fincheck/model.py`. Verify the currently available function-calling model and free-tier limits in [Google's documentation](https://ai.google.dev/gemini-api/docs/function-calling). The current implementation uses the official REST API with the Python standard library. If switching to an SDK later, pin its tested version. Keep the key in an environment variable, never source code.
- Let a single model call `get_financial_fact`, `search_filings`, `calculate_growth`, and `read_source`. Validate tool names/arguments and cap tool calls. Return structured JSON with verdict, evidence IDs, and missing information.
- Start tracing model and tool calls now, before adding complexity. Token count unavailable is different from zero. Log sanitized inputs/outputs, status, duration, model ID, and retry count.

Done means: one natural-language claim is checked against real runtime sources, with a source locator and actual calculation. Commit three development cases and intended grading rules; make the 30-second to 2-minute progress video and one-page update. A partial path is allowed at check-in; the offline `demo` command remains a tools-only fallback; use `check` to demonstrate the live agent.

## Milestone 2: F1 retrieval and F3 multi-agent review (through November 13)

- Persist the index. Filter by company and year. Implement TF-IDF, BM25, and embeddings behind the same search interface; add hybrid ranking or reranking.
- Implement `fincheck/workflow.py` using LangGraph once the ordinary tools work. Pin compatible versions in a clean environment.
- Shared state: original claim, plan, retrieved observations/passages, draft, reviewer issues, repair count, tool count, final status.
- Coordinator -> researcher -> reviewer -> finalize, or a bounded repair loop. The reviewer must inspect original evidence, not merely agree with the draft.
- Handle one worker failure explicitly. Start with two repair rounds, twelve total tool calls per claim, and a shared memo-wide budget (not twelve fresh calls for every agent).
- Implement the fixed retrieval-and-answer baseline and the single-agent-with-all-tools comparison. Use the same sources and comparable total budgets.
- Run the first real agent development evaluation. Any feature changes need instructor approval by November 13.

## Milestone 3: product flow, F4 recovery, F7 improvement (through November 25)

- Extract claims with offsets into the memo; retain dates, units, and qualifications. Test omitted claims and accidental additions separately.
- Add a small Streamlit interface: memo input -> claim table -> evidence panel -> proposed correction. Install and pin its dependencies when implementing it.
- Keep runtime errors separate from an evidence-based claim verdict. A timeout is an execution failure with incomplete evidence, not proof that a public fact is absent.
- Transient timeouts: retry safe reads with backoff. Wrong period or missing data: revise the plan. Exhausted budget: return a clear incomplete result.
- Inject five tool failures, five timeouts, and five bad-data cases, each with distinct inputs. Compare with the no-explicit-plan version.
- Inspect development traces for a real bottleneck. Save the before version, implement a fix, and freeze both before touching held-out results.

## Milestone 4: freeze, evaluate, submit (November 26 to December 1)

Read `EVALUATION.md`. Freeze exact data, model configuration, code, and scoring rules. Reserve final test cases from tuning. Save actual results for all systems, uncertainty intervals, failure analysis, cost and latency. Complete README run/eval commands, dependency lock, write-up, source archive, and git log export. The final is December 1; private demo is December 2.

## Do not build yet

Login, multi-user accounts, investment recommendations, long-term memory, an MCP server, and automatic trading are outside the current proposal. Keep the five-claim memo workflow understandable and testable first.
