"""Deterministic protocol/CLI checks. Scripted calls are not live-model scores."""

from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fincheck.__main__ import facts_path, main
from fincheck.agent import Agent, validate_args
from fincheck.calculations import calculate
from fincheck.ratios import compute_ratio
from fincheck.retrieval import PassageStore
from fincheck.tools import EvidenceError, FactStore, UnknownMetric, compare_growth
from test_agent import COMPARE, FINAL, PLAN, ScriptedModel
from test_ratios import store as ratio_store

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "data/sample/financial_facts.json"


def call(value="245.1", **changes):
    args = {"company": "Microsoft", "year": 2024, "operation": "fact", "measure": "Revenue",
            "claimed_value": value, "unit": "USD_billions"}
    args.update(changes)
    return {"name": "calculate", "args": args}


def final(verdict="supported", **changes):
    args = {"verdict": verdict, "explanation": "Model draft.", "calc_id": "calc-1",
            "evidence_ids": ["msft-ar2024-income"]}
    args.update(changes)
    return {"name": "submit_verdict", "args": args}


class NumericStageTests(unittest.TestCase):
    def setUp(self):
        self.facts = FactStore(SAMPLE)

    def run_agent(self, claim, calls, turns=10, facts=None):
        agent = Agent(facts or self.facts, PassageStore(ROOT / "tests/no-passages.json"),
                      ScriptedModel(calls), max_turns=turns)
        return agent.run(claim)

    def test_rounded_value_and_false_value_in_billions(self):
        for literal, verdict in [("245.1", "supported"), ("250", "contradicted")]:
            with self.subTest(literal=literal):
                result = calculate(self.facts, "MSFT", 2024, "fact", "revenue", literal, "USD_billions")
                self.assertEqual(result["verdict"], verdict)
                self.assertAlmostEqual(result["verified_value"], 245.122)
        exact = calculate(self.facts, "MSFT", 2024, "fact", "revenue", "245,122", "USD_millions")
        self.assertEqual(exact["verdict"], "supported")
        precision = calculate(self.facts, "MSFT", 2024, "fact", "revenue", "245.100", "USD_billions")
        self.assertEqual(precision["verdict"], "contradicted")

    def test_metric_aliases_and_unknown_metric(self):
        result = compare_growth(self.facts, "Microsoft", 2024, "Revenue", "operating cash flow")
        self.assertEqual(result["verdict"], "contradicted")
        with self.assertRaises(UnknownMetric):
            compare_growth(self.facts, "MSFT", 2024, "ebitda_xyz", "revenue")
        run = self.run_agent("Revenue was $245.1B.", [PLAN, call(measure="ebitda_xyz")], turns=2)
        self.assertEqual(run["status"], "incomplete")
        self.assertIn("Unknown metric", run["events"][-1]["output"]["error"])

    def test_growth_and_ratio_reuse_existing_tools(self):
        result = calculate(self.facts, "MSFT", 2024, "growth", "Revenue", "15.67", "%")
        self.assertEqual(result["verdict"], "supported")
        result = calculate(ratio_store(), "TEST", 2024, "ratio", "net_margin", "23.33", "%")
        self.assertEqual(result["verdict"], "supported")
        self.assertEqual(result["formula"], "net income / total revenue")

    def test_units_unknown_measure_and_nonfinite_arguments_are_errors(self):
        with self.assertRaises(ValueError):
            calculate(self.facts, "MSFT", 2024, "fact", "revenue", "245.1", "%")
        for value in ["NaN", "Infinity", "1e9", "245.1B", "9" * 100]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                calculate(self.facts, "MSFT", 2024, "fact", "revenue", value, "USD")
        for value in [float("nan"), float("inf")]:
            with self.assertRaises(ValueError):
                validate_args("submit_verdict", dict(FINAL["args"], left_growth_pct=value))

    def test_missing_period_is_insufficient_without_verified_number(self):
        result = calculate(self.facts, "MSFT", 2025, "fact", "revenue", "250", "USD_billions")
        self.assertEqual(result["verdict"], "insufficient_evidence")
        self.assertNotIn("verified_value", result)
        run = self.run_agent("Revenue was $250B in FY2025.", [PLAN, call("250", year=2025),
                             final("insufficient_evidence", evidence_ids=[])])
        self.assertEqual(run["status"], "complete")
        self.assertNotIn("verified_value", run["result"])

    def test_agent_copies_calculation_and_preserves_sources(self):
        for value, verdict in [("245.1", "supported"), ("250", "contradicted")]:
            run = self.run_agent(f"Microsoft revenue was ${value}B in FY2024.",
                                 [PLAN, call(value), final(verdict)])
            self.assertEqual(run["status"], "complete")
            self.assertEqual(run["result"]["verdict"], verdict)
            self.assertAlmostEqual(run["result"]["verified_value"], 245.122)
            self.assertIn("msft-ar2024-income", run["result"]["sources"])
            self.assertEqual(run["result"]["calculation"]["calc_id"], "calc-1")

    def test_rejects_altered_verdict_unknown_id_and_absent_citations(self):
        for finish in [final("contradicted"), final(calc_id="calc-99"), final(calc_id="calc-1", evidence_ids=[]),
                       final(calc_id="calc-1", left_growth_pct=99)]:
            with self.subTest(finish=finish):
                run = self.run_agent("Microsoft revenue was $245.1B in FY2024.",
                                     [PLAN, call(), finish], turns=3)
                self.assertEqual(run["status"], "incomplete")
                self.assertEqual(run["events"][-1]["status"], "error")

    def test_rejects_claimed_number_not_in_claim(self):
        run = self.run_agent("Microsoft revenue was $250B in FY2024.", [PLAN, call()], turns=2)
        self.assertIn("original claim", run["events"][-1]["output"]["error"])

    def test_invalid_numeric_attempt_cannot_finish_as_missing_evidence(self):
        run = self.run_agent("Microsoft revenue was $245.1B in FY2024.",
                             [PLAN, call(measure="ebitda_xyz"), final("insufficient_evidence", evidence_ids=[])],
                             turns=3)
        self.assertEqual(run["status"], "incomplete")
        self.assertIn("Call calculate", run["events"][-1]["output"]["error"])

    def test_numeric_fact_read_requires_calculation_before_verdict(self):
        read = {"name": "get_financial_fact", "args": {"company": "MSFT", "year": 2024, "metric": "Revenue"}}
        finish = final()
        finish["args"].pop("calc_id")
        run = self.run_agent("Revenue was $245.1B.", [PLAN, read, finish], turns=3)
        self.assertIn("Call calculate", run["events"][-1]["output"]["error"])

    def test_growth_comparison_supports_calc_id_and_old_protocol(self):
        args = dict(FINAL["args"], calc_id="calc-1")
        args.pop("left_growth_pct")
        args.pop("right_growth_pct")
        run = self.run_agent("Revenue grew faster than cash flow.",
                             [PLAN, COMPARE, {"name": "submit_verdict", "args": args}])
        self.assertEqual(run["status"], "complete")
        self.assertAlmostEqual(run["result"]["left_growth_pct"], 15.66996, places=4)

    def test_optional_input_errors_are_not_converted_to_zero(self):
        facts = ratio_store()
        target = next(r for r in facts.observations if r.metric == "commercial_paper" and r.fiscal_year == 2024)
        facts.observations.append(replace(target, id="duplicate"))
        result = compute_ratio(facts, "TEST", 2024, "debt_to_equity")
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertIn("found 2", result["reason"])

    def test_coverage_conflict_survives_missing_observation(self):
        facts = ratio_store(skip=("commercial_paper",))
        facts.coverage[("TEST", "commercial_paper", 2024)] = {"status": "conflict", "detail": "two values"}
        result = compute_ratio(facts, "TEST", 2024, "debt_to_equity")
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertIn("Conflicting", result["reason"])

    def test_assumption_dependent_ratio_requires_further_evidence(self):
        result = calculate(ratio_store(skip=("commercial_paper",)), "TEST", 2024,
                           "ratio", "debt_to_equity", "0.31", "x")
        self.assertEqual(result["verdict"], "insufficient_evidence")
        self.assertIn("treated as 0", result["reason"])

    def test_ratio_rejects_misaligned_dates_and_normalizes_millions(self):
        facts = ratio_store()
        facts.observations = [replace(r, period_start="2024-01-02")
                              if r.metric == "net_income" and r.fiscal_year == 2024 else r
                              for r in facts.observations]
        self.assertEqual(compute_ratio(facts, "TEST", 2024, "net_margin")["status"], "insufficient_evidence")
        facts = ratio_store()
        facts.observations = [replace(r, value=r.value / 10**6, unit="USD_millions")
                              if r.unit == "USD" else r for r in facts.observations]
        self.assertAlmostEqual(compute_ratio(facts, "TEST", 2024, "fcf")["value"], 300)

    def test_cli_selects_sec_for_checks_and_preserves_dev_sample(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            sec = root / "data/facts/observations.json"
            sec.parent.mkdir(parents=True)
            sec.write_text("{}")
            self.assertEqual(facts_path(root, "check", None), sec)
            self.assertEqual(facts_path(root, "ratio", None), sec)
            self.assertEqual(facts_path(root, "eval-agent-dev", None), root / "data/sample/financial_facts.json")
            self.assertEqual(facts_path(root, "check", SAMPLE), SAMPLE)

    def test_verify_number_command_does_not_call_model(self):
        argv = ["fincheck", "verify-number", "--facts", str(SAMPLE), "--company", "MSFT", "--year", "2024",
                "--measure", "revenue", "--value", "250", "--unit", "USD_billions"]
        with patch("sys.argv", argv), patch("builtins.print") as output, patch("fincheck.__main__.Gemini") as model:
            self.assertEqual(main(), 0)
            model.assert_not_called()
            result = json.loads(output.call_args.args[0])
            self.assertEqual(result["verdict"], "contradicted")

    def test_normal_cli_check_uses_numeric_tools(self):
        argv = ["fincheck", "check", "Microsoft revenue was $245.1B in FY2024.", "--facts", str(SAMPLE)]
        model = ScriptedModel([PLAN, call(), final()])
        with patch("sys.argv", argv), patch("fincheck.__main__.Gemini", return_value=model), \
                patch("fincheck.__main__.gemini_embedder", return_value=None), \
                patch("fincheck.__main__.save_run") as saved, patch("builtins.print"):
            self.assertEqual(main(), 0)
            run = saved.call_args.args[1]
            self.assertEqual(run["result"]["verdict"], "supported")
            self.assertEqual(run["facts_path"], str(SAMPLE))


if __name__ == "__main__":
    unittest.main()
