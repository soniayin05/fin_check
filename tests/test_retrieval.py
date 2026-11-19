"""Retrieval (F1) tests. Embeddings use a deterministic fake transport, never the network."""

import csv
import hashlib
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest

from fincheck.embeddings import BATCH_SIZE, Embedder, EmbeddingCache
from fincheck.model import ModelError
from fincheck.retrieval import METHODS, PassageStore, RetrievalError, chunk_passages, parse_filing
from fincheck.retrieval_eval import (answer_sheet, available_methods, evaluate, format_table, label,
                                     load_questions, score_sheet)
from fincheck.stats import p95, paired_bootstrap, wilson

ROOT = Path(__file__).resolve().parents[1]
DIM = 256


def fake_vector(text: str) -> list[float]:
    """Hashed bag of words with a few synonyms, so 'paraphrase' search can work offline."""
    synonyms = {"staff": "employees", "headcount": "employees", "people": "employees", "workers": "employees"}
    vector = [0.0] * DIM
    for word in re.findall(r"[a-z]+", text.lower()):
        word = synonyms.get(word, word)
        vector[int(hashlib.md5(word.encode()).hexdigest(), 16) % DIM] += 1.0
    return vector or [1.0] + [0.0] * (DIM - 1)


class FakeTransport:
    def __init__(self, dim=DIM, report_usage=True):
        self.calls, self.dim, self.report_usage = [], dim, report_usage

    def __call__(self, texts, titles, task):
        self.calls.append((len(texts), task, list(titles)))
        vectors = [fake_vector(text)[:self.dim] + [0.0] * max(0, self.dim - DIM) for text in texts]
        return vectors, (sum(len(t.split()) for t in texts) if self.report_usage else None)


def passage(pid, text, company="MSFT", year=2024, source_type="10-K", section="Item 7 MD&A"):
    return {"id": pid, "company": company, "year": year, "source_type": source_type, "section": section,
            "text": text}


CORPUS = [
    passage("p-azure", "Azure and other cloud services revenue grew 30% driven by consumption."),
    passage("p-gaming", "Xbox content and services revenue increased driven by the Activision Blizzard acquisition."),
    passage("p-staff", "As of June 30 we employed approximately 228,000 employees full time."),
    passage("p-long", "Revenue " * 3 + "cloud " + "filler text about many unrelated operating topics " * 40),
    passage("p-risk", "Issues in the use of AI in our offerings may result in reputational harm.", section="Item 1A Risk Factors"),
    passage("p-release", "Operating income was $27.9 billion for the fourth quarter.", source_type="earnings_release"),
    passage("p-2023", "Azure and other cloud services revenue grew 29%.", year=2023),
    passage("p-apple", "Azure is not an Apple product; iPhone net sales were stable.", company="AAPL"),
]


def store(embed=True, directory=None):
    embedder = None
    if embed:
        cache = EmbeddingCache(Path(directory), "fake-embed", DIM)
        embedder = Embedder(FakeTransport(), cache, "fake-embed", DIM)
    s = PassageStore(None, embedder=embedder)
    s.passages = [dict(p) for p in CORPUS]
    return s


class ChunkTests(unittest.TestCase):
    def blocks(self):
        html = ("<div>Item 7. MD&A</div>" + "".join(f"<p>Para {i} " + "word " * 60 + ".</p>" for i in range(12))
                + "<table><tr><td>Total revenue for the year</td><td>245,122</td></tr><tr><td>Net income in millions of dollars</td><td>88,136</td></tr></table>"
                + "<div>Item 8. Financial Statements</div><p>Tail " + "x " * 60 + "</p>"
                + "<p>" + " ".join(f"Long sentence number {n} has six words." for n in range(110)) + "</p>")
        return parse_filing(html, "https://example.com", "MSFT", 2024, "msft-10k-fy2024", "10-K", {"accession": "a-1"})

    def test_chunks_respect_sections_tables_and_size(self):
        blocks = self.blocks()
        chunks = chunk_passages(blocks)
        self.assertEqual([c["kind"] for c in chunks].count("table"), 1)
        table = next(c for c in chunks if c["kind"] == "table")
        self.assertIn("245,122", table["text"])
        self.assertIn("88,136", table["text"])
        for chunk in chunks:
            self.assertLessEqual(len(chunk["text"].split()), 400)
            self.assertTrue(chunk["id"].startswith("msft-10k-fy2024-"))
            self.assertEqual((chunk["company"], chunk["year"], chunk["accession"]), ("MSFT", 2024, "a-1"))
        sections = [c["section"] for c in chunks if c["kind"] == "text"]
        self.assertEqual(sections[:3], ["Item 7 MD&A"] * 3)
        self.assertIn("Item 8 Financial Statements", sections)
        self.assertTrue(any(len(c["text"].split()) >= 300 for c in chunks))
        long_parts = [c for c in chunks if c["blocks"][0] == c["blocks"][1] and c["text"].startswith("Long")]
        self.assertGreater(len(long_parts), 1)  # a 720-word block is split at sentence boundaries
        self.assertEqual([c["id"] for c in chunk_passages(self.blocks())], [c["id"] for c in chunks])

    def test_nothing_is_lost_and_duplicates_collapse(self):
        blocks = self.blocks()
        words = lambda items: sorted(" ".join(i["text"] for i in items).split())
        self.assertEqual(words(chunk_passages(blocks)), words(blocks))
        copy = [dict(b, block=b["block"] + 100) for b in blocks if b["kind"] == "table"]
        self.assertEqual(len(chunk_passages(blocks + copy)), len(chunk_passages(blocks)))


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.store = store(directory=self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def ids(self, query, method, **filters):
        return [hit["id"] for hit in self.store.search(query, method=method, **filters)]

    def test_every_method_shares_the_interface_and_filters(self):
        for method in METHODS:
            hits = self.store.search("Azure cloud services revenue", "Microsoft", 2024, k=3, method=method)
            self.assertTrue(hits, method)
            self.assertLessEqual(len(hits), 3)
            self.assertEqual([h["rank"] for h in hits], list(range(1, len(hits) + 1)))
            self.assertTrue(all(h["company"] == "MSFT" and h["year"] == 2024 and h["method"] == method for h in hits))
            self.assertEqual(hits[0]["id"], "p-azure", method)
            only = self.store.search("operating income quarter", "MSFT", 2024, "earnings_release", method=method)
            self.assertEqual([h["id"] for h in only], ["p-release"], method)

    def test_bm25_normalizes_length_and_tfidf_prefers_rare_terms(self):
        self.assertEqual(self.ids("cloud revenue", "bm25")[0], "p-azure")
        self.assertEqual(self.ids("cloud revenue", "tfidf")[0], "p-azure")
        self.assertEqual(self.ids("Activision acquisition", "tfidf")[0], "p-gaming")

    def test_embedding_finds_paraphrase_that_lexical_methods_miss(self):
        self.assertNotIn("p-staff", self.ids("headcount", "bm25"))
        self.assertEqual(self.ids("headcount", "embedding")[0], "p-staff")
        self.assertEqual(self.ids("headcount", "hybrid")[0], "p-staff")

    def test_errors_are_explicit(self):
        with self.assertRaises(RetrievalError):
            self.store.search("x", method="semantic")
        plain = store(embed=False)
        with self.assertRaises(RetrievalError):
            plain.search("Azure", method="embedding")
        self.assertEqual(available_methods(plain), ["keyword", "tfidf", "bm25"])
        self.assertEqual(self.store.search("Azure", "MSFT", 2030), [])

    def test_replacing_passages_rebuilds_statistics(self):
        self.store.passages = [passage("new", "Completely new text about dividends.")]
        self.assertEqual(self.ids("dividends", "bm25"), ["new"])
        self.assertEqual(self.store.by_id.keys(), {"new"})

    def test_default_method_is_configurable(self):
        self.assertEqual(self.store.default_method, "keyword")
        configured = PassageStore(None, default_method="bm25")
        configured.passages = [dict(p) for p in CORPUS]
        self.assertEqual(configured.search("Azure")[0]["method"], "bm25")


class EmbeddingCacheTests(unittest.TestCase):
    def test_cache_persists_batches_and_counts_usage(self):
        with TemporaryDirectory() as directory:
            transport = FakeTransport()
            embedder = Embedder(transport, EmbeddingCache(Path(directory), "fake-embed", DIM), "fake-embed", DIM)
            many = [passage(f"p{i}", f"text number {i}") for i in range(BATCH_SIZE + 5)]
            vectors = embedder.embed_documents(many)
            self.assertEqual([c[0] for c in transport.calls], [BATCH_SIZE, 5])
            self.assertTrue(all(abs(sum(v * v for v in vec) - 1) < 1e-5 for vec in vectors.values()))
            self.assertEqual(transport.calls[0][2][0], "MSFT 10-K FY2024 Item 7 MD&A")
            embedder.embed_query("text number 3")
            self.assertEqual(transport.calls[-1][1], "RETRIEVAL_QUERY")
            usage = embedder.usage()
            self.assertEqual((usage["api_calls"], usage["texts_embedded"], usage["tokens_estimated"]), (3, 106, False))

            again = Embedder(FakeTransport(), EmbeddingCache(Path(directory), "fake-embed", DIM), "fake-embed", DIM)
            self.assertEqual(again.embed_documents(many), vectors)
            self.assertEqual((again.api_calls, again.cache_hits), (0, len(many)))

    def test_bad_responses_and_settings_fail_loudly(self):
        with TemporaryDirectory() as directory:
            wrong = Embedder(FakeTransport(dim=8), EmbeddingCache(Path(directory), "m", DIM), "m", DIM)
            with self.assertRaises(ModelError):
                wrong.embed_query("anything")
            estimated = Embedder(FakeTransport(report_usage=False), EmbeddingCache(Path(directory), "m", DIM), "m", DIM)
            estimated.embed_query("twelve characters")
            self.assertTrue(estimated.usage()["tokens_estimated"])
            (Path(directory) / f"m-{DIM}.json").write_text(json.dumps({"model": "other", "dim": DIM, "keys": []}))
            with self.assertRaises(ModelError):  # a cache built with other settings is never reused
                EmbeddingCache(Path(directory), "m", DIM)


def questions_file(directory: str, rows: list[dict]) -> Path:
    path = Path(directory) / "questions.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def question(qid, text, relevant=(), **extra):
    return dict({"id": qid, "question": text, "company": "MSFT", "year": 2024, "qtype": "lexical",
                 "relevant_ids": list(relevant)}, **extra)


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.store = store(directory=self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_scores_methods_and_reports_unscored_questions(self):
        rows = [question("q1", "Azure cloud revenue growth", ["p-azure"]),
                question("q2", "headcount", ["p-staff"], qtype="paraphrase"),
                question("q3", "gaming acquisition", ["p-gaming"]),
                question("q4", "not labeled yet"),
                question("q5", "labeled but no passage", status="no_relevant_passage"),
                question("q6", "stale label", ["p-removed"])]
        result = evaluate(self.store, rows, ["bm25", "embedding"], k=1)
        self.assertEqual(result["methods"]["bm25"]["hits"], 2)
        self.assertEqual(result["methods"]["embedding"]["hits"], 3)
        self.assertEqual(result["methods"]["bm25"]["by_qtype"]["paraphrase"], {"hits": 0, "questions": 1})
        self.assertEqual(result["unlabeled_questions"], ["q4"])
        self.assertEqual(result["no_relevant_passage"], ["q5"])
        self.assertTrue(result["label_problems"] and "q6" in result["label_problems"][0])
        miss = next(c for c in result["cases"] if c["id"] == "q2")["methods"]["bm25"]
        self.assertEqual(miss["miss_position"], "not_in_top_20")
        comparison = result["paired_comparisons"]["bm25_vs_embedding"]
        self.assertEqual((comparison["only_b"], comparison["both"]), (1, 2))
        table = format_table(result)
        self.assertIn("embedding", table)
        self.assertIn("LABEL PROBLEM", table)

    def test_no_usable_labels_is_an_error(self):
        with self.assertRaises(ValueError):
            evaluate(self.store, [question("q1", "x")], ["bm25"])

    def test_draft_question_file_is_valid(self):
        rows = load_questions(ROOT / "evals/retrieval/questions.jsonl")
        self.assertGreaterEqual(len(rows), 20)
        self.assertEqual(len({r["id"] for r in rows}), len(rows))
        for row in rows:
            self.assertIn(row["company"], {"MSFT", "AAPL", "GOOGL"})
            self.assertIn(row.get("source_type"), {None, "10-K", "earnings_release", "annual_report_web"})
            self.assertIn(row["qtype"], {"lexical", "paraphrase", "numeric", "table"})


class LabelingTests(unittest.TestCase):
    def test_interactive_labels_are_saved_with_provenance(self):
        with TemporaryDirectory() as directory:
            s = store(directory=directory)
            rows = [question("q1", "Azure cloud revenue"), question("q2", "employees"),
                    question("q3", "nothing here"), question("q5", "wrong year", year=2031),
                    question("q4", "later")]
            path = questions_file(directory, rows)
            index = Path(directory) / "passages.json"
            index.write_text("[]")
            answers = iter(["1", "Azure grew 30%",          # q1: first candidate, then expected answer
                            "g reputational", "x", "99", "1",  # q2: grep, two bad inputs, then a choice
                            "",                             # q2: skip expected answer
                            "n",                            # q3: none relevant
                            "q"])                           # q4: quit
            shown = []
            done = label(s, rows, path, index, ["bm25"], ask=lambda _: next(answers),
                         show=shown.append, labeler="Sonia")
            self.assertEqual(done, 3)
            saved = {r["id"]: r for r in load_questions(path)}
            self.assertEqual(saved["q1"]["relevant_ids"], ["p-azure"])
            self.assertEqual(saved["q1"]["expected_answer"], "Azure grew 30%")
            self.assertEqual((saved["q1"]["labeled_by"], saved["q1"]["pooled_methods"]), ("Sonia", ["bm25"]))
            self.assertEqual(saved["q1"]["index_sha256"], hashlib.sha256(b"[]").hexdigest())
            self.assertTrue(saved["q1"]["relevant_snippets"]["p-azure"].startswith("Azure and other"))
            self.assertEqual(saved["q2"]["relevant_ids"], ["p-staff"])
            self.assertEqual((saved["q3"]["relevant_ids"], saved["q3"]["status"]), ([], "no_relevant_passage"))
            self.assertEqual(saved["q4"]["relevant_ids"], [])
            self.assertTrue(any("no passages for MSFT FY2031" in line for line in shown))
            self.assertTrue(any("found by grep" in line for line in shown))


class FakeModel:
    model = "fake-model"

    def answer(self, prompt, instruction):
        grounded = "Passages:" in prompt
        return {"text": "Azure grew 30% [1]" if grounded else "I don't know", "usage": {"totalTokenCount": 10}}


class AnswerExperimentTests(unittest.TestCase):
    def test_sheet_requires_manual_grades_then_scores(self):
        with TemporaryDirectory() as directory:
            s = store(directory=directory)
            rows = [question("q1", "Azure cloud revenue", ["p-azure"], expected_answer="Grew 30%"),
                    question("q2", "gaming", ["p-gaming"], expected_answer="Activision drove it"),
                    question("q3", "no expected answer", ["p-staff"])]
            sheet = Path(directory) / "sheet.csv"
            summary = answer_sheet(s, rows, FakeModel(), "bm25", sheet)
            self.assertEqual((summary["questions"], summary["model_calls"], summary["total_tokens"]), (2, 4, 40))
            with sheet.open() as handle:
                graded = list(csv.DictReader(handle))
            self.assertIn("p-azure", graded[0]["retrieved_ids"])
            with self.assertRaises(ValueError):
                score_sheet(sheet)
            graded[0].update(grade_no_retrieval="0", grade_with_retrieval="1")
            graded[1].update(grade_no_retrieval="0", grade_with_retrieval="0")
            with sheet.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=graded[0].keys())
                writer.writeheader()
                writer.writerows(graded)
            score = score_sheet(sheet)
            self.assertEqual((score["with_retrieval"]["correct"], score["no_retrieval"]["correct"]), (1, 0))
            self.assertEqual(score["with_minus_without"]["only_a"], 1)


class StatsTests(unittest.TestCase):
    def test_known_values(self):
        low, high = wilson(0, 10)
        self.assertEqual(low, 0.0)
        self.assertAlmostEqual(high, 0.2775, places=3)
        low, high = wilson(20, 25)
        self.assertAlmostEqual(low, 0.609, places=2)
        self.assertAlmostEqual(high, 0.911, places=2)
        self.assertEqual(wilson(0, 0), (0.0, 1.0))
        self.assertEqual(p95([5, 1, 2, 3, 4]), 5)
        self.assertIsNone(p95([]))

    def test_paired_bootstrap_is_reproducible_and_flags_zero(self):
        a, b = [1] * 15 + [0] * 5, [1] * 10 + [0] * 10
        first, second = paired_bootstrap(a, b), paired_bootstrap(a, b)
        self.assertEqual(first, second)
        self.assertAlmostEqual(first["difference"], 0.25)
        self.assertFalse(first["inconclusive"])
        self.assertTrue(paired_bootstrap([1, 0, 1, 0], [0, 1, 0, 1])["inconclusive"])
        with self.assertRaises(ValueError):
            paired_bootstrap([1], [1, 0])


if __name__ == "__main__":
    unittest.main()
