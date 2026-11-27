"""Archive public filings and build reproducible local facts and a passage index.

Raw downloads are cached under data/raw/ and never re-fetched while the file
exists, so the manifest (URL, time, SHA-256) pins exactly what was evaluated.
Delete a cached file to refresh it. Nothing here substitutes mocked values:
a source that cannot be fetched is reported and left out.
"""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import urllib.request

from .facts import coverage_table, merge, normalize
from .filings import filing_rows, pick_exhibit, select_annual_reports, select_earnings_release
from .retrieval import chunk_passages, parse_filing
from .sec import COMPANIES, TICKERS_URL, SecClient, SecError, archive_url, facts_url, submissions_url, company_universe

REPORT_URL = "https://www.microsoft.com/investor/reports/ar24/index.html"
ANNUAL_REPORTS = 3   # 10-K filings archived per company
FACT_YEARS = 5       # fiscal years of numbers kept, so growth and averages exist for the last three


class Archive:
    """Download-once cache with a manifest entry per file."""

    def __init__(self, raw: Path):
        self.raw = raw
        self.path = raw / "manifest.json"
        self.previous = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.manifest: dict[str, dict] = {}

    def fetch(self, name: str, url: str, getter, validate=None, **meta) -> Path:
        target = self.raw / name
        cached = target.exists()
        if not cached:
            payload = getter(url)
            if validate:
                validate(payload)
            target.write_bytes(payload)
        old = self.previous.get(name, {})
        self.manifest[name] = dict(meta, url=url,
            sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
            retrieved_at=old.get("retrieved_at") if cached else datetime.now(timezone.utc).isoformat(),
            cache_used=cached,
            note="Unknown retrieval time for pre-existing files" if cached and not old else "")
        return target

    def save(self) -> None:
        # Keep entries from earlier runs (for example a previous --include-sec) that still exist.
        merged = {name: entry for name, entry in self.previous.items() if (self.raw / name).exists()}
        merged.update(self.manifest)
        self.path.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n")


def _plain_get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": os.getenv(
        "SEC_USER_AGENT") or "FinCheck academic project https://github.com/soniayin05/fin_check"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def _check_report(payload: bytes) -> None:
    if b"microsoft" not in payload.lower() or b"2024" not in payload:
        raise ValueError("Downloaded page does not look like the Microsoft 2024 report")


def _check_json(payload: bytes) -> None:
    json.loads(payload)


def _all_filings(archive: Archive, client, ticker: str, cik: int, need: int) -> list[dict]:
    """Recent filings, plus older pages only if the recent page lacks enough 10-Ks."""
    path = archive.fetch(f"submissions-{ticker}.json", submissions_url(cik), client.get, _check_json,
                         kind="submissions", company=ticker)
    submissions = json.loads(path.read_text())
    rows = filing_rows(submissions["filings"]["recent"])
    for extra in submissions["filings"].get("files", []):
        if len(select_annual_reports(rows, need)) >= need:
            break
        name = extra["name"]
        page = archive.fetch(f"submissions-{ticker}-{name}", f"https://data.sec.gov/submissions/{name}",
                             client.get, _check_json, kind="submissions", company=ticker)
        rows += filing_rows(json.loads(page.read_text()))
    return rows


def prepare_sec(root: Path, archive: Archive, client=None, tickers=None) -> tuple[list[dict], dict]:
    """Download Company Facts, 10-Ks and earnings releases for every supported company."""
    client = client or SecClient()
    tickers_path = archive.fetch("sec-company-tickers.json", TICKERS_URL, client.get, _check_json,
                                 kind="ticker_map")
    listed = {row["ticker"]: int(row["cik_str"]) for row in json.loads(tickers_path.read_text()).values()}
    universe=company_universe(json.loads(tickers_path.read_text()),tickers)
    passages, results, problems, documents = [], [], [], []
    for ticker, info in universe.items():
        cik = info["cik"]
        if listed.get(ticker) != cik:
            raise ValueError(f"{ticker}: configured CIK {cik} does not match SEC ticker file ({listed.get(ticker)})")
        facts_path = archive.fetch(f"companyfacts-{ticker}.json", facts_url(cik), client.get, _check_json,
                                   kind="company_facts", company=ticker)
        raw = facts_path.read_bytes()
        results.append(normalize(json.loads(raw), ticker, cik, FACT_YEARS, hashlib.sha256(raw).hexdigest()))
        rows = _all_filings(archive, client, ticker, cik, ANNUAL_REPORTS)
        reports = select_annual_reports(rows, ANNUAL_REPORTS)
        if len(reports) < ANNUAL_REPORTS:
            problems.append(f"{ticker}: found only {len(reports)} 10-K filings")
        for report in reports:
            year, accession = int(report["reportDate"][:4]), report["accessionNumber"]
            url = archive_url(cik, accession, report["primaryDocument"])
            meta = {"company": ticker, "fiscal_year": year, "accession": accession,
                    "form": "10-K", "filed": report["filingDate"], "period_end": report["reportDate"]}
            path = archive.fetch(f"10k-{ticker}-FY{year}.htm", url, client.get, kind="annual_report", **meta)
            documents.append((path, url, ticker, year, f"{ticker.lower()}-10k-fy{year}", "10-K",
                              {"accession": accession, "filed": report["filingDate"]}))
            release = select_earnings_release(rows, report["reportDate"])
            if not release:
                problems.append(f"{ticker} FY{year}: no Item 2.02 8-K found after {report['reportDate']}")
                continue
            try:
                acc = release["accessionNumber"]
                index_path = archive.fetch(f"8k-index-{ticker}-FY{year}.json", archive_url(cik, acc, "index.json"),
                                           client.get, _check_json, kind="filing_index", company=ticker,
                                           fiscal_year=year, accession=acc)
                exhibit = pick_exhibit(json.loads(index_path.read_text()), release.get("primaryDocument", ""))
                if not exhibit:
                    problems.append(f"{ticker} FY{year}: no EX-99.1 exhibit recognised in 8-K {acc}")
                    continue
                url = archive_url(cik, acc, exhibit)
                path = archive.fetch(f"release-{ticker}-FY{year}.htm", url, client.get, kind="earnings_release",
                                     company=ticker, fiscal_year=year, accession=acc, form="8-K",
                                     filed=release["filingDate"])
                documents.append((path, url, ticker, year, f"{ticker.lower()}-release-fy{year}",
                                  "earnings_release", {"accession": acc, "filed": release["filingDate"]}))
            except SecError as error:
                problems.append(f"{ticker} FY{year}: earnings release not archived ({error})")
    for path, url, ticker, year, prefix, source_type, meta in documents:
        found = parse_filing(path.read_text(errors="replace"), url, ticker, year, prefix, source_type, meta)
        if len(found) < 20:
            problems.append(f"{path.name}: only {len(found)} text blocks parsed; inspect the file")
        passages += chunk_passages(found)
    facts_dir = root / "data/facts"
    facts_dir.mkdir(parents=True, exist_ok=True)
    (facts_dir / "observations.json").write_text(json.dumps(merge(results), indent=1) + "\n")
    coverage = [row for result in results for row in result["coverage"]]
    (facts_dir / "coverage.json").write_text(json.dumps(
        {"calendar": {t: r["calendar"] for t, r in zip(universe, results)}, "coverage": coverage},
        indent=1) + "\n")
    summary = {"observations": sum(len(r["observations"]) for r in results),
               "missing": sum(row["status"] == "missing" for row in coverage),
               "conflicts": sum(row["status"] == "conflict" for row in coverage),
               "documents": len(documents), "problems": problems, "sec_requests": getattr(client, "requests", None),
               "facts": str(facts_dir / "observations.json"), "coverage_table": coverage_table(coverage),
               "company_universe":universe}
    return passages, summary


def prepare(root: Path, include_sec: bool = False, client=None, report_getter=_plain_get,tickers=None) -> dict:
    raw = root / "data/raw"
    index = root / "data/indexes"
    raw.mkdir(parents=True, exist_ok=True)
    index.mkdir(parents=True, exist_ok=True)
    archive = Archive(raw)
    report = archive.fetch("msft-2024.html", REPORT_URL, report_getter, _check_report,
                           kind="annual_report_web", company="MSFT", fiscal_year=2024)
    blocks = parse_filing(report.read_text(), REPORT_URL)
    if len(blocks) < 20:
        raise ValueError("Too few filing passages; inspect downloaded HTML")
    passages = chunk_passages(blocks)
    result = {"sec_downloaded": False,
              "note": "Numeric tool uses the reviewed sample extract. Run with --include-sec for SEC data."}
    if include_sec:
        try:
            sec_passages, summary = prepare_sec(root, archive, client,tickers)
        finally:
            archive.save()  # keep what was fetched even if a later step failed
        passages += sec_passages
        result = {"sec_downloaded": True, "sec": summary,
                  "note": "Review data/facts/coverage.json, then use --facts data/facts/observations.json."}
    ids = [p["id"] for p in passages]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate passage IDs across documents")
    (index / "passages.json").write_text(json.dumps(passages, indent=2) + "\n")
    archive.save()
    return dict(result, passages=len(passages), manifest=str(archive.path))
