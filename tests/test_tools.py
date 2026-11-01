from dataclasses import replace
from pathlib import Path
import unittest

from fincheck.tools import EvidenceError, FactStore, calculate_growth, compare_growth

DATA = Path(__file__).resolve().parents[1] / "data/sample/financial_facts.json"


class FinancialToolTests(unittest.TestCase):
    def setUp(self):
        self.store = FactStore(DATA)
        self.prior = self.store.get_financial_fact("MSFT", "revenue", 2023)
        self.current = self.store.get_financial_fact("MSFT", "revenue", 2024)

    def test_known_growth(self):
        self.assertAlmostEqual(calculate_growth(self.prior, self.current), 15.66996, places=4)

    def test_rejects_nine_month_period(self):
        with self.assertRaises(EvidenceError):
            calculate_growth(self.prior, replace(self.current, period_start="2023-10-01"))

    def test_rejects_unit_mismatch(self):
        with self.assertRaises(EvidenceError):
            calculate_growth(self.prior, replace(self.current, unit="USD"))

    def test_rejects_zero_and_negative_base(self):
        for value in (0, -100):
            with self.subTest(value=value), self.assertRaises(EvidenceError):
                calculate_growth(replace(self.prior, value=value), self.current)

    def test_rejects_overlapping_annual_periods(self):
        with self.assertRaises(EvidenceError):
            calculate_growth(self.prior, replace(self.current, period_start="2023-06-01"))

    def test_rejects_metric_mismatch(self):
        with self.assertRaises(EvidenceError):
            calculate_growth(self.prior, replace(self.current, metric="net_income"))

    def test_missing_data_returns_insufficient(self):
        result = compare_growth(self.store, "MSFT", 2025, "revenue", "operating_cash_flow")
        self.assertEqual(result["verdict"], "insufficient_evidence")

    def test_real_comparison_includes_sources(self):
        result = compare_growth(self.store, "MSFT", 2024, "revenue", "operating_cash_flow")
        self.assertEqual(result["verdict"], "contradicted")
        self.assertEqual(len(result["sources"]), 2)


if __name__ == "__main__":
    unittest.main()
