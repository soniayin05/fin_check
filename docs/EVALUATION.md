# Evaluation plan (not completed results)

## Current status

`python -m fincheck eval-dev` runs three deterministic financial-tool checks against a small real-data sample. It does not call a model or perform memo extraction. Passing these checks does not establish agent performance or feature credit.

The starter uses only one Microsoft report. Two source IDs point to two tables in that report; do not count them as two real sources. Add independently sourced runtime data, such as SEC Company Facts plus investor-relations documents.

## Development vs held-out cases

- Develop on 10-15 claims plus memo-level examples. Cases inspected while improving the system remain development cases.
- Reserve at least 30 claims for final reporting (course solo minimum: 25). Do not populate the held-out set now with copies of development cases.
- Use independently verified numeric expected values and manually labeled acceptable passages for disclosures. Keep expected answers out of prompts, retrieval examples, and agent inputs.
- Grade verdict, numerical tolerance, company, period, unit, and supporting evidence. An ID merely existing does not prove support. Manually adjudicate valid alternative evidence according to frozen rules.
- Missing evidence in a bounded dataset is not proof a claim is false. Operational failures must be recorded, not silently dropped from denominators.
- Report claim extraction coverage/qualifier preservation and correction correctness on a small full-memo set. Claim-only accuracy cannot validate memo decomposition.

## Core evaluation

Compare a justified fixed retrieval-and-answer baseline with the final agent on the same cases/data and comparable resource limits. Record one primary run per case/system. Preselect at least five representative cases for three total runs each per system, keeping extra runs separate from the primary denominator.

Save case IDs, inputs, outputs, scores, evidence, model/version or alias and date, settings, retries, data versions, token counts, timings, and cost. Report primary pass counts and 95% Wilson intervals. Report both-pass, agent-only-pass, baseline-only-pass, both-fail counts. Use at least 1,000 paired bootstrap resamples for the pass-rate difference; an interval containing zero is inconclusive.

Report mean and sample p95 latency, repeated-run variability, measurement boundaries (setup, caching, retries, rate-limit waits), actual spending, and estimated per-task list-price cost with provider/date. No paid model calls occur in the current starter; its local timing is not agent latency.

Discuss observed failures in categories, up to ten representative examples. Supplement a low failure count with boundary/fault tests. Do not add an LLM judge casually: the handout requires outside-human validation and bias tests even without claiming F9.

## Feature experiments

| Feature | Evidence to build |
| --- | --- |
| F1 (10) | Chunking, persistent filtered index, hybrid retrieval/reranking. At least 20 source-labeled questions comparing TF-IDF/BM25/embeddings by top-5 source hit rate; answer correctness with/without retrieval; failure categories, cost, latency. |
| F3 (12) | Distinct research/reviewer roles, shared state, handoff traces, controller stop rule and worker failure test. Compare against one agent with all tools on the full core test set; accuracy/cost/latency plus three cases where coordination helped or hurt. |
| F4 (10) | Plan before first tool call, replanning and safe backoff. Fifteen distinct fault cases (5 failures, 5 timeouts, 5 bad-data) vs no-explicit-plan agent; recovery and task success, recovered/unrecovered traces. |
| F7 (8) | Every model/tool call traced, sensitive data redacted, searchable traces. Fix a real bottleneck on dev; compare frozen before/after versions on full core set, including quality and per-step cost/latency. |

Apply the handout's repeated-run and uncertainty rules to binary feature outcomes too. Reuse an evaluation runner and case evidence, but do not rename one experiment as two different feature comparisons.

## Artifact locations

- `evals/dev/`: visible development inputs and expected answers.
- `evals/heldout/`: add only after freezing rules; no current cases.
- `artifacts/local/`: ignored experiments and unreviewed traces.
- `evals/results/`: explicitly reviewed/redacted final evidence to commit.

Before submission, verify required artifacts are in the archive, not just ignored local folders. Never commit secrets; deletion from the latest file alone does not remove git history.
