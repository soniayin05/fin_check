"""CLI for the offline tools and live single-agent check-in prototype."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
from time import perf_counter

from .tools import FactStore, compare_growth, normalize_company
from .agent import Agent, save_run
from .facts import METRICS, coverage_table
from .ingest import prepare
from .model import Gemini, ModelError, load_env
from .ratios import RATIOS, UnknownRatio, compute_ratio, growth
from .retrieval import PassageStore
from .sec import SecError

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description="FinCheck: audit a financial claim against filing evidence")
    parser.add_argument("command", choices=["demo", "eval-dev", "prepare-data", "search", "check", "eval-agent-dev",
                                            "facts-report", "ratio"])
    parser.add_argument("text", nargs="?", help="Claim for check, or keywords for search")
    parser.add_argument("--include-sec", action="store_true",
                        help="Also archive SEC Company Facts, 10-K filings and earnings releases")
    parser.add_argument("--company", default="MSFT", help="Ticker or company name (search, ratio, facts-report)")
    parser.add_argument("--year", type=int, default=2024, help="Fiscal year (search, ratio, facts-report)")
    parser.add_argument("--ratio", default="all", help="Ratio name, <metric>_growth, or 'all'")
    parser.add_argument("--source-type", help="Restrict search to 10-K, earnings_release or annual_report_web")
    parser.add_argument("--facts", type=Path, default=ROOT / "data/sample/financial_facts.json")
    parser.add_argument("--cases", type=Path, default=ROOT / "evals/dev/claims.jsonl")
    args = parser.parse_args()
    load_env(ROOT)
    if args.command == "prepare-data":
        try:
            result = prepare(ROOT, args.include_sec)
        except (SecError, ValueError) as error:
            print(f"Data preparation failed: {error}")
            return 1
        except OSError as error:
            print(f"Data preparation failed: {type(error).__name__}. Check source access and cached files.")
            return 1
        table = result.get("sec", {}).pop("coverage_table", None)
        print(json.dumps(result, indent=2))
        if table:
            print(table)
        return 0
    if args.command == "facts-report":
        coverage_path = ROOT / "data/facts/coverage.json"
        if not coverage_path.exists():
            parser.error("Run prepare-data --include-sec first")
        coverage = json.loads(coverage_path.read_text())["coverage"]
        print(coverage_table(coverage))
        company = normalize_company(args.company)
        print(f"\nValues to verify by hand for {company} FY{args.year} (change with --company / --year):")
        for row in coverage:
            if (row["company"], row["fiscal_year"]) == (company, args.year):
                if row["status"] == "ok":
                    flag = "  RESTATED" if row.get("restated") else ""
                    print(f"  {row['metric']:<28}{row['value']:>20,} {row['unit']:<11}{row['tag']}  [{row['accession']}]{flag}")
                else:
                    print(f"  {row['metric']:<28}{row['status'].upper():>20} {row.get('detail', '')}")
        return 0
    passages = PassageStore(ROOT / "data/indexes/passages.json")
    if args.command == "search":
        if not args.text:
            parser.error("search requires quoted keywords")
        if not passages.passages:
            parser.error("Run prepare-data before searching")
        print(json.dumps(passages.search(args.text, args.company, args.year, args.source_type), indent=2))
        return 0
    store = FactStore(args.facts)
    if args.command == "ratio":
        try:
            if args.ratio != "all":
                metric = args.ratio[:-len("_growth")] if args.ratio.endswith("_growth") else None
                result = (growth(store, args.company, args.year, metric) if metric in METRICS
                          else compute_ratio(store, args.company, args.year, args.ratio))
                print(json.dumps(result, indent=2))
                return 0 if result["status"] == "ok" else 1
            for name, (number, label, unit, _, _) in RATIOS.items():
                result = compute_ratio(store, args.company, args.year, name)
                shown = f"{result['value']:,.4f} {unit}" if result["status"] == "ok" else f"n/a ({result['reason']})"
                print(f"{str(number or '-'):>3} {label:<46} {shown}")
            return 0
        except UnknownRatio as error:
            parser.error(str(error))
    if args.command in {"check", "eval-agent-dev"}:
        try:
            agent = Agent(store, passages, Gemini())
        except ModelError as error:
            print(str(error))
            return 1
        if args.command == "check":
            if not args.text:
                parser.error("check requires a quoted claim")
            try:
                run = agent.run(args.text)
            except ValueError as error:
                parser.error(str(error))
            path = save_run(ROOT, run, args.facts)
            print(json.dumps({"status": run["status"], "result": run["result"]}, indent=2))
            print(f"Trace: {path}")
            return 0 if run["status"] == "complete" else 1
        cases = [json.loads(line) for line in args.cases.read_text().splitlines() if line.strip()]
        if not cases:
            parser.error("No development cases supplied")
        passed = 0
        rows = []
        for case in cases:
            # Only natural-language claim goes to the agent, never input hints or gold labels.
            run = agent.run(case["claim"])
            expected, result = case["expected"], run["result"]
            ok = run["status"] == "complete" and result.get("verdict") == expected["verdict"]
            for key in ("left_growth_pct", "right_growth_pct"):
                if key in expected:
                    ok = ok and type(result.get(key)) in (int, float) and abs(result[key] - expected[key]) <= .01
            ok = ok and set(expected.get("required_source_ids", [])).issubset(result.get("sources", {}))
            path = save_run(ROOT, run, args.facts)
            row = {"case_id": case["id"], "passed": bool(ok), "expected": expected, "trace": str(path)}
            rows.append(row)
            passed += int(bool(ok))
            print(f"{'PASS' if ok else 'FAIL'} {case['id']} ({run['status']})")
        summary = {"split": "dev", "passed": passed, "total": len(cases), "cases": rows,
                   "cases_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest()}
        summary_path = path.parent / "evaluation.json"
        summary_path.write_text(json.dumps(summary, indent=2) + "\n")
        print(f"Agent development results: {passed}/{len(cases)}. {summary_path}")
        return 0 if passed == len(cases) else 1
    if args.command == "demo":
        result = compare_growth(store, "MSFT", 2024, "revenue", "operating_cash_flow")
        print("Tools-only demo: Did MSFT revenue grow faster than operating cash flow in FY2024?")
        print(json.dumps(result, indent=2))
        return 0

    # The checker receives ONLY case input. Expected answers stay in the scorer.
    cases = [json.loads(line) for line in args.cases.read_text().splitlines() if line.strip()]
    if not cases:
        raise ValueError("No development cases supplied")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output_dir = ROOT / "artifacts/local" / run_id
    output_dir.mkdir(parents=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                         capture_output=True, text=True)
    meta = {
        "run_id": run_id, "system": "deterministic-tools-starter", "split": "dev",
        "model": None, "temperature": None, "token_usage": None,
        "token_usage_note": "No model calls; not a course agent evaluation",
        "git_commit": sha.stdout.strip() if sha.returncode == 0 else "uncommitted",
        "working_tree_dirty": bool(subprocess.run(
            ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True).stdout),
        "facts_sha256": hashlib.sha256(args.facts.read_bytes()).hexdigest(),
        "cases_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
    }
    passed = 0
    with (output_dir / "results.jsonl").open("w") as file:
        for case in cases:
            start = perf_counter()
            result = compare_growth(store, **case["input"])
            elapsed = perf_counter() - start
            expected = case["expected"]
            ok = result["verdict"] == expected["verdict"]
            for key in ("left_growth_pct", "right_growth_pct"):
                if key in expected:
                    ok = ok and abs(result.get(key, float("inf")) - expected[key]) <= 0.01
            if "required_source_ids" in expected:
                ok = ok and set(expected["required_source_ids"]).issubset(result["sources"])
            passed += int(ok)
            file.write(json.dumps({"case_id": case["id"], "input": case["input"],
                                   "output": result, "expected": expected, "passed": ok,
                                   "latency_seconds": elapsed}) + "\n")
            print(f"{'PASS' if ok else 'FAIL'} {case['id']}")
    (output_dir / "metadata.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(f"Development tool checks: {passed}/{len(cases)}. Results: {output_dir}")
    print("These are development checks, not held-out results or agent feature credit.")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
