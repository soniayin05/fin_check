# Data pipeline (Phase 1)

`python -m fincheck prepare-data --include-sec` builds everything below for
Microsoft, Alphabet and Apple. Downloads are cached in `data/raw/`; a cached
file is never fetched again, so `data/raw/manifest.json` pins what was used.

| Output | Content | Source |
| --- | --- | --- |
| `data/facts/observations.json` | Annual numbers for the last five fiscal years, 39 line items per company | SEC Company Facts (XBRL) |
| `data/facts/coverage.json` | Which line item was found under which XBRL tag, per company and year | derived |
| `data/indexes/passages.json` | Text passages with company, fiscal year, source type and 10-K section | 10-K filings, earnings releases (8-K Exhibit 99.1), the FY2024 Microsoft annual report page |

## Setup

SEC requires automated requests to declare a contact. In `.env`:

```
SEC_USER_AGENT=FinCheck Your Name your.email@university.edu
```

Then:

```bash
python -m fincheck prepare-data --include-sec     # about 35 requests, well under SEC's 10 per second
python -m fincheck facts-report --company MSFT --year 2024
python -m fincheck ratio --facts data/facts/observations.json --company MSFT --year 2024
python -m fincheck ratio --facts data/facts/observations.json --company AAPL --year 2024 --ratio roe
python -m fincheck search "risk factors competition" --company GOOGL --year 2024 --source-type 10-K
```

## Line items and ratios

`fincheck/facts.py` maps 39 base line items to XBRL tags. `fincheck/ratios.py`
computes ratios 1-41 of the CFA Program Financial Ratio List from them, plus
EBITDA, free cash flow, net debt and a few other common measures. A ratio is
never stored; it is recomputed from observations, and the result lists the
formula, every observation used, and each assumption.

Rules that matter when reading results:

- USD values are whole dollars, not millions.
- A fiscal year is identified by its exact start and end dates. The `fy` field
  in Company Facts is ignored because it is the fiscal year of the filing.
- "Average" balances use this and the prior fiscal year-end, so the earliest
  loaded year has no average-based ratios.
- A missing required input gives `insufficient_evidence`. It means missing
  from the loaded data, not absent from public filings.
- An optional component that is missing (for example commercial paper inside
  total debt) is treated as zero and listed under `assumptions`.

## What must be checked by hand

The normalizer was built and tested without access to live SEC responses.
Before relying on it:

1. Read the coverage table printed by `prepare-data`. `--` means none of the
   candidate tags had a value. Decide for each gap whether the company does not
   report the item or the tag list in `facts.py` needs another candidate.
2. For each company, run `facts-report` for one year and compare at least
   revenue, net income, total assets, operating cash flow and diluted EPS with
   the 10-K. Record what you checked.
3. Read `problems` in the `prepare-data` output. An earnings release that could
   not be located is listed there and left out of the index.
4. Spot-check that `search` returns real 10-K text with sensible section labels.

After review, commit `data/facts/observations.json` and `coverage.json` so
evaluations run against a fixed snapshot.

## Known gaps in the SEC data (checked 2026-11-18)

| Gap | Effect | Handling |
| --- | --- | --- |
| Microsoft tags its "Depreciation, amortization, and other" line with a custom tag | No direct D&A value | D&A = depreciation + intangible amortization + finance lease amortization (FY2024: 21.8 vs 22.3 billion reported); stated as an assumption |
| Alphabet tags only depreciation | No combined D&A value | Same fallback; Alphabet's depreciation equals its cash-flow line |
| Apple has not tagged interest expense since FY2024 | EBIT-based ratios (ROIC, interest coverage, EBIT margin, interest burden, FCFF) are insufficient evidence for Apple FY2024 onward | Not approximated |
| Alphabet stopped reporting inventory after FY2022 | Inventory turnover, DOH, payables turnover and cash conversion cycle are insufficient evidence for Alphabet FY2023 onward | Not approximated |
| Alphabet long-term debt (non-current) is untagged before FY2023 | Debt-based averages for Alphabet FY2023 lack the prior year | The older tag includes finance leases, so it is not mixed in |
| No company has preferred stock or "short-term borrowings" | None | Treated as zero and stated as an assumption |

## Not covered

- Segment figures (Azure, iPhone, Google Cloud). Company Facts has only
  company-wide values; check segment claims against filing text.
- Valuation multiples such as P/E or EV/EBITDA. They need a market price,
  which no filing contains.
- Adjusted or non-GAAP measures as defined by each company.
- Quarterly periods.
