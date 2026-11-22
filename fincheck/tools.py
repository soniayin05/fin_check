"""Small deterministic tools that a future LLM can call.

Only annual, positive-base growth comparisons are supported in this starter.
Dates and sources travel with values so the caller can audit the calculation.
"""

from dataclasses import asdict, dataclass
from datetime import date
from math import isfinite
import json
from pathlib import Path
import re

from .facts import METRICS


class EvidenceError(ValueError):
    """Evidence is missing, ambiguous, or unsuitable for this calculation."""


class MissingEvidence(EvidenceError):
    """No observation was loaded for the requested company, metric and year."""


class UnknownMetric(ValueError):
    """The requested metric is not part of the supported financial vocabulary."""


def normalize_metric(metric: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", metric.strip().lower()).strip("_")
    aliases = {"total_revenue": "revenue", "revenues": "revenue", "net_sales": "revenue",
               "cash_flow_from_operations": "operating_cash_flow", "cfo": "operating_cash_flow",
               "diluted_eps": "eps_diluted", "basic_eps": "eps_basic"}
    key = aliases.get(key, key)
    if key not in METRICS:
        raise UnknownMetric(f"Unknown metric '{metric}'. Supported: {', '.join(METRICS)}")
    return key


COMPANY_ALIASES = {
    "MSFT": {"msft", "microsoft", "microsoft corporation", "microsoft corp"},
    "GOOGL": {"googl", "goog", "google", "alphabet", "alphabet inc", "alphabet inc. (google)"},
    "AAPL": {"aapl", "apple", "apple inc", "apple computer"},
}


def normalize_company(company: str) -> str:
    name = company.strip().casefold().rstrip(".")
    for ticker, aliases in COMPANY_ALIASES.items():
        if name in aliases:
            return ticker
    return company.strip().upper()


@dataclass(frozen=True)
class Observation:
    id: str
    company: str
    metric: str
    value: float
    unit: str
    fiscal_year: int
    period_start: str
    period_end: str
    source_id: str
    # "duration" = a fiscal-year flow (income statement, cash flow).
    # "instant" = a balance at fiscal year-end (balance sheet); start == end.
    period_type: str = "duration"
    tag: str | None = None          # XBRL concept the value came from
    accession: str | None = None    # SEC accession number of the filing used
    form: str | None = None
    filed: str | None = None
    restated: bool = False          # an earlier filing reported a different value

    def validate_annual(self) -> None:
        if type(self.value) not in (int, float) or not isfinite(self.value):
            raise EvidenceError(f"{self.id}: value must be finite")
        if self.period_type == "instant":
            if self.period_start != self.period_end:
                raise EvidenceError(f"{self.id}: an instant needs one balance date")
            return
        if self.period_type != "duration":
            raise EvidenceError(f"{self.id}: unknown period type {self.period_type}")
        days = (date.fromisoformat(self.period_end)
                - date.fromisoformat(self.period_start)).days + 1
        if not 350 <= days <= 380:
            raise EvidenceError(f"{self.id}: expected an annual period, got {days} days")


class FactStore:
    def __init__(self, path: Path):
        payload = json.loads(path.read_text())
        self.sources = payload["sources"]
        coverage = payload.get("coverage", [])
        coverage_path = path.with_name("coverage.json")
        if path.name == "observations.json" and coverage_path.exists():
            coverage = json.loads(coverage_path.read_text()).get("coverage", coverage)
        self.coverage = {(r["company"], r["metric"], r["fiscal_year"]): r for r in coverage}
        self.observations = [Observation(**row) for row in payload["observations"]]
        ids = [row.id for row in self.observations]
        if len(ids) != len(set(ids)):
            raise EvidenceError("Duplicate observation IDs")
        for row in self.observations:
            if row.source_id not in self.sources:
                raise EvidenceError(f"Missing source for {row.id}")

    def get_financial_fact(self, company: str, metric: str, year: int) -> Observation:
        company = normalize_company(company)
        metric = normalize_metric(metric)
        coverage = self.coverage.get((company, metric, year), {})
        if coverage.get("status") == "conflict":
            raise EvidenceError(f"Conflicting {company}/{metric}/{year} evidence: {coverage.get('detail', '')}")
        rows = [row for row in self.observations
                if (row.company, row.metric, row.fiscal_year) == (company, metric, year)]
        if not rows:
            raise MissingEvidence(f"Expected one {company}/{metric}/{year} observation; found 0")
        if len(rows) != 1:
            raise EvidenceError(
                f"Expected one {company}/{metric}/{year} observation; found {len(rows)}")
        rows[0].validate_annual()
        return rows[0]

    def known_metrics(self, company: str | None = None) -> list[str]:
        company = normalize_company(company) if company else None
        return sorted({row.metric for row in self.observations
                       if company is None or row.company == company})

    def read_source(self, source_id: str) -> dict:
        if source_id not in self.sources:
            raise EvidenceError(f"Unknown source: {source_id}")
        return self.sources[source_id]


def calculate_growth(prior: Observation, current: Observation) -> float:
    """Return percent growth, e.g. 20.0 for a change from 100 to 120."""
    prior.validate_annual()
    current.validate_annual()
    if (prior.company, prior.metric, prior.unit) != (
            current.company, current.metric, current.unit):
        raise EvidenceError("Company, metric, or unit mismatch")
    if current.fiscal_year != prior.fiscal_year + 1:
        raise EvidenceError("Expected consecutive fiscal years")
    if prior.period_type != current.period_type:
        raise EvidenceError("Cannot compare a balance with a flow")
    if current.period_type == "instant":
        apart = (date.fromisoformat(current.period_end)
                 - date.fromisoformat(prior.period_end)).days
        if not 350 <= apart <= 380:
            raise EvidenceError("Balance dates must be one fiscal year apart")
    else:
        gap = (date.fromisoformat(current.period_start)
               - date.fromisoformat(prior.period_end)).days
        if gap != 1:
            raise EvidenceError("Annual periods must be consecutive and non-overlapping")
    if prior.value <= 0:
        raise EvidenceError("Zero/negative-base growth requires explicit interpretation")
    return (current.value / prior.value - 1) * 100


def compare_growth(store: FactStore, company: str, year: int,
                   left_metric: str, right_metric: str) -> dict:
    """Check a structured assertion that left growth is strictly greater.

    This is a tool demonstration, not an LLM or natural-language parser.
    Missing evidence means missing from THIS loaded sample, not all public filings.
    """
    company = normalize_company(company)
    left_metric, right_metric = normalize_metric(left_metric), normalize_metric(right_metric)
    try:
        left = [store.get_financial_fact(company, left_metric, y) for y in (year - 1, year)]
        right = [store.get_financial_fact(company, right_metric, y) for y in (year - 1, year)]
        for a, b in zip(left, right):
            if (a.period_start, a.period_end, a.unit) != (b.period_start, b.period_end, b.unit):
                raise EvidenceError("Compared metrics must cover the same periods and units")
        lhs = calculate_growth(*left)
        rhs = calculate_growth(*right)
        source_ids = sorted({row.source_id for row in left + right})
        return {
            "company": company, "fiscal_year": year,
            "verdict": "supported" if lhs > rhs else "contradicted",
            "left_metric": left_metric, "left_growth_pct": lhs,
            "right_metric": right_metric, "right_growth_pct": rhs,
            "observation_ids": [row.id for row in left + right],
            "observations": [asdict(row) for row in left + right],
            "sources": {key: store.read_source(key) for key in source_ids},
        }
    except EvidenceError as error:
        return {"verdict": "insufficient_evidence", "reason": str(error),
                "sources": {}, "observation_ids": []}
