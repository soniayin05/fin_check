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
from .calculations import UNITS, calculate
from .facts import METRICS, coverage_table
from .ingest import prepare
from .model import Gemini, ModelError, load_env
from .ratios import RATIOS, UnknownRatio, compute_ratio, growth
from .embeddings import gemini_embedder
from .retrieval import METHODS, PassageStore, RetrievalError
from .retrieval_eval import (answer_sheet, available_methods, evaluate, format_table, index_sha256, label,
                             load_questions, score_sheet)
from .sec import SecError

ROOT = Path(__file__).resolve().parent.parent


def facts_path(root: Path, command: str, explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    sec = root / "data/facts/observations.json"
    if command in {"check", "ratio", "verify-number"} and sec.exists():
        return sec
    return root / "data/sample/financial_facts.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="FinCheck: audit a financial claim against filing evidence")
    parser.add_argument("command", choices=["demo", "eval-dev", "prepare-data", "search", "check", "eval-agent-dev",
                                            "facts-report", "ratio", "build-index", "label-retrieval",
                                            "eval-retrieval", "answer-sheet", "score-answers", "verify-number"])
    parser.add_argument("text", nargs="?", help="Claim for check, keywords for search, or a sheet for score-answers")
    parser.add_argument("--include-sec", action="store_true",
                        help="Also archive SEC Company Facts, 10-K filings and earnings releases")
    parser.add_argument("--company", default="MSFT", help="Ticker or company name (search, ratio, facts-report)")
    parser.add_argument("--year", type=int, default=2024, help="Fiscal year (search, ratio, facts-report)")
    parser.add_argument("--ratio", default="all", help="Ratio name, <metric>_growth, or 'all'")
    parser.add_argument("--source-type", help="Restrict search to 10-K, earnings_release or annual_report_web")
    parser.add_argument("--method", choices=METHODS, help="Retrieval method (default: FINCHECK_RETRIEVER or keyword)")
    parser.add_argument("--methods", help="Comma-separated methods for eval-retrieval / label-retrieval")
    parser.add_argument("-k", type=int, default=5, help="Passages returned or scored (search, eval-retrieval)")
    parser.add_argument("--questions", type=Path, default=ROOT / "evals/retrieval/questions.jsonl")
    parser.add_argument("--only", help="label-retrieval: (re)label just this question ID")
    parser.add_argument("--labeler", default="", help="label-retrieval: your name, stored with each label")
    parser.add_argument("--facts", type=Path, help="Facts JSON. check/ratio/verify-number use SEC data when present")
    parser.add_argument("--operation", choices=["fact", "growth", "ratio"], default="fact")
    parser.add_argument("--measure", help="Metric or ratio name for verify-number")
    parser.add_argument("--value", help="Numeric literal as written in the claim, preserving decimal places")
    parser.add_argument("--unit", choices=list(UNITS), help="Unit of the claimed value for verify-number")
    parser.add_argument("--cases", type=Path, default=ROOT / "evals/dev/claims.jsonl")
    args = parser.parse_args()
    args.facts = facts_path(ROOT, args.command, args.facts)
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
    index_path = ROOT / "data/indexes/passages.json"
    embedder = None
    if args.command in {"search", "build-index", "label-retrieval", "eval-retrieval", "answer-sheet",
                        "check", "eval-agent-dev"}:  # offline commands never depend on embedding settings
        try:
            embedder = gemini_embedder(ROOT)
        except ModelError as error:
            print(str(error))
            return 1
    passages = PassageStore(index_path, embedder=embedder)
    if args.command in {"search", "build-index", "label-retrieval", "eval-retrieval", "answer-sheet"} \
            and not passages.passages:
        parser.error("Run prepare-data before using the passage index")
    try:
        retrieval_exit = retrieval_command(args, parser, passages, index_path)
    except (RetrievalError, ModelError, ValueError) as error:
        print(f"{args.command} failed: {error}")
        return 1
    if retrieval_exit is not None:
        return retrieval_exit
    try:
        store = FactStore(args.facts)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(f"Cannot load facts from {args.facts}: {error}")
    if args.command == "verify-number":
        if args.measure is None or args.value is None or args.unit is None:
            parser.error("verify-number requires --measure, --value and --unit")
        try:
            result = calculate(store, args.company, args.year, args.operation, args.measure, args.value, args.unit)
        except ValueError as error:
            parser.error(str(error))
        print(json.dumps(dict(result, facts_path=str(args.facts)), indent=2, allow_nan=False))
        return 0 if result["status"] == "ok" else 1
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
            run["facts_path"] = str(args.facts)
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


def retrieval_command(args, parser, passages: PassageStore, index_path: Path) -> int | None:
    """Phase 2 commands. Returns None when the command is not a retrieval command."""
    if args.command == "search":
        if not args.text:
            parser.error("search requires quoted keywords")
        print(json.dumps(passages.search(args.text, args.company, args.year, args.source_type,
                                         k=args.k, method=args.method), indent=2))
        return 0
    if args.command == "build-index":
        if passages.embedder is None:
            parser.error("build-index embeds every passage and needs GEMINI_API_KEY")
        start = perf_counter()
        passages.embedder.embed_documents(passages.passages)
        summary = dict(passages.embedder.usage(), passages=len(passages.passages),
                       seconds=round(perf_counter() - start, 2), index_sha256=index_sha256(index_path))
        print(json.dumps(summary, indent=2))
        return 0
    methods = available_methods(passages, args.methods.split(",") if args.methods else None)
    if args.command == "label-retrieval":
        questions = load_questions(args.questions)
        count = label(passages, questions, args.questions, index_path, methods, only=args.only,
                      labeler=args.labeler)
        remaining = sum(1 for q in questions if not q["relevant_ids"] and q.get("status") != "no_relevant_passage")
        print(f"Labeled {count} question(s); {remaining} still unlabeled. Saved to {args.questions}")
        return 0
    if args.command == "eval-retrieval":
        questions = load_questions(args.questions)
        result = evaluate(passages, questions, methods, args.k)
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        target = ROOT / "artifacts/local" / run_id
        target.mkdir(parents=True)
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True)
        result.update(run_id=run_id, split="retrieval-dev", git_commit=commit.stdout.strip() or None,
                      index_sha256=index_sha256(index_path),
                      questions_sha256=hashlib.sha256(args.questions.read_bytes()).hexdigest(),
                      embedding_usage=passages.embedder.usage() if passages.embedder else None,
                      latency_note="Per-query wall time inside search(). For embedding and hybrid it includes "
                                   "the Gemini query-embedding call unless the query vector was cached; see "
                                   "embedding_usage.cache_hits.")
        (target / "retrieval.json").write_text(json.dumps(result, indent=2) + "\n")
        print(format_table(result))
        print(f"Saved: {target / 'retrieval.json'}")
        return 0
    if args.command == "answer-sheet":
        method = args.method or passages.default_method
        target = ROOT / "artifacts/local" / f"answers-{method}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.csv"
        target.parent.mkdir(parents=True, exist_ok=True)
        print(json.dumps(answer_sheet(passages, load_questions(args.questions), Gemini(), method, target, args.k),
                         indent=2))
        print("Grade each row by hand: 1 = matches expected_answer, 0 = wrong, missing or unsupported. "
              "Then run: python -m fincheck score-answers <sheet>")
        return 0
    if args.command == "score-answers":
        if not args.text:
            parser.error("score-answers requires the path of a graded sheet")
        print(json.dumps(score_sheet(Path(args.text)), indent=2))
        return 0
    return None


if __name__ == "__main__":
    raise SystemExit(main())
