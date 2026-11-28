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
6. Compare answers with and without retrieval. See **Answer experiment** below.

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
- Mark every relevant chunk within the source type requested by the question.
  Use `f <number>` to read a complete candidate.
- Enter `n` if no chunk answers the question. Such questions are reported but
  not scored.
- The optional expected answer is a short reference for the answer experiment.

Labels store the index hash and the first words of each chosen chunk. If a
rebuild changes chunk IDs, `eval-retrieval` lists the affected questions as
label problems instead of counting them as misses; relabel them with
`label-retrieval --only <id>`.

Labels are made on development data. Questions inspected while choosing the
retrieval method are not held-out cases.

### Review newly retrieved passages

With document and query vectors cached, run:

```bash
python3 -m fincheck label-retrieval --review-new --cached-embeddings --labeler "Your Name"
```

This shows candidates beyond the existing lexical review in rank order. Enter
the numbers of relevant additions or `n` if none of the new candidates answers
the question. This mode saves the human review alongside the old labels. It
skips already reviewed pools on the next run. Use `q` to stop or
`--only rq-07` to revisit a single question. Then rerun the comparison:

```bash
python3 -m fincheck eval-retrieval --cached-embeddings
```

Both commands use the saved vectors. A missing vector stops the command with
an error.

### Answer experiment

After reviewing the labels and reference answers, preview the model call
count and then generate answers on the same questions:

```bash
python3 -m fincheck answer-sheet --method embedding --cached-embeddings --dry-run
python3 -m fincheck answer-sheet --method embedding --cached-embeddings
```

The experiment uses one model call per condition per question. It alternates
which condition goes first and keeps reference answers out of model prompts.
Questions marked as having no relevant passage are excluded.

The command prints the CSV path and saves a companion `.run.json` with each
answer, the retrieved passages, token usage, timing and any interruption.
If interrupted, rerun `answer-sheet "<saved CSV path>" --resume --method
embedding --cached-embeddings`. It skips answers already saved for the same
questions, model and evidence.

In the CSV, enter `1` or `0` in each grade column. Check the answer against the
reference and confirm that any citations in the retrieval condition point to
passages supporting the answer. Grade a wrong, missing or unsupported answer
as `0`. Run `python3 -m fincheck score-answers "<saved CSV path>"` for paired
accuracy and uncertainty.

### Measurement boundaries

- Latency is wall time inside `search()`. For `embedding` and `hybrid` it
  includes the Gemini call that embeds the query, unless the query vector was
  already cached. `embedding_usage.cache_hits` in the saved output shows how
  many were.
- Cost is the embedding list price per million input tokens, as of
  2026-11-16. Free-tier usage is not billed.
- The answer experiment uses manual grading only. No model grades answers.
