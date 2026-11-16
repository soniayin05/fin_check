import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fincheck.facts import METRICS
from fincheck.ratios import RATIOS, UnknownRatio, compute_ratio, growth
from fincheck.tools import FactStore

VALUES = {  # (FY2023, FY2024) simple numbers so expected ratios can be checked by hand
    "revenue": (1000, 1200), "cost_of_revenue": (400, 480), "gross_profit": (600, 720),
    "research_and_development": (100, 120), "operating_income": (300, 360), "interest_expense": (10, 12),
    "pretax_income": (290, 350), "income_tax_expense": (58, 70), "net_income": (232, 280),
    "preferred_dividends": (0, 0), "eps_basic": (2.32, 2.8), "eps_diluted": (2.3, 2.75),
    "weighted_shares_basic": (100, 100), "weighted_shares_diluted": (101, 102),
    "operating_cash_flow": (310, 400), "capex": (90, 100), "depreciation_amortization": (50, 60),
    "dividends_paid": (58, 70), "debt_issued": (0, 50), "debt_repaid": (20, 30),
    "short_term_debt_net_change": (0, 5), "interest_paid": (9, 11), "lease_payments": (20, 24),
    "cash": (100, 150), "short_term_investments": (50, 50), "receivables": (100, 140),
    "inventory": (40, 60), "current_assets": (350, 450), "net_ppe": (500, 600),
    "total_assets": (1500, 1700), "accounts_payable": (80, 100), "current_liabilities": (200, 250),
    "total_liabilities": (700, 800), "commercial_paper": (10, 20), "short_term_borrowings": (0, 0),
    "long_term_debt_current": (20, 30), "long_term_debt_noncurrent": (270, 250),
    "shareholders_equity": (800, 900), "shares_outstanding": (100, 100),
}


def payload(skip=()):
    rows = []
    for metric, (period_type, unit, _) in METRICS.items():
        for index, year in enumerate((2023, 2024)):
            if (metric, year) in skip or metric in skip:
                continue
            end = f"{year}-12-31"
            rows.append({"id": f"test-{metric}-{year}", "company": "TEST", "metric": metric,
                         "value": VALUES[metric][index], "unit": unit, "fiscal_year": year,
                         "period_start": end if period_type == "instant" else f"{year}-01-01",
                         "period_end": end, "source_id": "src", "period_type": period_type})
    return {"sources": {"src": {"url": "https://example.com"}}, "observations": rows}


def store(skip=()):
    with TemporaryDirectory() as directory:
        path = Path(directory) / "facts.json"
        path.write_text(json.dumps(payload(skip)))
        return FactStore(path)


class RatioTests(unittest.TestCase):
    def setUp(self):
        self.store = store()

    def value(self, name, year=2024):
        result = compute_ratio(self.store, "TEST", year, name)
        self.assertEqual(result["status"], "ok", result)
        return result["value"]

    def test_list_is_complete_and_every_ratio_computes(self):
        numbers = sorted(number for number, *_ in RATIOS.values() if number)
        self.assertEqual(numbers, list(range(1, 42)))
        self.assertEqual(set(VALUES), set(METRICS))
        for name in RATIOS:
            result = compute_ratio(self.store, "TEST", 2024, name)
            self.assertEqual(result["status"], "ok", (name, result))
            self.assertTrue(result["observation_ids"] and result["sources"], name)

    def test_liquidity_and_margins(self):
        self.assertAlmostEqual(self.value("current_ratio"), 450 / 250)
        self.assertAlmostEqual(self.value("quick_ratio"), (150 + 50 + 140) / 250)
        self.assertAlmostEqual(self.value("cash_ratio"), 200 / 250)
        self.assertAlmostEqual(self.value("gross_profit_margin"), 60.0)
        self.assertAlmostEqual(self.value("net_profit_margin"), 280 / 1200 * 100)

    def test_activity_ratios_use_average_balances_and_period_days(self):
        turnover = 1200 / ((100 + 140) / 2)
        self.assertAlmostEqual(self.value("receivables_turnover"), turnover)
        self.assertAlmostEqual(self.value("DSO"), 366 / turnover)  # 2024 is a leap year
        purchases = 480 + 60 - 40
        payables_turnover = purchases / 90
        self.assertAlmostEqual(self.value("payables_turnover"), payables_turnover)
        ccc = 366 / (480 / 50) + 366 / turnover - 366 / payables_turnover
        self.assertAlmostEqual(self.value("cash conversion cycle"), ccc)

    def test_returns_and_dupont_pieces(self):
        self.assertAlmostEqual(self.value("ROE"), 280 / 850 * 100)
        self.assertAlmostEqual(self.value("return_on_assets"), 280 / 1600 * 100)
        ebit = 350 + 12
        capital = ((300 + 800) + (300 + 900)) / 2
        self.assertAlmostEqual(self.value("return_on_invested_capital_pretax"), ebit / capital * 100)
        self.assertAlmostEqual(self.value("roic"), ebit * (1 - 70 / 350) / capital * 100)
        product = (self.value("tax_burden") * self.value("interest_burden") * self.value("ebit_margin") / 100
                   * self.value("total_asset_turnover") * self.value("financial_leverage"))
        self.assertAlmostEqual(product * 100, self.value("roe"))

    def test_solvency_payout_and_cash_flow(self):
        self.assertAlmostEqual(self.value("total_debt"), 250 + 30 + 20 + 0)
        self.assertAlmostEqual(self.value("debt_to_capital"), 300 / 1200)
        self.assertAlmostEqual(self.value("interest_coverage"), 362 / 12)
        self.assertAlmostEqual(self.value("fixed_charge_coverage"), (362 + 24) / (12 + 24))
        self.assertAlmostEqual(self.value("dividend_payout"), 25.0)
        self.assertAlmostEqual(self.value("sustainable_growth_rate"), 0.75 * 280 / 850 * 100)
        self.assertAlmostEqual(self.value("eps"), 2.8)
        self.assertAlmostEqual(self.value("bvps"), 9.0)
        self.assertAlmostEqual(self.value("fcfe"), 400 - 100 + (50 - 30 + 5))
        self.assertAlmostEqual(self.value("fcff"), 400 + 12 * (1 - 0.2) - 100)
        self.assertAlmostEqual(self.value("ebitda"), 420)
        self.assertAlmostEqual(self.value("net debt / ebitda"), (300 - 200) / 420)

    def test_missing_prior_year_is_insufficient_not_a_number(self):
        result = compute_ratio(self.store, "TEST", 2023, "return_on_equity")  # needs FY2022 equity
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertNotIn("value", result)
        self.assertEqual(compute_ratio(self.store, "TEST", 2023, "current_ratio")["status"], "ok")

    def test_optional_components_are_disclosed_as_assumptions(self):
        result = compute_ratio(store(skip=("commercial_paper", "gross_profit")), "TEST", 2024, "debt_to_equity")
        self.assertEqual(result["status"], "ok")
        self.assertAlmostEqual(result["value"], 280 / 900)
        self.assertTrue(any("commercial_paper FY2024" in note for note in result["assumptions"]))
        derived = compute_ratio(store(skip=("gross_profit",)), "TEST", 2024, "gross_margin")
        self.assertAlmostEqual(derived["value"], 60.0)
        self.assertTrue(any("revenue minus cost" in note for note in derived["assumptions"]))

    def test_required_input_missing_and_zero_denominator(self):
        missing = compute_ratio(store(skip=("long_term_debt_noncurrent",)), "TEST", 2024, "debt_to_equity")
        self.assertEqual(missing["status"], "insufficient_evidence")
        no_interest = compute_ratio(store(skip=("interest_expense",)), "TEST", 2024, "ebit_margin")
        self.assertEqual(no_interest["status"], "insufficient_evidence")

    def test_unknown_ratio_is_an_error_not_a_verdict(self):
        with self.assertRaises(UnknownRatio):
            compute_ratio(self.store, "TEST", 2024, "ebitda_xyz")

    def test_growth_of_flows_and_balances(self):
        self.assertAlmostEqual(growth(self.store, "TEST", 2024, "revenue")["value"], 20.0)
        self.assertAlmostEqual(growth(self.store, "TEST", 2024, "total_assets")["value"], (1700 / 1500 - 1) * 100)
        self.assertEqual(growth(self.store, "TEST", 2023, "revenue")["status"], "insufficient_evidence")


if __name__ == "__main__":
    unittest.main()
