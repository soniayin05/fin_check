# Financial Data Reconciliation

Review date: 2026-11-23
Method: Manual comparison of normalized SEC Company Facts observations
against the FY2024 columns in locally cached 10-K filings.

## Review scope

This review covers 15 observations: revenue, net income, total assets,
operating cash flow, and diluted EPS for Microsoft, Apple, and Alphabet
in FY2024.

The reviewer confirmed that these values matched the filing figures
after accounting for reporting units. Other observations and fiscal
years are outside this review.

## Results

Monetary amounts below are in millions of USD.
Diluted EPS is in USD per share.
The observations JSON stores monetary amounts in whole USD.

| Company | Revenue | Net income | Total assets | Operating cash flow | Diluted EPS | Result |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| MSFT | 245,122 | 88,136 | 512,163 | 118,548 | 11.80 | Matched |
| AAPL | 391,035 | 93,736 | 364,980 | 118,254 | 6.08 | Matched |
| GOOGL | 350,018 | 100,118 | 450,256 | 125,299 | 8.04 | Matched |

## Fiscal periods

| Company | Fiscal year start | Fiscal year end |
| --- | --- | --- |
| MSFT | 2023-07-01 | 2024-06-30 |
| AAPL | 2023-10-01 | 2024-09-28 |
| GOOGL | 2024-01-01 | 2024-12-31 |

Revenue, net income, operating cash flow, and diluted EPS refer to
the full fiscal year. Total assets refer to the fiscal year-end date.

## Reference filings and locations

### Microsoft

Cached file: data/raw/10k-MSFT-FY2024.htm
Reference accession: 0000950170-24-087843

- Revenue, net income, diluted EPS: Consolidated Income Statements.
- Total assets: Consolidated Balance Sheets.
- Operating cash flow: Consolidated Statements of Cash Flows.

### Apple

Cached file: data/raw/10k-AAPL-FY2024.htm
Reference accession: 0000320193-24-000123

- Revenue, net income, diluted EPS: Consolidated Statements of Operations.
- Total assets: Consolidated Balance Sheets.
- Operating cash flow: Consolidated Statements of Cash Flows.

### Alphabet

Cached file: data/raw/10k-GOOGL-FY2024.htm
Reference accession: 0001652044-25-000014

- Revenue, net income, diluted EPS: Consolidated Statements of Income.
- Total assets: Consolidated Balance Sheets.
- Operating cash flow: Consolidated Statements of Cash Flows.

## Limitations

- This is a spot check of 15 observations, not validation of all 546.
- Missing fields were not treated as verified zeros.
- Ratios, growth rates, earnings-release selection, and retrieval labels
  require separate checks.
- The reference accessions above identify the cached filings used for
  manual comparison. Observation-level accession metadata remains
  unchanged; some observations come from later comparative filings.

## Snapshot SHA-256

```text
44869d6654f5c4af9b381002e8c084af881c678b6a6e57f8f1cb10712064bfcb  data/facts/observations.json
681948b289d6b8de1a799d20233b146d9dc32a9658462c50619d5efeaf0cb996  data/facts/coverage.json
```
