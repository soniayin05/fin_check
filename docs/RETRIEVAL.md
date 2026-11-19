# Retrieval (F1)

Filing text is split into chunks and searched with one of five methods through
`PassageStore.search(query, company, year, source_type, k, method)`.

| Method | What it does |
| --- | --- |
| `keyword` | The check-in scorer: counts how many query words a passage contains. Kept as the baseline. |
| `tfidf` | Cosine similarity of log-scaled TF-IDF vectors. |
| `bm25` | Okapi BM25, k1 = 1.5, b = 0.75. |
| `embedding` | Cosine similarity of Gemini `gemini-embedding-001` vectors (768 dimensions). |
| `hybrid` | Reciprocal rank fusion of `bm25` and `embedding`, k = 60. |

Company, fiscal year and source type filters are applied before ranking. The
agent uses `FINCHECK_RETRIEVER` from `.env` (default `keyword`) until the
evaluation below picks a method.

## Chunking

`prepare-data` merges consecutive paragraphs from the same document section
into chunks of about 300 words (at most 400). A table is always its own chunk
and is never split. A paragraph longer than 400 words is split at sentence
boundaries. Chunk IDs are a hash of the chunk text, so they stay the same
across rebuilds unless the source files or these sizes change.

## Embeddings

`python -m fincheck build-index` embeds every chunk once and caches the
vectors in `data/indexes/embeddings/`. Later runs embed only new text. The
command prints the API calls, tokens (as reported by Gemini) and list-price
cost. Embedding and hybrid search need `GEMINI_API_KEY`; without it they stop
with an error and never switch to another method.

## Evaluation workflow

1. `python -m fincheck prepare-data --include-sec`
2. `python -m fincheck build-index`
3. Review and edit `evals/retrieval/questions.jsonl` (see below).
4. `python -m fincheck label-retrieval --labeler "Your Name"`
5. `python -m fincheck eval-retrieval`
6. Optional answer experiment: `python -m fincheck answer-sheet --method <best>`,
   grade the CSV by hand, then `python -m fincheck score-answers <csv>`.

`eval-retrieval` prints, per method, top-5 hit rate with a 95% Wilson
interval, MRR@5, recall@5 and per-query latency. For each pair of methods it
prints a paired bootstrap difference (1,000 resamples); an interval containing
zero is inconclusive. The full per-question output, including where each miss
ranked, is saved under `artifacts/local/<run>/retrieval.json`.

### The question set

The 25 questions in `evals/retrieval/questions.jsonl` are drafts written
before any retrieval results were seen. They cover all three companies, both
10-K filings and earnings releases, and four types:

- `lexical`: uses the filing's own wording (favours keyword methods)
- `paraphrase`: asks in different words (tests embeddings)
- `numeric`: asks for a specific number stated in text
- `table`: the answer is in a table

Before labeling, check each question: is it something a junior analyst would
ask, and is the fiscal year one that is in your index? Edit or replace
questions freely, and record your changes in `notes` and `author`.

### Labeling rules

`label-retrieval` shows, for each question, the top 8 chunks from every
available method in one pooled list, so no method gets an advantage. `g
<words>` searches all chunks for that company and year to find relevant text
no method returned.

- Mark a chunk relevant if it alone states the answer or the key fact needed.
- A chunk that only mentions the topic is not relevant.
- Mark every relevant chunk, including the same text in the 10-K and in the
  annual report web page.
- Enter `n` if no chunk answers the question. Such questions are reported but
  not scored.
- The optional expected answer is a short reference for the answer experiment.

Labels store the index hash and the first words of each chosen chunk. If a
rebuild changes chunk IDs, `eval-retrieval` lists the affected questions as
label problems instead of counting them as misses; relabel them with
`label-retrieval --only <id>`.

Labels are made on development data. Questions inspected while choosing the
retrieval method are not held-out cases.

### Measurement boundaries

- Latency is wall time inside `search()`. For `embedding` and `hybrid` it
  includes the Gemini call that embeds the query, unless the query vector was
  already cached. `embedding_usage.cache_hits` in the saved output shows how
  many were.
- Cost is the embedding list price per million input tokens, as of
  2026-11-16. Free-tier usage is not billed.
- The answer experiment uses manual grading only. No model grades answers.
