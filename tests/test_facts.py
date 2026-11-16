import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))  # for sec_fixtures under any test runner

from fincheck.facts import METRICS, NormalizationError, coverage_table, merge, normalize
from fincheck.tools import FactStore, calculate_growth

from sec_fixtures import FILED, accn, companyfacts, concept

SAMPLE = Path(__file__).resolve().parents[1] / "data/sample/financial_facts.json"


class NormalizeTests(unittest.TestCase):
    def setUp(self):
        self.result = normalize(companyfacts(), "MSFT", 789019, raw_sha256="abc")
        self.obs = {row["id"]: row for row in self.result["observations"]}

    def test_matches_manually_verified_sample(self):
        sample = {row["id"]: row for row in json.loads(SAMPLE.read_text())["observations"]}
        for year in (2023, 2024):
            self.assertEqual(self.obs[f"msft-revenue-{year}"]["value"] / 1e6, sample[f"msft-revenue-{year}"]["value"])
            self.assertEqual(self.obs[f"msft-revenue-{year}"]["period_start"], sample[f"msft-revenue-{year}"]["period_start"])

    def test_fiscal_year_comes_from_dates_not_fy_field(self):
        row = self.obs["msft-revenue-2023"]  # also reported with fy=2024 as a comparative
        self.assertEqual((row["fiscal_year"], row["period_end"]), (2023, "2023-06-30"))
        self.assertEqual(row["accession"], accn(2024))  # most recently filed report wins
        self.assertFalse(row["restated"])

    def test_nine_month_and_quarterly_values_are_rejected(self):
        values = [row["value"] for row in self.result["observations"] if row["metric"] == "revenue"]
        self.assertNotIn(180000e6, values)
        self.assertNotIn(64727e6, values)

    def test_tag_fallback_is_recorded_per_year(self):
        self.assertEqual(self.obs["msft-revenue-2022"]["tag"], "Revenues")
        self.assertEqual(self.obs["msft-revenue-2024"]["tag"], "RevenueFromContractWithCustomerExcludingAssessedTax")

    def test_restatement_uses_latest_filing_and_is_flagged(self):
        row = self.obs["msft-net_income-2022"]
        self.assertEqual((row["value"], row["restated"], row["filed"]), (90e6, True, FILED[2023]))

    def test_conflict_in_one_filing_is_reported_not_chosen(self):
        self.assertNotIn("msft-operating_income-2024", self.obs)
        entry = next(r for r in self.result["coverage"]
                     if (r["metric"], r["fiscal_year"]) == ("operating_income", 2024))
        self.assertEqual(entry["status"], "conflict")
        self.assertIn("msft-operating_income-2023", self.obs)

    def test_balances_only_at_fiscal_year_end(self):
        assets = {row["fiscal_year"]: row for row in self.result["observations"] if row["metric"] == "total_assets"}
        self.assertEqual(sorted(assets), [2023, 2024])
        self.assertEqual(assets[2024]["period_type"], "instant")
        self.assertEqual(assets[2024]["period_start"], assets[2024]["period_end"])

    def test_missing_metric_is_reported_and_every_observation_is_sourced(self):
        missing = [r for r in self.result["coverage"] if r["metric"] == "inventory"]
        self.assertTrue(missing and all(r["status"] == "missing" for r in missing))
        for row in self.result["observations"]:
            source = self.result["sources"][row["source_id"]]
            self.assertTrue(row["accession"] and row["filed"] and row["tag"] and source["url"].startswith("https://www.sec.gov/"))
        self.assertIn("ok", coverage_table(self.result["coverage"]))

    def test_units_other_than_usd(self):
        self.assertEqual(self.obs["msft-eps_diluted-2024"]["unit"], "USD/shares")
        self.assertEqual(self.obs["msft-eps_diluted-2024"]["value"], 11.80)

    def test_output_loads_into_fact_store_and_growth_matches(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "observations.json"
            path.write_text(json.dumps(merge([self.result])))
            store = FactStore(path)
            growth = calculate_growth(store.get_financial_fact("Microsoft", "revenue", 2023),
                                      store.get_financial_fact("Microsoft", "revenue", 2024))
            self.assertAlmostEqual(growth, 15.66996, places=4)
            assets = calculate_growth(store.get_financial_fact("MSFT", "total_assets", 2023),
                                      store.get_financial_fact("MSFT", "total_assets", 2024))
            self.assertAlmostEqual(assets, (512163 / 411976 - 1) * 100, places=6)

    def test_keeps_only_recent_years(self):
        result = normalize(companyfacts(), "MSFT", 789019, keep_years=2)
        self.assertEqual({row["fiscal_year"] for row in result["observations"]}, {2023, 2024})

    def test_ambiguous_calendar_fails_clearly(self):
        payload = companyfacts()
        payload["facts"]["us-gaap"]["Revenues"]["units"]["USD"].append(
            {"start": "2023-06-01", "end": "2024-05-31", "val": 1, "accn": accn(2024), "fy": 2024,
             "fp": "FY", "form": "10-K", "filed": FILED[2024]})
        with self.assertRaises(NormalizationError):
            normalize(payload, "MSFT", 789019)
        with self.assertRaises(NormalizationError):
            normalize({"facts": {"us-gaap": {"Assets": concept([])}}}, "MSFT", 789019)

    def test_metric_specs_are_well_formed(self):
        for metric, (period_type, unit, tags) in METRICS.items():
            self.assertIn(period_type, {"duration", "instant"}, metric)
            self.assertIn(unit, {"USD", "shares", "USD/shares"}, metric)
            self.assertTrue(tags and len(tags) == len(set(tags)), metric)


if __name__ == "__main__":
    unittest.main()
