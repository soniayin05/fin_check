"""Passage parsing, chunking and retrieval (F1).

Five retrieval methods share one interface, PassageStore.search():
  keyword   - the check-in scorer (query-term coverage); kept as a baseline
  tfidf     - cosine similarity of log-scaled TF-IDF vectors
  bm25      - Okapi BM25 (k1=1.5, b=0.75)
  embedding - cosine similarity of Gemini embeddings (needs an embedder)
  hybrid    - reciprocal rank fusion of bm25 and embedding (k=60)

Filters (company, fiscal year, source type) are applied before ranking.
Corpus statistics (document frequencies) are computed once over the whole
index when the store loads, not per query.
"""

from collections import Counter
from html.parser import HTMLParser
import hashlib
import json
import math
import os
from pathlib import Path
import re
from .tools import normalize_company


# Text inside these elements is never evidence. ix:header holds hidden inline-XBRL facts.
SKIPPED = {"script", "style", "ix:header"}
ITEM = re.compile(r"^(?:part\s+[iv]+\W+)?item\s+(\d{1,2}[abc]?)\b", re.IGNORECASE)
ITEM_NAMES = {"1": "Business", "1A": "Risk Factors", "7": "MD&A", "7A": "Market Risk",
              "8": "Financial Statements", "9A": "Controls and Procedures"}


class FilingParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.table_depth = 0
        self.buffer = []
        self.blocks = []
        self.sections = []  # section label for each kept block
        self.kinds = []     # "table" or "text" for each kept block
        self.section = None

    def flush(self):
        text = " ".join(" ".join(self.buffer).split())
        # Short blocks only, so a table of contents never sets the section. A heading laid out
        # as a one-row table still counts, as it did before tables were tagged.
        heading = ITEM.match(text) if len(text) < 120 else None
        if heading:  # a 10-K "Item 7." heading; short headings are tracked, not kept
            number = heading.group(1).upper()
            self.section = f"Item {number}" + (f" {ITEM_NAMES[number]}" if number in ITEM_NAMES else "")
        if len(text) >= 50:
            self.blocks.append(text)
            self.sections.append(self.section)
            self.kinds.append("table" if self.table_depth else "text")
        self.buffer = []

    def handle_starttag(self, tag, attrs):
        if tag in SKIPPED:
            self.skip += 1
        if self.skip:
            return
        if tag == "table":
            if not self.table_depth:
                self.flush()
            self.table_depth += 1
        elif not self.table_depth and tag in {"p", "div", "h1", "h2", "h3", "li"}:
            self.flush()

    def handle_endtag(self, tag):
        if tag in SKIPPED:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag == "table":
            if self.table_depth == 1:
                self.flush()  # still inside the table, so the block is recorded as one
            self.table_depth = max(0, self.table_depth - 1)
        elif not self.table_depth and tag in {"p", "div", "h1", "h2", "h3", "li"}:
            self.flush()

    def handle_data(self, data):
        if not self.skip:
            self.buffer.append(data)


def parse_filing(html: str, url: str, company: str = "MSFT", year: int = 2024,
                 prefix: str = "msft-ar2024", source_type: str = "annual_report_web",
                 metadata: dict | None = None) -> list[dict]:
    """Split one document into passages with stable, content-derived IDs."""
    parser = FilingParser()
    parser.feed(html)
    parser.flush()
    passages = []
    seen = set()
    for position, (text, section, kind) in enumerate(zip(parser.blocks, parser.sections, parser.kinds)):
        digest = hashlib.sha256(text.encode()).hexdigest()[:16]
        if digest in seen:
            continue
        seen.add(digest)
        passage = {"id": f"{prefix}-{digest}", "doc_id": prefix, "company": company,
                   "year": year, "url": url, "block": position, "kind": kind,
                   "source_type": source_type, "section": section, "text": text}
        passage.update(metadata or {})
        passages.append(passage)
    return passages


CHUNK_TARGET_WORDS = 300   # stop adding blocks once a chunk reaches this size
CHUNK_MAX_WORDS = 400      # a single text block longer than this is split at sentences


def _split_long(text: str, target: int) -> list[str]:
    sentences = re.split(r"(?<=[.!?])\s+", text)
    pieces, current = [], []
    for sentence in sentences:
        if current and len(" ".join(current + [sentence]).split()) > target:
            pieces.append(" ".join(current))
            current = []
        current.append(sentence)
    if current:
        pieces.append(" ".join(current))
    return pieces


def chunk_passages(blocks: list[dict], target: int = CHUNK_TARGET_WORDS,
                   maximum: int = CHUNK_MAX_WORDS) -> list[dict]:
    """Merge consecutive text blocks of one document section into ~target-word chunks.

    Tables are never merged with text and never split, so a row keeps its header.
    A chunk ID is its document prefix plus a hash of its text, so it is stable
    across rebuilds as long as the source document and these parameters are unchanged.
    """
    chunks, current = [], []

    def emit(parts: list[dict], text: str | None = None):
        first = parts[0]
        body = text if text is not None else "\n\n".join(part["text"] for part in parts)
        chunk = {key: value for key, value in first.items() if key not in {"id", "text", "block"}}
        prefix = first.get("doc_id") or first["id"].rsplit("-", 1)[0]
        chunk.update(id=f"{prefix}-{hashlib.sha256(body.encode()).hexdigest()[:16]}",
                     blocks=[parts[0]["block"], parts[-1]["block"]], text=body)
        chunks.append(chunk)

    def flush():
        if current:
            emit(list(current))
            current.clear()

    for block in blocks:
        words = len(block["text"].split())
        same_place = current and (current[0].get("doc_id"), current[0].get("section")) == (
            block.get("doc_id"), block.get("section"))
        if block.get("kind") == "table":
            flush()
            emit([block])
            continue
        if words > maximum:
            flush()
            for piece in _split_long(block["text"], target):
                emit([block], piece)
            continue
        if current and (not same_place or len(" ".join(p["text"] for p in current).split()) + words > maximum):
            flush()
        current.append(block)
        if len(" ".join(p["text"] for p in current).split()) >= target:
            flush()
    flush()
    seen, unique = set(), []
    for chunk in chunks:  # identical boilerplate chunks in one document collapse to one
        if chunk["id"] not in seen:
            seen.add(chunk["id"])
            unique.append(chunk)
    return unique


STOPWORDS = frozenset("""a an and are as at be been by for from has have in is it its of on or that the
their this to was were which will with we our us not but also such than other may any these those""".split())
KEYWORD_STOPWORDS = {"the", "and", "of", "in", "a", "is", "was"}  # check-in scorer, unchanged
METHODS = ("keyword", "tfidf", "bm25", "embedding", "hybrid")
BM25_K1, BM25_B, RRF_K, FUSION_DEPTH = 1.5, 0.75, 60, 50


class RetrievalError(ValueError):
    """Unknown method, or a method whose requirements (an embedder) are not configured."""


def tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def terms(text: str) -> list[str]:
    return [word for word in tokens(text) if word not in STOPWORDS]


def embedding_title(passage: dict) -> str:
    parts = [passage.get("company"), passage.get("source_type"), f"FY{passage.get('year')}", passage.get("section")]
    return " ".join(str(part) for part in parts if part)


class PassageStore:
    def __init__(self, path: Path | None = None, embedder=None, default_method: str | None = None):
        self.embedder = embedder
        self.default_method = default_method or os.getenv("FINCHECK_RETRIEVER") or "keyword"
        self.passages = json.loads(path.read_text()) if path and path.exists() else []

    @property
    def passages(self) -> list[dict]:
        return self._passages

    @passages.setter
    def passages(self, value: list[dict]) -> None:
        """Replacing the passages rebuilds the corpus statistics."""
        self._passages = value
        self.by_id = {p["id"]: p for p in value}
        self._counts = {p["id"]: Counter(terms(p["text"])) for p in value}
        self._keyword_counts = {p["id"]: Counter(tokens(p["text"])) for p in value}
        self._lengths = {key: sum(counts.values()) for key, counts in self._counts.items()}
        total = len(value)
        df = Counter(word for counts in self._counts.values() for word in counts)
        self._avgdl = (sum(self._lengths.values()) / total) if total else 0.0
        self._bm25_idf = {w: math.log(1 + (total - n + 0.5) / (n + 0.5)) for w, n in df.items()}
        self._tfidf_idf = {w: math.log((1 + total) / (1 + n)) + 1 for w, n in df.items()}
        self._tfidf_norm = {key: math.sqrt(sum(((1 + math.log(c)) * self._tfidf_idf[w]) ** 2
                                               for w, c in counts.items())) or 1.0
                            for key, counts in self._counts.items()}

    def candidates(self, company: str, year: int, source_type: str | None = None) -> list[dict]:
        company = normalize_company(company)
        return [p for p in self._passages if (p["company"], p["year"]) == (company, year)
                and (not source_type or p.get("source_type") == source_type)]

    def search(self, query: str, company: str = "MSFT", year: int = 2024,
               source_type: str | None = None, k: int = 5, method: str | None = None) -> list[dict]:
        method = method or self.default_method
        if method not in METHODS:
            raise RetrievalError(f"Unknown retrieval method '{method}'. Choose from {', '.join(METHODS)}")
        pool = self.candidates(company, year, source_type)
        scored = getattr(self, f"_{method}")(query, pool) if pool else []
        scored = [(score, p) for score, p in scored if score > 0]
        scored.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
        return [dict(p, score=round(score, 6), rank=rank, method=method)
                for rank, (score, p) in enumerate(scored[:k], start=1)]

    def _keyword(self, query, pool):
        words = set(tokens(query)) - KEYWORD_STOPWORDS
        # Coverage first; bounded frequency breaks ties without favoring long tables.
        return [(sum(1 + min(self._keyword_counts[p["id"]][w], 3) * .1
                     for w in words if self._keyword_counts[p["id"]][w]), p) for p in pool]

    def _tfidf(self, query, pool):
        q = Counter(terms(query))
        qvec = {w: (1 + math.log(c)) * self._tfidf_idf[w] for w, c in q.items() if w in self._tfidf_idf}
        qnorm = math.sqrt(sum(v * v for v in qvec.values())) or 1.0
        results = []
        for p in pool:
            counts = self._counts[p["id"]]
            dot = sum(weight * (1 + math.log(counts[w])) * self._tfidf_idf[w]
                      for w, weight in qvec.items() if counts[w])
            results.append((dot / (qnorm * self._tfidf_norm[p["id"]]), p))
        return results

    def _bm25(self, query, pool):
        words = [w for w in set(terms(query)) if w in self._bm25_idf]
        results = []
        for p in pool:
            counts, length = self._counts[p["id"]], self._lengths[p["id"]]
            norm = BM25_K1 * (1 - BM25_B + BM25_B * length / (self._avgdl or 1))
            results.append((sum(self._bm25_idf[w] * counts[w] * (BM25_K1 + 1) / (counts[w] + norm)
                                for w in words if counts[w]), p))
        return results

    def _embedding(self, query, pool):
        if self.embedder is None:
            raise RetrievalError("Embedding search needs GEMINI_API_KEY (see docs/RETRIEVAL.md); "
                                 "it never falls back to another method silently.")
        query_vector = self.embedder.embed_query(query)
        vectors = self.embedder.embed_documents(pool)
        # Vectors are unit length, so the dot product is the cosine similarity.
        return [(sum(a * b for a, b in zip(query_vector, vectors[p["id"]])), p) for p in pool]

    def _hybrid(self, query, pool):
        fused: dict[str, float] = {}
        for method in ("bm25", "embedding"):
            ranked = sorted(((s, p) for s, p in getattr(self, f"_{method}")(query, pool) if s > 0),
                            key=lambda pair: (-pair[0], pair[1]["id"]))[:FUSION_DEPTH]
            for rank, (_, p) in enumerate(ranked, start=1):
                fused[p["id"]] = fused.get(p["id"], 0.0) + 1 / (RRF_K + rank)
        return [(fused.get(p["id"], 0.0), p) for p in pool]
