"""Gemini embeddings with a persistent on-disk cache.

Vectors are requested once per (task, title, text, model, dimension) and stored
under data/indexes/embeddings/, so a rebuilt index or a repeated evaluation
re-embeds only text it has not seen. The cache is two files: a JSON key index
and a float32 binary array (Python's array module; no third-party packages).

Gemini embedding requests carry the task type, so documents and queries are
embedded differently (RETRIEVAL_DOCUMENT vs RETRIEVAL_QUERY). Reduced-dimension
vectors are normalized here to unit length, so a dot product is a cosine.
"""

from array import array
import hashlib
import json
import math
import os
from pathlib import Path
import time
import urllib.error
import urllib.request

from .model import ModelError

DEFAULT_MODEL = "gemini-embedding-001"
DEFAULT_DIM = 768
BATCH_SIZE = 100
MAX_CHARS = 6000  # about 1,500 tokens; keeps each input under the model's input limit
# USD per 1M input tokens, from the Gemini API pricing page as read on 2026-11-16.
PRICE_PER_MILLION = {"gemini-embedding-001": 0.15}


class EmbeddingCache:
    def __init__(self, directory: Path, model: str, dim: int):
        directory.mkdir(parents=True, exist_ok=True)
        stem = f"{model}-{dim}"
        self.index_path, self.data_path = directory / f"{stem}.json", directory / f"{stem}.f32"
        self.dim = dim
        self.keys: list[str] = []
        self.data = array("f")
        if self.index_path.exists() and self.data_path.exists():
            meta = json.loads(self.index_path.read_text())
            if meta.get("dim") != dim or meta.get("model") != model:
                raise ModelError(f"Embedding cache {self.index_path} was built with different settings")
            self.keys = meta["keys"]
            with self.data_path.open("rb") as handle:
                self.data.fromfile(handle, len(self.keys) * dim)
        self.model = model
        self.position = {key: n for n, key in enumerate(self.keys)}
        self.dirty = False

    def get(self, key: str) -> list[float] | None:
        n = self.position.get(key)
        return None if n is None else list(self.data[n * self.dim:(n + 1) * self.dim])

    def put(self, key: str, vector: list[float]) -> None:
        if key in self.position:
            return
        self.position[key] = len(self.keys)
        self.keys.append(key)
        self.data.extend(vector)
        self.dirty = True

    def save(self) -> None:
        if not self.dirty:
            return
        temp = self.data_path.with_suffix(".tmp")
        with temp.open("wb") as handle:
            self.data.tofile(handle)
        temp.replace(self.data_path)
        self.index_path.write_text(json.dumps({"model": self.model, "dim": self.dim, "keys": self.keys}))
        self.dirty = False


def normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    if not norm:
        raise ModelError("Gemini returned a zero embedding vector")
    return [v / norm for v in vector]


def cache_key(task: str, title: str, text: str) -> str:
    return hashlib.sha256(f"{task}\n{title}\n{text}".encode()).hexdigest()


class Embedder:
    """Cache, batching and accounting around a transport function.

    `transport(texts, titles, task)` returns (vectors, prompt_tokens or None).
    Tests pass a fake transport; GeminiEmbedder supplies the real one.
    """

    def __init__(self, transport, cache: EmbeddingCache, model: str, dim: int):
        self.transport, self.cache, self.model, self.dim = transport, cache, model, dim
        self.api_calls = self.api_texts = self.cache_hits = 0
        self.tokens = 0
        self.tokens_estimated = False

    def _embed(self, items: list[tuple[str, str]], task: str) -> list[list[float]]:
        keys = [cache_key(task, title, text[:MAX_CHARS]) for title, text in items]
        missing = {}
        for key, (title, text) in zip(keys, items):
            if self.cache.get(key) is None:
                missing.setdefault(key, (title, text[:MAX_CHARS]))
        self.cache_hits += len(items) - len(missing)
        pending = list(missing.items())
        try:
            for start in range(0, len(pending), BATCH_SIZE):
                batch = pending[start:start + BATCH_SIZE]
                vectors, used = self.transport([t for _, (_, t) in batch], [h for _, (h, _) in batch], task)
                if len(vectors) != len(batch) or any(len(v) != self.dim for v in vectors):
                    raise ModelError(f"Expected {len(batch)} embeddings of size {self.dim}; "
                                     "the API may have ignored the requested dimension")
                self.api_calls += 1
                self.api_texts += len(batch)
                if used is None:  # no usage reported: estimate and say so
                    used = sum(len(text) for _, (_, text) in batch) // 4
                    self.tokens_estimated = True
                self.tokens += used
                for (key, _), vector in zip(batch, vectors):
                    self.cache.put(key, normalize(vector))
        finally:
            self.cache.save()  # keep whatever was paid for, even if a later batch failed
        return [self.cache.get(key) for key in keys]

    def embed_query(self, query: str) -> list[float]:
        return self._embed([("", query)], "RETRIEVAL_QUERY")[0]

    def embed_documents(self, passages: list[dict]) -> dict[str, list[float]]:
        from .retrieval import embedding_title  # avoid an import cycle
        vectors = self._embed([(embedding_title(p), p["text"]) for p in passages], "RETRIEVAL_DOCUMENT")
        return {p["id"]: vector for p, vector in zip(passages, vectors)}

    def usage(self) -> dict:
        price = PRICE_PER_MILLION.get(self.model)
        return {"model": self.model, "dim": self.dim, "api_calls": self.api_calls,
                "texts_embedded": self.api_texts, "cache_hits": self.cache_hits,
                "tokens": self.tokens, "tokens_estimated": self.tokens_estimated,
                "cost_usd": None if price is None else round(self.tokens * price / 1e6, 6),
                "price_note": "List price per 1M input tokens as of 2026-11-16; free-tier usage is not billed."}


def gemini_transport(key: str, model: str, dim: int, min_interval: float = 0.5):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:batchEmbedContents"
    last = [0.0]

    def send(texts: list[str], titles: list[str], task: str):
        requests = []
        for text, title in zip(texts, titles):
            request = {"model": f"models/{model}", "content": {"parts": [{"text": text}]},
                       "taskType": task, "outputDimensionality": dim}
            if title and task == "RETRIEVAL_DOCUMENT":
                request["title"] = title
            requests.append(request)
        body = json.dumps({"requests": requests}).encode()
        for attempt in range(4):
            wait = min_interval - (time.monotonic() - last[0])
            if wait > 0:
                time.sleep(wait)
            last[0] = time.monotonic()
            request = urllib.request.Request(url, data=body, headers={
                "Content-Type": "application/json", "x-goog-api-key": key})
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    payload = json.load(response)
                vectors = [item.get("values", []) for item in payload.get("embeddings", [])]
                return vectors, (payload.get("usageMetadata") or {}).get("promptTokenCount")
            except urllib.error.HTTPError as error:
                if error.code in {429, 500, 502, 503, 504} and attempt < 3:
                    time.sleep(min(60, 5 * 2 ** attempt))
                    continue
                raise ModelError(f"Gemini embedding HTTP {error.code}. Check key, model access and quota.") from None
            except (urllib.error.URLError, TimeoutError):
                if attempt < 3:
                    time.sleep(2)
                    continue
                raise ModelError("Gemini embedding connection failed.") from None
            except (ValueError, KeyError):
                raise ModelError("Gemini embedding returned malformed JSON.") from None
        raise ModelError("Gemini embedding request failed.")
    return send


def gemini_embedder(root: Path, *, cache_only: bool = False) -> Embedder | None:
    """The configured embedder, or None when no API key is set."""
    key = os.getenv("GEMINI_API_KEY", "")
    if not key and not cache_only:
        return None
    model = os.getenv("GEMINI_EMBED_MODEL") or DEFAULT_MODEL
    try:
        dim = int(os.getenv("GEMINI_EMBED_DIM") or DEFAULT_DIM)
    except ValueError:
        raise ModelError("GEMINI_EMBED_DIM must be a whole number such as 768") from None
    if not 64 <= dim <= 3072:
        raise ModelError("GEMINI_EMBED_DIM must be between 64 and 3072")
    cache = EmbeddingCache(root / "data/indexes/embeddings", model, dim)
    if cache_only:
        def transport(texts, titles, task):
            raise ModelError(f"Missing {len(texts)} cached vectors for {task}. Populate the embedding cache first.")
    else:
        transport = gemini_transport(key, model, dim)
    return Embedder(transport, cache, model, dim)
