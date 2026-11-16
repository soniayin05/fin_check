"""Normalize SEC Company Facts (XBRL) JSON into auditable annual observations.

Design rules:
- Fiscal periods are identified by exact start/end dates, never by the `fy` field.
  In Company Facts `fy` is the fiscal year of the *filing*, so prior-year
  comparatives inside a FY2024 10-K also carry fy=2024.
- Only values from annual reports (10-K, 10-K/A) are used. Durations must span
  350-380 days, which rejects quarterly and nine-month figures.
- When several filings report the same period, the most recently filed one wins
  and the observation is marked `restated` if an earlier value differed. Two
  different values inside that same latest filing are a conflict: the value is
  skipped and reported, never chosen silently.
- Each metric has an ordered list of candidate XBRL tags because companies use
  different concepts. The tag actually used is stored on every observation.
- Company Facts only contains company-wide (non-dimensional) values. Segment
  figures such as Azure or iPhone revenue are NOT here; check those against
  filing text.

Fiscal-year label = calendar year of the period end date (MSFT FY2024 ends
2024-06-30, AAPL FY2024 ends 2024-09-28, GOOGL FY2024 ends 2024-12-31). This is
correct for the three supported companies; a company whose year ends in early
January would need a different rule.
"""

from datetime import date

from .sec import archive_url, facts_url

ANNUAL_FORMS = {"10-K", "10-K/A"}
ANCHOR_METRICS = ("revenue", "net_income")  # used to discover the fiscal calendar

# metric -> (period type, XBRL unit, candidate us-gaap tags in priority order)
D, I = "duration", "instant"
METRICS: dict[str, tuple[str, str, list[str]]] = {
    # Income statement
    "revenue": (D, "USD", ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
                           "SalesRevenueNet", "RevenueFromContractWithCustomerIncludingAssessedTax"]),
    "cost_of_revenue": (D, "USD", ["CostOfGoodsAndServicesSold", "CostOfRevenue"]),
    "gross_profit": (D, "USD", ["GrossProfit"]),
    "research_and_development": (D, "USD", ["ResearchAndDevelopmentExpense"]),
    "operating_income": (D, "USD", ["OperatingIncomeLoss"]),
    "interest_expense": (D, "USD", ["InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt"]),
    "pretax_income": (D, "USD", [
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"]),
    "income_tax_expense": (D, "USD", ["IncomeTaxExpenseBenefit"]),
    "net_income": (D, "USD", ["NetIncomeLoss"]),
    "preferred_dividends": (D, "USD", ["PreferredStockDividendsIncomeStatementImpact", "DividendsPreferredStock"]),
    "eps_basic": (D, "USD/shares", ["EarningsPerShareBasic"]),
    "eps_diluted": (D, "USD/shares", ["EarningsPerShareDiluted"]),
    "weighted_shares_basic": (D, "shares", ["WeightedAverageNumberOfSharesOutstandingBasic"]),
    "weighted_shares_diluted": (D, "shares", ["WeightedAverageNumberOfDilutedSharesOutstanding"]),
    # Cash flow statement
    "operating_cash_flow": (D, "USD", ["NetCashProvidedByUsedInOperatingActivities"]),
    "capex": (D, "USD", ["PaymentsToAcquirePropertyPlantAndEquipment"]),
    "depreciation_amortization": (D, "USD", [
        "DepreciationDepletionAndAmortization", "DepreciationAmortizationAndOther",
        "DepreciationAndAmortization", "DepreciationAmortizationAndAccretionNet"]),
    "dividends_paid": (D, "USD", ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends"]),
    "debt_issued": (D, "USD", ["ProceedsFromIssuanceOfLongTermDebt", "ProceedsFromIssuanceOfDebt",
                               "ProceedsFromDebtNetOfIssuanceCosts"]),
    "debt_repaid": (D, "USD", ["RepaymentsOfLongTermDebt", "RepaymentsOfDebt",
                               "RepaymentsOfDebtAndCapitalLeaseObligations"]),
    "short_term_debt_net_change": (D, "USD", [
        "ProceedsFromRepaymentsOfCommercialPaper", "ProceedsFromRepaymentsOfShortTermDebt",
        "ProceedsFromRepaymentsOfShortTermDebtMaturingInThreeMonthsOrLess"]),
    "interest_paid": (D, "USD", ["InterestPaidNet", "InterestPaid"]),
    "lease_payments": (D, "USD", ["OperatingLeasePayments", "OperatingLeaseCost"]),
    # Balance sheet (fiscal year-end balances)
    "cash": (I, "USD", ["CashAndCashEquivalentsAtCarryingValue"]),
    "short_term_investments": (I, "USD", ["ShortTermInvestments", "MarketableSecuritiesCurrent",
                                          "AvailableForSaleSecuritiesDebtSecuritiesCurrent"]),
    "receivables": (I, "USD", ["AccountsReceivableNetCurrent"]),
    "inventory": (I, "USD", ["InventoryNet"]),
    "current_assets": (I, "USD", ["AssetsCurrent"]),
    "net_ppe": (I, "USD", [
        "PropertyPlantAndEquipmentNet",
        "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization"]),
    "total_assets": (I, "USD", ["Assets"]),
    "accounts_payable": (I, "USD", ["AccountsPayableCurrent"]),
    "current_liabilities": (I, "USD", ["LiabilitiesCurrent"]),
    "total_liabilities": (I, "USD", ["Liabilities"]),
    "commercial_paper": (I, "USD", ["CommercialPaper"]),
    "short_term_borrowings": (I, "USD", ["ShortTermBorrowings", "OtherShortTermBorrowings"]),
    "long_term_debt_current": (I, "USD", ["LongTermDebtCurrent"]),
    "long_term_debt_noncurrent": (I, "USD", ["LongTermDebtNoncurrent"]),
    "shareholders_equity": (I, "USD", ["StockholdersEquity"]),
    "shares_outstanding": (I, "shares", ["CommonStockSharesOutstanding"]),
}


class NormalizationError(ValueError):
    """The filing data cannot be normalized without guessing."""


def _days(start: str, end: str) -> int:
    return (date.fromisoformat(end) - date.fromisoformat(start)).days + 1


def _resolve(rows: list[dict]) -> tuple[dict | None, bool, str]:
    """Pick one value for a period. Returns (row, restated, problem)."""
    latest = max(row["filed"] for row in rows)
    newest = [row for row in rows if row["filed"] == latest]
    values = {row["val"] for row in newest}
    if len(values) != 1:
        return None, False, f"conflicting values {sorted(values)} filed {latest}"
    chosen = sorted(newest, key=lambda row: row["accn"])[-1]
    restated = any(row["val"] != chosen["val"] for row in rows)
    return chosen, restated, ""


def _periods(concept: dict | None, unit: str, period_type: str) -> dict[tuple[str, str], list[dict]]:
    """Group a concept's annual-report rows by exact (start, end)."""
    grouped: dict[tuple[str, str], list[dict]] = {}
    for row in (concept or {}).get("units", {}).get(unit, []):
        if row.get("form") not in ANNUAL_FORMS or not row.get("end") or not row.get("filed"):
            continue
        if not isinstance(row.get("val"), (int, float)) or isinstance(row.get("val"), bool):
            continue
        start, end = row.get("start"), row["end"]
        if period_type == D:
            if not start or not 350 <= _days(start, end) <= 380:
                continue
            key = (start, end)
        else:
            if start:
                continue
            key = (end, end)
        grouped.setdefault(key, []).append(row)
    return grouped


def fiscal_calendar(gaap: dict, keep_years: int) -> list[tuple[int, str, str]]:
    """Return [(fiscal_year, start, end)] for the most recent annual periods."""
    for metric in ANCHOR_METRICS:
        _, unit, tags = METRICS[metric]
        periods: set[tuple[str, str]] = set()
        for tag in tags:
            periods.update(_periods(gaap.get(tag), unit, D))
        if periods:
            ordered = sorted(periods, key=lambda p: p[1])
            for (_, prev_end), (_, end) in zip(ordered, ordered[1:]):
                if prev_end[:4] == end[:4]:
                    raise NormalizationError(
                        f"Two annual periods end in {end[:4]} ({prev_end}, {end}); fiscal-year labels are ambiguous")
            return [(int(end[:4]), start, end) for start, end in ordered[-keep_years:]]
    raise NormalizationError("No annual revenue or net income found; cannot determine fiscal periods")


def normalize(companyfacts: dict, ticker: str, cik: int, keep_years: int = 5,
              raw_sha256: str | None = None) -> dict:
    """Return {"observations": [...], "sources": {...}, "coverage": [...]} for one company."""
    gaap = companyfacts.get("facts", {}).get("us-gaap", {})
    if not gaap:
        raise NormalizationError(f"{ticker}: no us-gaap facts in Company Facts payload")
    calendar = fiscal_calendar(gaap, keep_years)
    by_duration = {(start, end): year for year, start, end in calendar}
    by_instant = {(end, end): year for year, _, end in calendar}
    observations, sources, coverage = [], {}, []
    for metric, (period_type, unit, tags) in METRICS.items():
        wanted = by_duration if period_type == D else by_instant
        per_tag = {tag: _periods(gaap.get(tag), unit, period_type) for tag in tags}
        for key, year in sorted(wanted.items(), key=lambda item: item[1]):
            entry = {"company": ticker, "metric": metric, "fiscal_year": year, "status": "missing"}
            for tag in tags:
                rows = per_tag[tag].get(key)
                if not rows:
                    continue
                chosen, restated, problem = _resolve(rows)
                if chosen is None:
                    entry.update(status="conflict", tag=tag, detail=problem)
                    break
                accession = chosen["accn"]
                source_id = f"sec-{ticker.lower()}-{accession}"
                sources[source_id] = {
                    "url": archive_url(cik, accession),
                    "section": f"XBRL financial data, {chosen['form']} filed {chosen['filed']}",
                    "accession": accession, "form": chosen["form"], "filed": chosen["filed"],
                    "api_url": facts_url(cik), "raw_sha256": raw_sha256,
                    "method": "Normalized automatically from SEC Company Facts; verify against the filing",
                }
                observations.append({
                    "id": f"{ticker.lower()}-{metric}-{year}", "company": ticker, "metric": metric,
                    "value": chosen["val"], "unit": unit, "fiscal_year": year,
                    "period_start": key[0], "period_end": key[1], "source_id": source_id,
                    "period_type": period_type, "tag": tag, "accession": accession,
                    "form": chosen["form"], "filed": chosen["filed"], "restated": restated,
                })
                entry.update(status="ok", tag=tag, value=chosen["val"], unit=unit,
                             accession=accession, restated=restated)
                break
            coverage.append(entry)
    return {"observations": observations, "sources": sources, "coverage": coverage,
            "calendar": [{"fiscal_year": y, "start": s, "end": e} for y, s, e in calendar]}


def merge(results: list[dict]) -> dict:
    """Combine per-company results into one FactStore-compatible payload."""
    payload = {
        "description": "Annual observations normalized from SEC Company Facts (XBRL). "
                       "Values are in the unit shown (USD = whole dollars, not millions).",
        "sources": {}, "observations": [],
    }
    for result in results:
        payload["sources"].update(result["sources"])
        payload["observations"].extend(result["observations"])
    return payload


def coverage_table(coverage: list[dict]) -> str:
    """Plain-text metric x (company, year) table for manual review."""
    columns = sorted({(row["company"], row["fiscal_year"]) for row in coverage})
    cell = {(row["metric"], row["company"], row["fiscal_year"]): row for row in coverage}
    mark = {"ok": "ok", "missing": "--", "conflict": "!!"}
    width = max(len(metric) for metric in METRICS) + 2
    lines = ["".ljust(width) + " ".join(f"{c}{str(y)[2:]}".rjust(7) for c, y in columns)]
    for metric in METRICS:
        row = [mark.get(cell.get((metric, c, y), {}).get("status", "missing"), "--").rjust(7) for c, y in columns]
        lines.append(metric.ljust(width) + " ".join(row))
    lines.append("ok = value found; -- = not reported under the candidate tags; !! = conflicting values")
    return "\n".join(lines)
