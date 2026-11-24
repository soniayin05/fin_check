"""Numeric claims checked in Python, with explicit units and rounding precision."""

from dataclasses import asdict
from decimal import Decimal
from math import isfinite
import re

from .ratios import compute_ratio, growth
from .tools import EvidenceError, FactStore, normalize_company, normalize_metric


# Dimension and multiplier into its base unit. Percent values use percentage points.
UNITS = {
    "USD": ("USD", 1), "USD_thousands": ("USD", 1000),
    "USD_millions": ("USD", 10**6), "USD_billions": ("USD", 10**9),
    "shares": ("shares", 1), "shares_millions": ("shares", 10**6),
    "USD/shares": ("USD/shares", 1), "%": ("%", 1), "x": ("x", 1), "days": ("days", 1),
}
NUMBER = re.compile(r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")


def financial_fact(store: FactStore, company: str, year: int, metric: str) -> dict:
    metric = normalize_metric(metric)
    base = {"company": normalize_company(company), "fiscal_year": year, "metric": metric,
            "formula": "reported observation"}
    try:
        row = store.get_financial_fact(company, metric, year)
    except EvidenceError as error:
        return dict(base, status="insufficient_evidence", reason=str(error), sources={}, observations=[])
    return dict(base, status="ok", value=row.value, unit=row.unit, observations=[asdict(row)],
                observation_ids=[row.id], sources={row.source_id: store.read_source(row.source_id)},
                assumptions=[])


def calculate(store: FactStore, company: str, year: int, operation: str, measure: str,
              claimed_value: str, unit: str) -> dict:
    """Check equality to the precision written in a claim, e.g. 245.1 USD_billions.

    The caller supplies the literal from the original claim. It cannot choose a
    tolerance. Half of the last written decimal place defines the rounding interval.
    """
    if not isinstance(claimed_value, str) or len(claimed_value) > 64 or not NUMBER.fullmatch(claimed_value):
        raise ValueError("claimed_value must be a decimal literal copied from the claim")
    if unit not in UNITS:
        raise ValueError(f"Unsupported claim unit: {unit}")
    if operation == "fact":
        result = financial_fact(store, company, year, measure)
    elif operation == "growth":
        result = growth(store, company, year, measure)
    elif operation == "ratio":
        result = compute_ratio(store, company, year, measure)
    else:
        raise ValueError("operation must be fact, growth or ratio")
    result = dict(result, operation=operation, measure=measure, claimed_value=claimed_value, claimed_unit=unit)
    if result["status"] != "ok":
        return dict(result, verdict="insufficient_evidence")
    if result["unit"] not in UNITS or UNITS[result["unit"]][0] != UNITS[unit][0]:
        raise ValueError(f"Claim unit {unit} is incompatible with evidence unit {result['unit']}")
    if not isfinite(result["value"]):
        raise ValueError("Calculation returned a non-finite value")
    # Any substituted zero or approximate D&A needs further source verification.
    uncertain = [note for note in result["assumptions"] if "treated as 0" in note or "D&A =" in note]
    if uncertain:
        return dict(result, status="insufficient_evidence", verdict="insufficient_evidence",
                    reason="Calculation depends on missing or approximate inputs: " + "; ".join(uncertain))
    claimed = Decimal(claimed_value.replace(",", ""))
    factor = Decimal(UNITS[unit][1])
    actual = Decimal(str(result["value"])) * Decimal(UNITS[result["unit"]][1])
    tolerance = Decimal("0.5") * Decimal(10) ** claimed.as_tuple().exponent * factor
    supported = abs(actual - claimed * factor) <= tolerance
    return dict(result, verdict="supported" if supported else "contradicted",
                verified_value=float(actual / factor), verified_unit=unit,
                tolerance=float(tolerance / factor))
