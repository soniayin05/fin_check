# PDF memo workflow

The memo stage accepts PDFs, saves page text and PNG previews, extracts quoted
claims, then checks approved claims through the existing single agent. It emits
a JSON report and an HTML report linking each quote to its source page image.
The original PDF is preserved. A separate annotated copy highlights located
quotations and records verdicts/source details; HTML/JSON retain full results.

## Setup

Python 3.10+ and the pinned PyMuPDF dependency are required for PDF commands:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

Reuse an existing project environment rather than recreate it. Core non-PDF
commands still work without PyMuPDF. Page text/image methods follow the
[PyMuPDF documentation](https://pymupdf.readthedocs.io/en/latest/page.html).
Keep GEMINI_API_KEY and GEMINI_MODEL in this checkout's `.env` for live commands.

## Review and check a memo offline

Start `python3 -m fincheck serve` and open `http://127.0.0.1:8765`.
Under **PDF memo workflow**, select **Enter claims manually (offline)** and
open the PDF. Pick a page and select text to copy complete source lines into
the quote field. Compare the quotation and its qualifiers with the image.
Save the claim or edit it from the saved list. The claim file stores each
revision with its prior and updated entry. Saving an edit clears the current
selection. Open the claim file from the page to see the revision history.

For a historical annual figure for the entire company, enter its ticker,
fiscal year (for example FY2024), metric, complete numeric literal, unit and
calculation type. Select the reviewed claims, confirm their scope and run
**Cached numeric check (offline)**. Forecasts, special accounting qualifiers
and entries missing calculation fields stay flagged for review. The reviewer's
confirmation covers details the quote parser cannot reliably infer, including
segment scope, the unit and whether the quote makes a single assertion.
The report links the page and cached evidence and appears in **Saved reports**.
It records the revision, facts snapshot and zero model calls. Choose one of
the Gemini verification modes when you need model-assisted interpretation.

## Commands

Generate the synthetic five-page development PDF without an API call:

```bash
python3 -m fincheck demo-memo
```

The command prints its PDF path under ignored `artifacts/local/`. To ingest a
PDF offline, replace the example path with that printed path or your own memo:

```bash
python3 -m fincheck ingest-memo "/absolute/path/memo.pdf"
```

The new artifact folder contains `memo.json` and `pages/page-001.txt`,
`pages/page-001.png`, etc. Page numbers are one-based. Source/page hashes,
dimensions and parser version are recorded. Empty or scanned pages, embedded
images, vector drawings and overlong text produce explicit review warnings.
The default page cap is ten (configurable to 50); PDFs are limited to 20 MiB.
Encrypted PDFs are rejected. No OCR or chart interpretation is performed.

Extract claims with one model request per eligible page:

```bash
python3 -m fincheck extract-memo "/absolute/path/memo.pdf" --max-pages 10 --max-claims 12 --memo-model-budget 20
```

`--dry-run` ingests pages and previews the extraction schedule without model
calls. Optional `--memo-company MSFT --memo-year 2024` supplies user-declared
context. The opening page also accompanies later pages to help resolve local
references. Explicit text on a later page takes precedence over opening context.

Outputs include `claims.json` and `extraction_trace.json`. A claim has page,
exact quote, character start/end, company, metric, period, value, unit, type
and qualifiers. The controller rejects invented quotes, ambiguous duplicate
locations, partial-line quotations, invented values and invented qualifiers.
It retains extraction failures and rejected-item reasons. Truncation or budget
limits are visible issues, not silently successful extraction.

## Review and check

Open the page PNGs and compare every quotation and its metadata. Each claim
starts with `approved: false`. Set `approved: true` for reviewed entries in
`claims.json`, or use `--approve-extracted` to confirm that you have reviewed
the file as a whole. This flag is an explicit user confirmation, not an LLM
reviewer. Exact quote matching alone cannot establish atomicity, completeness,
correct fiscal interpretation, chart coverage or preserved cross-line qualifiers.

```bash
python3 -m fincheck check-memo "/absolute/path/claims.json" --dry-run
python3 -m fincheck check-memo "/absolute/path/claims.json" --approve-extracted --system multi --memo-model-budget 40 --memo-tool-budget 60
```

Use the exact `claims.json` path printed by extraction. Memo/source hashes,
stored page hashes and claim offsets are rechecked before verification. Edits
to values, quotes or qualifiers must still agree with the source text. Claims
with forecasts, conditional wording, unresolved company/period, or adjusted,
constant-currency and other special qualifiers remain `needs_review` even
when approved. This stage does not define a verifier for those claim types.

The checker sends the original quote plus explicitly marked context hints to
the existing agent. The source memo is the object being checked, never
independent financial evidence. Numeric verdicts still require Python
calculations and external source citations. Per-claim traces are saved through
the existing redaction/hash mechanism.

Verification uses shared model/tool budgets across claims, plus the existing
per-claim limits `--max-turns` (10) and `--max-calls` (12). Unrun claims remain
`not_run` after exhaustion; completed claims keep their results. Extraction
and verification have separate stage budgets: total configured logical model
requests are bounded by their sum. Provider-internal retries may add HTTP
attempts and are recorded by the adapter. No concurrency is used.

The printed report directory contains `report.json`, `report.html` and
incremental `progress.json`. Open the HTML report in a browser. Values from
the Python check appear as verified values where available; forecasts and
pending items show their review reasons. An overall `complete` report requires
all claims completed and no extraction issues/page warnings. This is an
execution status, not a guarantee that all claims in the original memo were
found. An empty extraction requires review. Exit code 1 denotes remaining
review work, incomplete checking or an execution failure, not necessarily a
false financial claim.

## Evaluation remaining

`evals/dev/memo_cases.json` records the authored five-page demo's expected
quotations/outcomes. It stays out of model inputs. Use this to inspect missed
and extra claims during development. Two independently annotated 3-5 page
memos and extraction coverage/qualifier measurements remain pending, along
with image/OCR evaluation. This is not a completed rubric experiment.

All memo files, page images, extraction traces and reports stay under ignored
local artifacts. Review/redact before explicitly exporting final evidence.
The permission-scoped workflow and upload interface are described in `docs/WORKFLOW.md`.

## Recorded development checks (November 27)

The five-page PDF was generated and ingested into five text files and five PNGs;
representative pages were visually inspected. A live Gemini extraction first
expanded two billion-denominated literals into whole dollars, which the exact
value guard rejected. After literal-format guidance was clarified, all five
authored quotations were extracted with no reported grounding issues. Both
attempts remain in local extraction traces.

Live verification returned supported for the revenue, margin and EPS examples,
contradicted for the $250B revenue claim, and needs_review for the forecast.
The four historical checks used 14 logical model calls and 14 tool calls within
the shared budgets. The report remained needs_review because of the forecast.
These are inspected synthetic development checks, not held-out accuracy results.

The HTML report parses and all five source-page links resolve. Automated browser
preview was unavailable in this environment, so HTML visual review remains a
manual step. PDF page text/image checks and protocol regressions run in the
test suite; the course's independent full-memo evaluation remains pending.
