"""Small deterministic tools that a future LLM can call.

Only annual, positive-base growth comparisons are supported in this starter.
Dates and sources travel with values so the caller can audit the calculation.
"""

from dataclasses import asdict, dataclass
from datetime import date
from math import isfinite
import json
from pathlib import Path


class EvidenceError(ValueError):
    """Evidence is missing, ambiguous, or unsuitable for this calculation."""


def normalize_company(company: str) -> str:
    name = company.strip().casefold().rstrip(".")
    return "MSFT" if name in {"msft", "microsoft", "microsoft corporation", "microsoft corp"} else company.strip().upper()


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

    def validate_annual(self) -> None:
        days = (date.fromisoformat(self.period_end)
                - date.fromisoformat(self.period_start)).days + 1
        if not 350 <= days <= 380:
            raise EvidenceError(f"{self.id}: expected an annual period, got {days} days")
        if not isfinite(self.value):
            raise EvidenceError(f"{self.id}: value must be finite")


class FactStore:
    def __init__(self, path: Path):
        payload = json.loads(path.read_text())
        self.sources = payload["sources"]
        self.observations = [Observation(**row) for row in payload["observations"]]
        ids = [row.id for row in self.observations]
        if len(ids) != len(set(ids)):
            raise EvidenceError("Duplicate observation IDs")
        for row in self.observations:
            if row.source_id not in self.sources:
                raise EvidenceError(f"Missing source for {row.id}")

    def get_financial_fact(self, company: str, metric: str, year: int) -> Observation:
        company = normalize_company(company)
        rows = [row for row in self.observations
                if (row.company, row.metric, row.fiscal_year) == (company, metric, year)]
        if len(rows) != 1:
            raise EvidenceError(
                f"Expected one {company}/{metric}/{year} observation; found {len(rows)}")
        rows[0].validate_annual()
        return rows[0]

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
