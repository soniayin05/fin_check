"""End-to-end ingest against a fake SEC. No network; live SEC access is not tested here."""

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fincheck.filings import filing_rows, pick_exhibit, select_annual_reports, select_earnings_release
from fincheck.ingest import REPORT_URL, prepare
from fincheck.ratios import growth
from fincheck.retrieval import PassageStore, parse_filing
from fincheck.sec import COMPANIES, TICKERS_URL, SecClient, SecError, archive_url, facts_url, submissions_url
from fincheck.tools import FactStore

from sec_fixtures import FY, accn, companyfacts, filing_html, submissions


class FakeSec:
    """Serves fixture bytes by URL and counts requests."""

    def __init__(self, drop_exhibit_for=None, wrong_cik=False):
        self.requests = 0
        self.pages = {TICKERS_URL: json.dumps({str(n): {
            "cik_str": info["cik"] + (1 if wrong_cik and ticker == "AAPL" else 0), "ticker": ticker, "title": info["name"]}
            for n, (ticker, info) in enumerate(COMPANIES.items())}).encode()}
        for ticker, info in COMPANIES.items():
            cik = info["cik"]
            self.pages[facts_url(cik)] = json.dumps(companyfacts()).encode()
            payload, extra = submissions(ticker, older_page=(ticker == "GOOGL"))
            self.pages[submissions_url(cik)] = json.dumps(payload).encode()
            if extra:
                self.pages[f"https://data.sec.gov/submissions/CIK-{ticker}-submissions-001.json"] = json.dumps(extra).encode()
            for year in (2022, 2023, 2024):
                self.pages[archive_url(cik, accn(year), f"{ticker.lower()}-{year}0630.htm")] = filing_html(ticker, year).encode()
                release = f"0000950170-{str(year)[2:]}-000002"
                names = [f"{ticker.lower()}-8k.htm", f"{ticker.lower()}-ex99_1.htm", "R1.htm", "logo.jpg"]
                if drop_exhibit_for == (ticker, year):
                    names.remove(f"{ticker.lower()}-ex99_1.htm")
                self.pages[archive_url(cik, release, "index.json")] = json.dumps(
                    {"directory": {"item": [{"name": name, "type": "text.gif", "size": "1"} for name in names]}}).encode()
                self.pages[archive_url(cik, release, f"{ticker.lower()}-ex99_1.htm")] = (
                    f"<html><body>{''.join(f'<p>{ticker} press release FY{year} line {n}: revenue increased and the company returned cash to shareholders this quarter.</p>' for n in range(22))}</body></html>").encode()

    def get(self, url):
        self.requests += 1
        if url not in self.pages:
            raise SecError(f"SEC HTTP 404 for {url}.")
        return self.pages[url]


def report_page(url):
    assert url == REPORT_URL
    return ("<html><body>" + "".join(
        f"<p>Microsoft annual report 2024 paragraph {n} about Azure and other cloud services revenue growth.</p>"
        for n in range(25)) + "</body></html>").encode()


class IngestTests(unittest.TestCase):
    def run_prepare(self, directory, client):
        return prepare(Path(directory), include_sec=True, client=client, report_getter=report_page)

    def test_full_pipeline_writes_facts_index_and_manifest(self):
        with TemporaryDirectory() as directory:
            root, client = Path(directory), FakeSec()
            result = self.run_prepare(root, client)
            self.assertEqual(result["sec"]["problems"], [])
            self.assertEqual(result["sec"]["documents"], 18)  # 3 companies x 3 years x (10-K + release)
            store = FactStore(root / "data/facts/observations.json")
            self.assertEqual(store.get_financial_fact("Alphabet", "revenue", 2024).value, 245122e6)
            self.assertAlmostEqual(growth(store, "apple", 2024, "operating_cash_flow")["value"], 35.35658, places=4)
            coverage = json.loads((root / "data/facts/coverage.json").read_text())
            self.assertEqual(sorted(coverage["calendar"]), ["AAPL", "GOOGL", "MSFT"])
            manifest = json.loads((root / "data/raw/manifest.json").read_text())
            for name in ("companyfacts-MSFT.json", "10k-GOOGL-FY2022.htm", "release-AAPL-FY2024.htm",
                         "sec-company-tickers.json", "msft-2024.html"):
                self.assertEqual(len(manifest[name]["sha256"]), 64, name)
                self.assertTrue(manifest[name]["url"].startswith("https://"), name)
            self.assertEqual(manifest["10k-AAPL-FY2023.htm"]["accession"], accn(2023))

            passages = PassageStore(root / "data/indexes/passages.json")
            hits = passages.search("risk competition regulation", "Apple", 2023)
            self.assertTrue(hits and all(h["company"] == "AAPL" and h["year"] == 2023 for h in hits))
            self.assertEqual(hits[0]["section"], "Item 1A Risk Factors")
            self.assertEqual(hits[0]["source_type"], "10-K")
            only_release = passages.search("revenue shareholders", "GOOGL", 2024, "earnings_release")
            self.assertTrue(only_release and all(h["source_type"] == "earnings_release" for h in only_release))
            self.assertFalse(any("HIDDENXBRLFACT" in p["text"] for p in passages.passages))
            self.assertTrue(passages.search("Azure", "MSFT", 2024, "annual_report_web"))

            before = client.requests
            again = self.run_prepare(root, client)  # everything is cached: no new requests
            self.assertEqual(client.requests, before)
            self.assertEqual(again["passages"], result["passages"])
            unchanged = json.loads((root / "data/raw/manifest.json").read_text())
            self.assertEqual(unchanged["companyfacts-MSFT.json"]["retrieved_at"],
                             manifest["companyfacts-MSFT.json"]["retrieved_at"])

    def test_missing_exhibit_is_reported_and_pipeline_continues(self):
        with TemporaryDirectory() as directory:
            result = self.run_prepare(directory, FakeSec(drop_exhibit_for=("MSFT", 2023)))
            self.assertEqual(result["sec"]["documents"], 17)
            self.assertTrue(any("MSFT FY2023" in p and "EX-99.1" in p for p in result["sec"]["problems"]))

    def test_cik_mismatch_stops_before_downloading_facts(self):
        with TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                self.run_prepare(directory, FakeSec(wrong_cik=True))
            self.assertTrue((Path(directory) / "data/raw/manifest.json").exists())  # partial work is recorded

    def test_default_prepare_does_not_touch_sec(self):
        with TemporaryDirectory() as directory:
            result = prepare(Path(directory), report_getter=report_page)
            self.assertFalse(result["sec_downloaded"])
            self.assertFalse((Path(directory) / "data/facts").exists())


class FilingSelectionTests(unittest.TestCase):
    def setUp(self):
        self.rows = filing_rows(submissions("MSFT")[0]["filings"]["recent"])

    def test_selects_three_most_recent_annual_reports(self):
        reports = select_annual_reports(self.rows, 3)
        self.assertEqual([r["reportDate"] for r in reports], [FY[2024][1], FY[2023][1], FY[2022][1]])
        self.assertEqual(len(select_annual_reports(self.rows, 2)), 2)

    def test_earnings_release_is_first_item_202_filing_after_year_end(self):
        release = select_earnings_release(self.rows, FY[2024][1])
        self.assertEqual((release["form"], release["items"]), ("8-K", "2.02,9.01"))
        self.assertTrue(release["filingDate"].startswith("2024-07"))
        self.assertIsNone(select_earnings_release(self.rows, "2019-06-30"))

    def test_exhibit_picker_never_guesses(self):
        index = lambda *names: {"directory": {"item": [{"name": n} for n in names]}}
        self.assertEqual(pick_exhibit(index("a8-k.htm", "a8-kex991q4202409282024.htm"), "a8-k.htm"),
                         "a8-kex991q4202409282024.htm")
        self.assertEqual(pick_exhibit(index("goog-8k.htm", "googexhibit991q42024.htm")), "googexhibit991q42024.htm")
        self.assertEqual(pick_exhibit(index("msft-ex99_1.htm", "msft-ex99_2.htm")), "msft-ex99_1.htm")
        self.assertIsNone(pick_exhibit(index("msft-8k.htm", "msft-ex9910.htm", "R1.htm")))
        self.assertIsNone(pick_exhibit({}))


class SecClientTests(unittest.TestCase):
    def test_requires_declared_contact_email(self):
        for agent in ("", "FinCheck academic project https://github.com/x/y", "name@example.edu"):
            with self.subTest(agent=agent), self.assertRaises(SecError):
                SecClient(agent)
        with patch.dict("os.environ", {"SEC_USER_AGENT": "FinCheck Test test@example.edu"}):
            self.assertEqual(SecClient().user_agent, "FinCheck Test test@example.edu")

    def test_sections_tracked_and_old_defaults_preserved(self):
        passages = parse_filing(filing_html("MSFT", 2024), "https://example.com", "MSFT", 2024, "msft-10k-fy2024", "10-K")
        self.assertEqual({p["section"] for p in passages}, {"Item 1A Risk Factors", "Item 7 MD&A"})
        legacy = parse_filing("<p>" + "word " * 20 + "</p>", "https://example.com")
        self.assertTrue(legacy[0]["id"].startswith("msft-ar2024-"))
        self.assertEqual((legacy[0]["company"], legacy[0]["year"], legacy[0]["section"]), ("MSFT", 2024, None))


if __name__ == "__main__":
    unittest.main()
