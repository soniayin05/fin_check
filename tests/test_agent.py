"""Protocol tests use a scripted fake, not a live model evaluation."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fincheck.agent import Agent, save_run, validate_args
from fincheck.model import ModelError
from fincheck.retrieval import PassageStore, parse_filing
from fincheck.tools import FactStore

ROOT = Path(__file__).resolve().parent.parent
FACTS = ROOT / "data/sample/financial_facts.json"


class ScriptedModel:
    model = "test-only-scripted-provider"

    def __init__(self, calls):
        self.calls = iter(calls)
        self.requests = []

    def generate(self, messages, tools, instruction):
        self.requests.append(list(messages))
        call = next(self.calls)
        if isinstance(call, Exception):
            raise call
        return {"content": {"role": "model", "parts": [{"functionCall": call,
                                                          "thoughtSignature": "test-signature"}]},
                "usage": None, "retries": 0}


PLAN = {"name": "record_plan", "args": {"plan": "Compare matching annual growth rates."}}
COMPARE = {"name": "compare_growth", "args": {"company": "MSFT", "year": 2024,
           "left_metric": "revenue", "right_metric": "operating_cash_flow"}}
FINAL = {"name": "submit_verdict", "args": {"verdict": "contradicted", "explanation": "Revenue grew more slowly.",
         "evidence_ids": ["msft-ar2024-income", "msft-ar2024-cashflow"],
         "left_growth_pct": 15.66996, "right_growth_pct": 35.35658}}


class AgentTests(unittest.TestCase):
    def agent(self, calls, **kwargs):
        return Agent(FactStore(FACTS), PassageStore(ROOT / "tests/nonexistent.json"), ScriptedModel(calls), **kwargs)

    def test_complete_numeric_path_and_signature_preserved(self):
        agent = self.agent([PLAN, COMPARE, FINAL])
        run = agent.run("Revenue grew faster than cash flow.")
        self.assertEqual(run["status"], "complete")
        self.assertEqual(run["result"]["verdict"], "contradicted")
        self.assertEqual(agent.model.requests[1][1]["parts"][0]["thoughtSignature"], "test-signature")

    def test_plan_required_and_budget_stops(self):
        run = self.agent([COMPARE, COMPARE], max_calls=1).run("Check this")
        self.assertEqual(run["status"], "incomplete")
        self.assertIn("Record a plan", run["events"][1]["output"]["error"])

    def test_fake_citation_is_rejected(self):
        final = {"name": "submit_verdict", "args": dict(FINAL["args"], evidence_ids=["invented"])}
        run = self.agent([PLAN, COMPARE, final], max_turns=3).run("Check this")
        self.assertEqual(run["status"], "incomplete")
        self.assertIn("actually retrieved", run["events"][-1]["output"]["error"])

    def test_company_alias_and_observation_citations(self):
        compare = {"name": "compare_growth", "args": dict(COMPARE["args"], company="Microsoft")}
        final = {"name": "submit_verdict", "args": dict(FINAL["args"], evidence_ids=[
            "msft-revenue-2023", "msft-revenue-2024", "msft-cfo-2023", "msft-cfo-2024"])}
        run = self.agent([PLAN, compare, final]).run("Check this")
        self.assertEqual(run["status"], "complete")
        self.assertIn("msft-ar2024-cashflow", run["result"]["sources"])
        self.assertIn("35.36%", run["result"]["explanation"])

    def test_fabricated_calculation_is_rejected(self):
        final = {"name": "submit_verdict", "args": dict(FINAL["args"], left_growth_pct=99)}
        run = self.agent([PLAN, COMPARE, final], max_turns=3).run("Check this")
        self.assertEqual(run["status"], "incomplete")

    def test_model_failure_is_not_a_factual_verdict(self):
        run = self.agent([ModelError("Connection failed")]).run("Check this")
        self.assertEqual(run["status"], "error")
        self.assertNotIn("verdict", run["result"])

    def test_disclosure_does_not_display_uncomputed_growth(self):
        search = {"name": "search_filings", "args": {"query": "Azure", "company": "MSFT", "year": 2024}}
        final = {"name": "submit_verdict", "args": {"verdict": "supported", "explanation": "Source states this.",
                 "evidence_ids": ["passage-1"], "left_growth_pct": 0, "right_growth_pct": 0}}
        agent = self.agent([PLAN, search, final])
        agent.passages.passages = [{"id": "passage-1", "company": "MSFT", "year": 2024,
                                   "text": "Azure and other cloud services revenue grew 30%."}]
        run = agent.run("Azure revenue grew 30%.")
        self.assertEqual(run["status"], "complete")
        self.assertNotIn("left_growth_pct", run["result"])

    def test_arguments_cannot_supply_gold_or_bool_year(self):
        for args in [dict(COMPARE["args"], expected="supported"), dict(COMPARE["args"], year=True)]:
            with self.assertRaises(ValueError):
                validate_args("compare_growth", args)

    def test_saved_trace_redacts_configured_secret(self):
        with TemporaryDirectory() as directory, patch.dict("os.environ", {"GEMINI_API_KEY": "test-secret"}):
            path = save_run(Path(directory), {"claim": "test-secret"}, FACTS)
            self.assertNotIn("test-secret", path.read_text())
            self.assertEqual(json.loads(path.read_text())["claim"], "[REDACTED]")

    def test_parser_preserves_table_and_omits_script(self):
        html = '<script>secret text</script><table><tr><td>Revenue for fiscal year 2024</td><td>245122 million dollars in total</td></tr></table>'
        passages = parse_filing(html, "https://example.com")
        self.assertEqual(len(passages), 1)
        self.assertIn("245122", passages[0]["text"])
        self.assertNotIn("secret", passages[0]["text"])
        with TemporaryDirectory() as directory:
            path = Path(directory) / "passages.json"
            path.write_text(json.dumps(passages))
            store = PassageStore(path)
            self.assertEqual(len(store.search("revenue")), 1)
            self.assertEqual(store.search("revenue", year=2025), [])


if __name__ == "__main__":
    unittest.main()
