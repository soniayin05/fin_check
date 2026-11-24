"""Deterministic financial ratios computed from annual observations.

Ratios 1-41 follow the CFA Program "Financial Ratio List" definitions and
numbering. Entries numbered None are common stock-pitch measures outside that
list. Every result carries the formula, the exact observations used, and any
assumption made, so a reviewer can re-derive the number.

Not covered here, by design:
- Valuation multiples (P/E, EV/EBITDA, FCF yield): they need a market price,
  which is not in company filings.
- Segment metrics (Azure growth, iPhone revenue): Company Facts has no segment
  data; verify those against filing text.
- "Adjusted" / non-GAAP figures: definitions differ by company.
"""

from dataclasses import asdict
from datetime import date
import re

from .facts import METRICS
from .tools import EvidenceError, FactStore, MissingEvidence, calculate_growth, normalize_company, normalize_metric


class Inputs:
    """Reads observations for one company-year and records everything it touched."""

    def __init__(self, store: FactStore, company: str, year: int):
        self.store, self.company, self.year = store, normalize_company(company), year
        self.used: dict[str, dict] = {}
        self.assumptions: list[str] = []
        self.periods: dict[int, tuple[str | None, str]] = {}

    def obs(self, metric: str, year: int | None = None):
        row = self.store.get_financial_fact(self.company, metric, self.year if year is None else year)
        kind, unit, _ = METRICS[row.metric]
        if row.period_type != kind or (row.unit != unit and not (unit == "USD" and row.unit == "USD_millions")):
            raise EvidenceError(f"Invalid period type or unit for {row.id}")
        start = row.period_start if kind == "duration" else None
        old_start, old_end = self.periods.get(row.fiscal_year, (None, row.period_end))
        if old_end != row.period_end or (old_start and start and old_start != start):
            raise EvidenceError("Ratio inputs must cover the same fiscal period")
        self.periods[row.fiscal_year] = (old_start or start, row.period_end)
        self.used[row.id] = asdict(row)
        return row

    def v(self, metric: str, year: int | None = None) -> float:
        row = self.obs(metric, year)
        return row.value * 10**6 if row.unit == "USD_millions" else row.value

    def opt(self, metric: str, year: int | None = None) -> float:
        """A component that may legitimately be absent; absence is recorded, not hidden."""
        try:
            return self.v(metric, year)
        except MissingEvidence:
            when = self.year if year is None else year
            note = f"{metric} FY{when} is not in the loaded data and was treated as 0"
            if note not in self.assumptions:
                self.assumptions.append(note)
            return 0.0

    def avg(self, fn) -> float:
        """Average of a balance at this and the prior fiscal year-end."""
        return (fn(self, self.year) + fn(self, self.year - 1)) / 2

    def days(self) -> int:
        row = self.obs("revenue")
        return (date.fromisoformat(row.period_end) - date.fromisoformat(row.period_start)).days + 1


def div(numerator: float, denominator: float) -> float:
    if denominator == 0:
        raise EvidenceError("Denominator is zero")
    return numerator / denominator


def bal(metric: str):
    return lambda i, year: i.v(metric, year)


def gross_profit(i: Inputs) -> float:
    try:
        return i.v("gross_profit")
    except MissingEvidence:
        i.assumptions.append("gross profit is not tagged; computed as revenue minus cost of revenue")
        return i.v("revenue") - i.v("cost_of_revenue")


def depreciation_amortization(i: Inputs) -> float:
    try:
        return i.v("depreciation_amortization")
    except MissingEvidence:
        pass
    total = i.v("depreciation") + i.opt("amortization_intangibles") + i.opt("finance_lease_amortization")
    i.assumptions.append("D&A = depreciation + amortization of intangibles + finance lease amortization; the "
                         "company's combined cash-flow line is not tagged in SEC Company Facts, so this can "
                         "differ slightly from the reported line")
    return total


def ebit(i: Inputs) -> float:
    return i.v("pretax_income") + i.v("interest_expense")


def tax_rate(i: Inputs) -> float:
    return div(i.v("income_tax_expense"), i.v("pretax_income"))


def total_debt(i: Inputs, year: int) -> float:
    i.assumptions.append("total debt = commercial paper + short-term borrowings + current and "
                         "non-current long-term debt; finance lease liabilities are excluded")
    return (i.v("long_term_debt_noncurrent", year) + i.opt("long_term_debt_current", year)
            + i.opt("commercial_paper", year) + i.opt("short_term_borrowings", year))


def liquid(i: Inputs, year: int) -> float:
    return i.v("cash", year) + i.opt("short_term_investments", year)


def working_capital(i: Inputs, year: int) -> float:
    return i.v("current_assets", year) - i.v("current_liabilities", year)


def equity(i: Inputs, year: int) -> float:
    return i.v("shareholders_equity", year)


def invested_capital(i: Inputs, year: int) -> float:
    return total_debt(i, year) + equity(i, year)


def income_to_common(i: Inputs) -> float:
    return i.v("net_income") - i.opt("preferred_dividends")


def receivables_turnover(i): return div(i.v("revenue"), i.avg(bal("receivables")))
def inventory_turnover(i): return div(i.v("cost_of_revenue"), i.avg(bal("inventory")))


def payables_turnover(i: Inputs) -> float:
    purchases = i.v("cost_of_revenue") + i.v("inventory") - i.v("inventory", i.year - 1)
    i.assumptions.append("purchases = cost of revenue + change in inventory")
    return div(purchases, i.avg(bal("accounts_payable")))


def dso(i): return div(i.days(), receivables_turnover(i))
def doh(i): return div(i.days(), inventory_turnover(i))
def days_payables(i): return div(i.days(), payables_turnover(i))
def roe(i): return div(i.v("net_income"), i.avg(equity)) * 100
def payout(i): return div(i.v("dividends_paid"), income_to_common(i)) * 100
def ebitda(i): return i.v("operating_income") + depreciation_amortization(i)
def free_cash_flow(i): return i.v("operating_cash_flow") - i.v("capex")
def net_debt(i): return total_debt(i, i.year) - liquid(i, i.year)


def defensive_interval(i: Inputs) -> float:
    i.assumptions.append("daily cash expenditures = (revenue - operating income - depreciation "
                         "and amortization) / days in period; the ratio list does not define it")
    daily = div(i.v("revenue") - i.v("operating_income") - depreciation_amortization(i), i.days())
    return div(liquid(i, i.year) + i.v("receivables"), daily)


def interest_coverage(i: Inputs) -> float:
    i.assumptions.append("interest payments taken as interest expense")
    return div(ebit(i), i.v("interest_expense"))


def fixed_charge_coverage(i: Inputs) -> float:
    lease = i.v("lease_payments")
    i.assumptions.append("interest payments taken as interest expense; lease payments from the lease note")
    return div(ebit(i) + lease, i.v("interest_expense") + lease)


def fcfe(i: Inputs) -> float:
    borrowing = i.v("debt_issued") - i.v("debt_repaid") + i.opt("short_term_debt_net_change")
    i.assumptions.append("net borrowing = long-term debt issued - repaid + net change in short-term debt")
    return free_cash_flow(i) + borrowing


def fcff(i: Inputs) -> float:
    i.assumptions.append("assumes interest was deducted in operating cash flow (US GAAP presentation)")
    return i.v("operating_cash_flow") + i.v("interest_expense") * (1 - tax_rate(i)) - i.v("capex")


# name -> (CFA list number, label, unit, formula, function)
RATIOS = {
    "current_ratio": (1, "Current ratio", "x", "current assets / current liabilities",
                      lambda i: div(i.v("current_assets"), i.v("current_liabilities"))),
    "quick_ratio": (2, "Quick ratio", "x", "(cash + short-term marketable investments + receivables) / current liabilities",
                    lambda i: div(liquid(i, i.year) + i.v("receivables"), i.v("current_liabilities"))),
    "cash_ratio": (3, "Cash ratio", "x", "(cash + short-term marketable investments) / current liabilities",
                   lambda i: div(liquid(i, i.year), i.v("current_liabilities"))),
    "defensive_interval": (4, "Defensive interval ratio", "days",
                           "(cash + short-term marketable investments + receivables) / daily cash expenditures",
                           defensive_interval),
    "receivables_turnover": (5, "Receivables turnover ratio", "x", "total revenue / average receivables",
                             receivables_turnover),
    "days_sales_outstanding": (6, "Days of sales outstanding (DSO)", "days",
                               "days in period / receivables turnover", dso),
    "inventory_turnover": (7, "Inventory turnover ratio", "x", "cost of goods sold / average inventory",
                           inventory_turnover),
    "days_inventory_on_hand": (8, "Days of inventory on hand (DOH)", "days",
                               "days in period / inventory turnover", doh),
    "payables_turnover": (9, "Payables turnover ratio", "x", "purchases / average trade payables",
                          payables_turnover),
    "days_payables": (10, "Number of days of payables", "days", "days in period / payables turnover",
                      days_payables),
    "cash_conversion_cycle": (11, "Cash conversion cycle", "days", "DOH + DSO - days of payables",
                              lambda i: doh(i) + dso(i) - days_payables(i)),
    "working_capital_turnover": (12, "Working capital turnover ratio", "x",
                                 "total revenue / average working capital",
                                 lambda i: div(i.v("revenue"), i.avg(working_capital))),
    "fixed_asset_turnover": (13, "Fixed asset turnover ratio", "x", "total revenue / average net fixed assets",
                             lambda i: div(i.v("revenue"), i.avg(bal("net_ppe")))),
    "total_asset_turnover": (14, "Total asset turnover ratio", "x", "total revenue / average total assets",
                             lambda i: div(i.v("revenue"), i.avg(bal("total_assets")))),
    "gross_profit_margin": (15, "Gross profit margin", "%", "gross profit / total revenue",
                            lambda i: div(gross_profit(i), i.v("revenue")) * 100),
    "operating_profit_margin": (16, "Operating profit margin", "%", "operating profit / total revenue",
                                lambda i: div(i.v("operating_income"), i.v("revenue")) * 100),
    "pretax_margin": (17, "Pretax margin", "%", "earnings before tax but after interest / total revenue",
                      lambda i: div(i.v("pretax_income"), i.v("revenue")) * 100),
    "net_profit_margin": (18, "Net profit margin", "%", "net income / total revenue",
                          lambda i: div(i.v("net_income"), i.v("revenue")) * 100),
    "operating_return_on_assets": (19, "Operating return on assets", "%",
                                   "operating income / average total assets",
                                   lambda i: div(i.v("operating_income"), i.avg(bal("total_assets"))) * 100),
    "return_on_assets": (20, "Return on assets", "%", "net income / average total assets",
                         lambda i: div(i.v("net_income"), i.avg(bal("total_assets"))) * 100),
    "return_on_equity": (21, "Return on equity", "%", "net income / average shareholders' equity", roe),
    "return_on_invested_capital_pretax": (
        22, "Return on invested capital (pre-tax)", "%",
        "EBIT / (average interest-bearing debt + average shareholders' equity)",
        lambda i: div(ebit(i), i.avg(invested_capital)) * 100),
    "return_on_invested_capital": (
        23, "Return on invested capital", "%",
        "EBIT x (1 - effective tax rate) / (average interest-bearing debt + average shareholders' equity)",
        lambda i: div(ebit(i) * (1 - tax_rate(i)), i.avg(invested_capital)) * 100),
    "return_on_common_equity": (24, "Return on common equity", "%",
                                "(net income - preferred dividends) / average common shareholders' equity",
                                lambda i: div(income_to_common(i), i.avg(equity)) * 100),
    "tax_burden": (25, "Tax burden", "x", "net income / earnings before taxes",
                   lambda i: div(i.v("net_income"), i.v("pretax_income"))),
    "interest_burden": (26, "Interest burden", "x", "earnings before taxes / EBIT",
                        lambda i: div(i.v("pretax_income"), ebit(i))),
    "ebit_margin": (27, "EBIT margin", "%", "EBIT / total revenue",
                    lambda i: div(ebit(i), i.v("revenue")) * 100),
    "financial_leverage": (28, "Financial leverage ratio (equity multiplier)", "x",
                           "average total assets / average shareholders' equity",
                           lambda i: div(i.avg(bal("total_assets")), i.avg(equity))),
    "total_debt": (29, "Total debt", "USD", "interest-bearing short-term + long-term debt",
                   lambda i: total_debt(i, i.year)),
    "debt_to_assets": (30, "Debt-to-assets ratio", "x", "total debt / total assets",
                       lambda i: div(total_debt(i, i.year), i.v("total_assets"))),
    "debt_to_equity": (31, "Debt-to-equity ratio", "x", "total debt / total shareholders' equity",
                       lambda i: div(total_debt(i, i.year), equity(i, i.year))),
    "debt_to_capital": (32, "Debt-to-capital ratio", "x", "total debt / (total debt + total shareholders' equity)",
                        lambda i: div(total_debt(i, i.year), invested_capital(i, i.year))),
    "interest_coverage": (33, "Interest coverage ratio", "x", "EBIT / interest payments", interest_coverage),
    "fixed_charge_coverage": (34, "Fixed charge coverage ratio", "x",
                              "(EBIT + lease payments) / (interest payments + lease payments)",
                              fixed_charge_coverage),
    "dividend_payout": (35, "Dividend payout ratio", "%",
                        "common share dividends / net income attributable to common shares", payout),
    "retention_rate": (36, "Retention rate", "%", "1 - dividend payout ratio", lambda i: 100 - payout(i)),
    "sustainable_growth_rate": (37, "Sustainable growth rate", "%", "retention rate x return on equity",
                                lambda i: (100 - payout(i)) / 100 * roe(i)),
    "earnings_per_share": (38, "Earnings per share (basic)", "USD/shares",
                           "(net income - preferred dividends) / weighted average shares outstanding",
                           lambda i: div(income_to_common(i), i.v("weighted_shares_basic"))),
    "book_value_per_share": (39, "Book value per share", "USD/shares",
                             "common stockholders' equity / common shares outstanding",
                             lambda i: div(equity(i, i.year), i.v("shares_outstanding"))),
    "free_cash_flow_to_equity": (40, "Free cash flow to equity (FCFE)", "USD",
                                 "operating cash flow - investment in fixed capital + net borrowing", fcfe),
    "free_cash_flow_to_firm": (41, "Free cash flow to the firm (FCFF)", "USD",
                               "operating cash flow + interest expense x (1 - tax rate) - investment in fixed capital",
                               fcff),
    # Common stock-pitch measures that are not on the CFA list.
    "ebitda": (None, "EBITDA", "USD", "operating income + depreciation and amortization", ebitda),
    "ebitda_margin": (None, "EBITDA margin", "%", "EBITDA / total revenue",
                      lambda i: div(ebitda(i), i.v("revenue")) * 100),
    "free_cash_flow": (None, "Free cash flow", "USD", "operating cash flow - capital expenditures", free_cash_flow),
    "free_cash_flow_margin": (None, "Free cash flow margin", "%", "free cash flow / total revenue",
                              lambda i: div(free_cash_flow(i), i.v("revenue")) * 100),
    "net_debt": (None, "Net debt", "USD", "total debt - cash - short-term marketable investments", net_debt),
    "net_debt_to_ebitda": (None, "Net debt / EBITDA", "x", "net debt / EBITDA", lambda i: div(net_debt(i), ebitda(i))),
    "rd_to_revenue": (None, "R&D as % of revenue", "%", "research and development / total revenue",
                      lambda i: div(i.v("research_and_development"), i.v("revenue")) * 100),
    "capex_to_revenue": (None, "Capex as % of revenue", "%", "capital expenditures / total revenue",
                         lambda i: div(i.v("capex"), i.v("revenue")) * 100),
    "effective_tax_rate": (None, "Effective tax rate", "%", "income tax expense / pretax income",
                           lambda i: tax_rate(i) * 100),
}

ALIASES = {
    "roe": "return_on_equity", "roa": "return_on_assets", "roic": "return_on_invested_capital",
    "roce": "return_on_common_equity", "dso": "days_sales_outstanding", "doh": "days_inventory_on_hand",
    "dpo": "days_payables", "ccc": "cash_conversion_cycle", "eps": "earnings_per_share",
    "bvps": "book_value_per_share", "fcfe": "free_cash_flow_to_equity", "fcff": "free_cash_flow_to_firm",
    "fcf": "free_cash_flow", "gross_margin": "gross_profit_margin", "operating_margin": "operating_profit_margin",
    "net_margin": "net_profit_margin", "equity_multiplier": "financial_leverage",
    "payout_ratio": "dividend_payout", "dividend_payout_ratio": "dividend_payout",
    "asset_turnover": "total_asset_turnover", "debt_to_equity_ratio": "debt_to_equity",
    "interest_coverage_ratio": "interest_coverage", "fcf_margin": "free_cash_flow_margin",
    "sustainable_growth": "sustainable_growth_rate", "net_debt_ebitda": "net_debt_to_ebitda",
}


class UnknownRatio(ValueError):
    """The name is not a supported ratio. A caller bug, not missing evidence."""


def ratio_name(name: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    key = ALIASES.get(key, key)
    if key not in RATIOS:
        raise UnknownRatio(f"Unknown ratio '{name}'. Supported: {', '.join(RATIOS)}")
    return key


def compute_ratio(store: FactStore, company: str, year: int, name: str) -> dict:
    """Compute one ratio. An unknown ratio name raises; missing data returns insufficient_evidence."""
    key = ratio_name(name)
    number, label, unit, formula, fn = RATIOS[key]
    inputs = Inputs(store, company, year)
    base = {"ratio": key, "cfa_number": number, "label": label, "company": inputs.company,
            "fiscal_year": year, "unit": unit, "formula": formula}
    try:
        value = fn(inputs)
    except EvidenceError as error:
        # Missing from THIS loaded dataset; not a statement about public filings in general.
        return dict(base, status="insufficient_evidence", reason=str(error),
                    observation_ids=sorted(inputs.used))
    source_ids = sorted({row["source_id"] for row in inputs.used.values()})
    return dict(base, status="ok", value=value,
                observation_ids=sorted(inputs.used), observations=list(inputs.used.values()),
                sources={source_id: store.read_source(source_id) for source_id in source_ids},
                assumptions=list(dict.fromkeys(inputs.assumptions)))


def growth(store: FactStore, company: str, year: int, metric: str) -> dict:
    """Year-over-year growth of one metric, in percent."""
    metric = normalize_metric(metric)
    inputs = Inputs(store, company, year)
    base = {"ratio": f"{metric}_growth", "company": inputs.company, "fiscal_year": year, "unit": "%",
            "formula": f"{metric} FY{year} / {metric} FY{year - 1} - 1"}
    try:
        value = calculate_growth(inputs.obs(metric, year - 1), inputs.obs(metric, year))
    except EvidenceError as error:
        return dict(base, status="insufficient_evidence", reason=str(error),
                    observation_ids=sorted(inputs.used))
    source_ids = sorted({row["source_id"] for row in inputs.used.values()})
    return dict(base, status="ok", value=value, observation_ids=sorted(inputs.used),
                observations=list(inputs.used.values()),
                sources={source_id: store.read_source(source_id) for source_id in source_ids},
                assumptions=[])
