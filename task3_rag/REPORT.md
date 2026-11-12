# Task 3 — RAG under the microscope

## Summary

The final system is **R5**, a hybrid retriever enhanced with LLM query decomposition and a
deterministic identifier-chain second hop. On the 120 answerable dev questions (the 30
unanswerable questions are excluded from retrieval metrics) it reaches **Context Recall@5 =
0.975** and **Context Precision@5 = 0.872**. On a stratified 50-question judge sample every
answer was scored correct (1.000), with faithfulness 0.988 and abstention 0.200. The 100-row
test submission is in `predictions_test.jsonl`.

All experiments use only the standard library, `numpy`, `rank-bm25`, `tiktoken`, and the
provided `common/` client helpers. `uv` was unavailable, so commands use `.venv/bin/python`.

## Data and setup

- Corpus: 932 documents (config, error, CLI, incident, changelog, forum, etc.).
- Dev: 150 questions, 30 each of `identifier`, `paraphrase`, `multi-hop`, `version`,
  `unanswerable`.
- Test: 100 questions with hidden labels.
- Chat model: `openai.gpt-6-luna`. Embedding model: `kiconnect.qwen3-embedding-8b` (4096 dims).
- Embeddings and query rewrites are cached under `.cache/task3_rag/`; the cache is not part of
  the submission.
- `temperature` is not sent to the chat model. Generation and judging use generous token limits.

## Implementation

### Chunking

`chunking.py` loads the JSONL corpus and attaches a short metadata header to every chunk:
document type, title, version, and source doc ID. This makes version and type visible to the
embedding model and to BM25.

- Fixed chunking: token windows with configurable size and overlap.
- Recursive chunking: split on Markdown headings, blank lines, then sentences; pack pieces up
  to the size limit with overlap.
- One cached `tiktoken` encoder is shared by all chunks; the first version re-created the encoder
  for every chunk and added roughly 30 seconds to every build.

Final chunk choices:

| Configuration | Chunker | Size / overlap | Chunks |
|---|---|---:|---:|
| R1 | recursive | 80 / 16 | 3432 |
| R2 | fixed | 160 / 32 | 1249 |
| R3 | recursive | 80 / 16 | 3432 |
| R4 | recursive | 160 / 32 | 1284 |
| R5 | recursive | 160 / 32 | 1284 |

R1 deliberately uses the same chunks as R3; R1 is the BM25-only baseline.

### R1–R4

- **R1 — BM25.** A custom tokenizer keeps identifiers such as `PQ-E1565`, `auth.max_connections`,
  and `--queue-name` intact while still lowercasing ordinary words. BM25 is strong on exact
  identifiers and weak on paraphrases.
- **R2 — Dense.** Embeddings of the raw question, cosine similarity, top-5 documents. Dense
  retrieval handles paraphrases but is weak on exact identifiers and multi-hop questions.
- **R3 — Dense with better chunks.** Same dense retriever as R2, but using the recursive 80-token
  chunks. This is the controlled comparison for the chunking method.
- **R4 — Hybrid.** Dense and BM25 rankings are fused with weighted score normalization
  (`alpha * dense + (1 - alpha) * bm25`), followed by a small forum penalty. Alpha 0.7 was the
  best value in the sweep.

### R5 — decomposition-driven second hop

R5 is R4 plus one enhancement: **LLM-assisted query decomposition with a deterministic
identifier-chain second hop**. The LLM decomposition and the deterministic link resolution are
two parts of the same second-hop enhancement, not two independent additions.

1. The LLM rewrites each question into the original question plus up to three focused
   subqueries. The subqueries emphasize exact identifiers, version names, and the likely answer
   type.
2. Hybrid retrieval is run for every query and the rankings are fused with reciprocal rank
   fusion (`k = 60`).
3. For identifier-shaped questions the second hop is executed deterministically:
   - incident question → exact incident postmortem → extract the linked `PQ-E...` code →
     inject the incident and error pages into the final top-5;
   - error/fix question → exact error page → read the setting or CLI flag in `## How to fix` →
     inject the error and target pages into the final top-5.
4. Injected documents are placed at the front of the top-5. This prevents a correct first-hop
   paraphrase from voting the linked document out of the context.

The deterministic hop was important. Embedding a second subquery and hoping RRF would surface
the linked page was unreliable; resolving the exact identifier and fetching its official page
fixed the multi-hop failures (`dev025`, `dev042`, `dev062`, `dev069`).

The LLM rewrite is cached. The first cold run spent `$0.00991` on 150 rewrite calls; later runs
cost zero for rewriting. A small forum penalty (`-0.03`) is applied after fusion to reduce the
chance of a forum post outranking an official page.

### Generation

`generate.py` builds a grounded prompt from the retrieved documents. It instructs the model to:

- answer only from the provided documentation;
- preserve exact identifiers and version numbers;
- answer the question directly, with no preamble;
- return exactly `The documentation does not contain this information.` when the context is
  insufficient.

Answers are written incrementally, so an interrupted run can be resumed without paying for
completed questions again.

## Retrieval results

Final dev retrieval results over the 120 answerable questions; the 30 unanswerable questions
are scored only by the end-to-end judge:

| Config | Retriever | Chunks | R@5 | P@5 | identifier R | multi-hop R | paraphrase R | version R |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| R1 | BM25 | 3432 | 0.554 | 0.518 | 0.933 | 0.417 | 0.233 | 0.633 |
| R2 | Dense | 1249 | 0.604 | 0.500 | 0.700 | 0.083 | 0.933 | 0.700 |
| R3 | Dense | 3432 | 0.600 | 0.541 | 0.567 | 0.033 | 0.833 | 0.967 |
| R4 | Hybrid | 1284 | 0.683 | 0.650 | 0.867 | 0.267 | 0.833 | 0.767 |
| **R5** | **Hybrid + decomposition + second hop** | **1284** | **0.975** | **0.872** | **1.000** | **1.000** | **0.967** | **0.933** |

R3 chunk-size sweep:

| Chunk size | R@5 | P@5 | identifier R | multi-hop R | paraphrase R | version R |
|---:|---:|---:|---:|---:|---:|---:|
| 80 | 0.600 | 0.541 | 0.567 | 0.033 | 0.833 | 0.967 |
| 160 | 0.554 | 0.467 | 0.567 | 0.017 | 0.867 | 0.767 |
| 320 | 0.571 | 0.482 | 0.567 | 0.017 | 0.933 | 0.767 |

R4 alpha sweep (160/32):

| alpha | R@5 | P@5 |
|---:|---:|---:|
| 0.3 | 0.642 | 0.599 |
| 0.5 | 0.671 | 0.626 |
| **0.7** | **0.683** | **0.650** |

R5 sweep (160/32, forum penalty 0.03 unless noted):

| alpha | penalty | R@5 | P@5 | identifier R | multi-hop R | paraphrase R | version R |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 0.65 | 0.03 | 0.983 | 0.858 | 1.000 | 1.000 | 0.967 | 0.967 |
| **0.70** | **0.03** | **0.975** | **0.872** | **1.000** | **1.000** | **0.967** | **0.933** |
| 0.75 | 0.03 | 0.975 | 0.872 | 1.000 | 1.000 | 0.967 | 0.933 |
| 0.80 | 0.03 | 0.967 | 0.870 | 1.000 | 1.000 | 0.933 | 0.933 |
| 0.85 | 0.03 | 0.967 | 0.859 | 1.000 | 1.000 | 0.933 | 0.933 |
| 0.70 | 0.02 | 0.975 | 0.872 | 1.000 | 1.000 | 0.967 | 0.933 |
| 0.70 | 0.04 | 0.975 | 0.872 | 1.000 | 1.000 | 0.967 | 0.933 |
| 0.70 | 0.05 | 0.975 | 0.872 | 1.000 | 1.000 | 0.967 | 0.933 |

Alpha 0.65 has the highest recall (0.983), but alpha 0.70 has a slightly better F1
(0.920 vs 0.916) and higher precision. Penalty values from 0.02 to 0.05 are indistinguishable
on these metrics, so `alpha = 0.7`, `penalty = 0.03` was chosen.

## End-to-end results

The two best retrievers were judged on a stratified 50-question sample (10 per category):

| Config | Category | n | Abstained | Faithfulness | Relevance | Correctness |
|---|---|---:|---:|---:|---:|---:|
| R4 hybrid | OVERALL | 50 | 0.440 | 0.982 | 0.615 | 0.740 |
| R4 hybrid | identifier | 10 | 0.200 | 1.000 | 0.739 | 0.800 |
| R4 hybrid | multi-hop | 10 | 1.000 | — | 0.378 | 0.000 |
| R4 hybrid | paraphrase | 10 | 0.000 | 1.000 | 0.734 | 0.900 |
| R4 hybrid | version | 10 | 0.000 | 0.950 | 0.797 | 1.000 |
| R4 hybrid | unanswerable | 10 | 1.000 | — | 0.425 | 1.000 |
| **R5 final** | **OVERALL** | **50** | **0.200** | **0.988** | **0.616** | **1.000** |
| **R5 final** | **identifier** | **10** | **0.000** | **1.000** | **0.734** | **1.000** |
| **R5 final** | **multi-hop** | **10** | **0.000** | **1.000** | **0.445** | **1.000** |
| **R5 final** | **paraphrase** | **10** | **0.000** | **1.000** | **0.704** | **1.000** |
| **R5 final** | **version** | **10** | **0.000** | **0.950** | **0.786** | **1.000** |
| **R5 final** | **unanswerable** | **10** | **1.000** | **—** | **0.414** | **1.000** |

R5 removes all multi-hop abstentions and all sampled identifier/paraphrase/version errors. On
the full 150-question dev set it abstained exactly 30 times, once for each unanswerable
question, and never on an answerable question. The remaining relevance score is low mainly
because the judge is strict about short but complete answers, and because the unanswerable
answers are intentionally generic abstentions.

### Manual judge check

Ten R5 decisions were re-read manually. The judge was reasonable in all ten; `dev145` is the
only borderline case, because the answer omits one explanatory sentence from the reference even
though it contains the required fix.

| Question | Category | Judge | Manual check |
|---|---|---|---|
| dev145 | identifier | correct 1.0, faith 1.0 | Agree; answer omits “Throttling protects the node” but gives the required actions |
| dev008 | identifier | correct 1.0, faith 1.0 | Agree |
| dev025 | multi-hop | correct 1.0, faith 1.0 | Agree; second hop supplied the linked error page |
| dev030 | multi-hop | correct 1.0, faith 1.0 | Agree |
| dev004 | paraphrase | correct 1.0, faith 1.0 | Agree |
| dev011 | paraphrase | correct 1.0, faith 1.0 | Agree |
| dev009 | version | correct 1.0, faith 1.0 | Agree |
| dev033 | version | correct 1.0, faith 1.0 | Agree |
| dev013 | unanswerable | correct 1.0, abstained | Agree |
| dev054 | unanswerable | correct 1.0, abstained | Agree |

## Failure analysis

R5 has only three retrieval failures on dev (`dev003`, `dev078`, `dev100`). All three are
out-of-context cases where the model still produced a correct answer from other evidence or
prior knowledge, which is useful but less trustworthy. The fifth case is an R4 baseline failure
that motivates the R5 second hop.

1. **`dev003` — paraphrase / synonym mismatch (retrieval miss).** The question describes the
   partition coordinator indirectly (“the component that decides which consumer reads which
   partition”) and asks for the error code. R5 returned changelog documents, not the canonical
   error page `d0759`, so Context Recall was 0. The model nevertheless answered
   `PQ-E1565 (COORDINATOR_CONNECTION_LIMIT_REACHED)`, likely from a changelog mention. This is
   a lucky correct answer, not a grounded retrieval success. A glossary-based query expansion
   or a cross-encoder reranker would make this robust.

2. **`dev078` — version / changelog retrieval miss.** The question asks when
   `partitioner.max_retries` changed and what the previous default was. R5 retrieved current
   config and unrelated pages but not the 4.0 changelog. The answer was still correct
   (`4.0`, previous default `10`), but the canonical changelog was absent. Version questions
   need an explicit changelog lookup or a metadata filter on `type=changelog` plus the target
   release.

3. **`dev100` — version / weak grounding.** The question asks when
   `partitioner.compression` changed and what the previous default was. R5 returned the current
   config page and a 3.x page, but missed the changelog. The answer was correct (`4.0`,
   previous default `lz4`), but the judge gave faithfulness 0.5 because only part of the answer
   was supported by the retrieved context. This is the most important residual risk: a correct
   answer that is not fully grounded.

4. **`dev145` — generation completeness.** Retrieval was correct (error page plus setting), and
   the answer contained the required actions, but it omitted the reference’s opening sentence
   (“Throttling protects the node”). The judge still gave correctness 1.0. The prompt could
   explicitly ask the model to preserve conditions and explanations from the `## How to fix`
   section, or ask for a short bullet list.

5. **R4 `dev025` — multi-hop baseline failure.** R4 retrieved the incident postmortem but not
   the error page it links to, and the generator abstained. All ten sampled R4 multi-hop
   questions failed the same way. Hybrid ranking alone cannot follow this link because the
   second document does not share the question’s wording. R5 fixes this by extracting the
   identifier from the anchor page and injecting the exact target page.

## Cost and latency

| Stage | Calls | Prompt tok | Completion tok | Cost USD | Mean latency | Notes |
|---|---:|---:|---:|---:|---:|---|
| R1 retrieval | 0 | 0 | 0 | 0.00000 | 12.9 ms/q | index 1.74 s, 3432 chunks |
| R2 retrieval | 23 | 0 | 0 | 0.00000 | 51.1 ms/q | index 47.43 s, 1249 chunks |
| R3 retrieval | 54 | 0 | 0 | 0.00000 | 3.3 ms/q | index 100.74 s cold, 3432 chunks |
| R4 retrieval | 0 | 0 | 0 | 0.00000 | 8.9 ms/q | index 1.16 s, 1284 chunks |
| R5 retrieval | 0 | 0 | 0 | 0.00000 | 39.3 ms/q | index 1.27 s; rewrite cache warm |
| R5 rewrite (first cold run) | 150 | 23,754 | 15,075 | 0.00991 | 3.09 s/q | one-time; cached afterwards |
| R4 generation | 150 | 135,024 | 6,634 | 0.01711 | 3.35 s/q | p95 4.11 s |
| R5 generation | 150 | 139,690 | 6,027 | 0.01723 | 2.17 s/q | p95 3.44 s |
| R4 judge | 128 | 35,755 | 10,109 | 0.00863 | 2.43 s/call | 50 sampled questions |
| R5 judge | 141 | 45,333 | 10,385 | 0.00973 | 2.30 s/call | 50 sampled questions |
| R5 test retrieval | 0 | 0 | 0 | 0.00000 | 26.0 ms/q | 100 questions; rewrite cache warm |
| R5 test generation | 100 | 92,826 | 4,258 | 0.01162 | 1.83 s/q | p95 2.59 s |

The final dev/test artifacts cost roughly **$0.08** of gateway spend, including the one-time
R5 rewrite pass. Repeated experiments add cost; the largest expense is answer generation, not
retrieval or judging. The R2 and R3 index-build times are dominated by embedding the corpus;
R4/R5 reuse the same chunk embeddings and only rebuild the BM25 index.

## Recommendation

For PaderQueue in production we would deploy **R5**: hybrid retrieval at `alpha=0.7` with a
`forum_penalty=0.03` over recursive 160/32 chunks, plus LLM query decomposition and the
deterministic identifier-chain second hop. On our measured results it is the clear winner. R5
reaches **R@5 = 0.975** and **P@5 = 0.872** on the 120 answerable dev questions, versus 0.683
and 0.650 for R4. On the stratified 50-question judge sample it scores **1.000 correctness** and
**0.988 faithfulness**, and it abstains accurately: on the full dev set it abstained exactly 30
times, once per unanswerable question, and never on an answerable one. The second hop is what
closes the multi-hop gap (R@5 from 0.267 to 1.000), so the deployment gains are concentrated
where R4 fails.

The cost/latency trade-offs are modest. Cold query rewriting is the only meaningful one-time
expense: **~$0.00991 across 150 queries** and **~3.09 s/q** when uncached. Once the rewrite cache
is warm, R5 retrieval runs at **~39.3 ms/q**, which is fast enough for interactive use. Answer
generation adds **~2.17 s/q** and **~$0.00011/query**, and that is easily worthwhile given the
**+0.292 R@5** and **+0.260 correctness** jump over R4. In short, R5 buys a large quality gain
for a small per-query latency and cost increase.

We would pick **R4** instead only when the constraints rule R5 out: for example a strict
sub-second budget that cannot tolerate generation/rewrite LLM calls, or a domain without
structured `INC-`/`PQ-E` identifier chains where the deterministic second hop has nothing to
follow and its benefit would not materialize.

## Test predictions

`task3_rag/predictions_test.jsonl` contains the final R5 test output:

- 100 rows, one per test question;
- fields: `id`, `answer`, `retrieved_doc_ids`;
- 1–5 unique valid document IDs per row;
- 26 abstentions, all with the required exact abstention sentence;
- generated with the same R5 configuration that produced the dev results.

The test labels are hidden, so no test accuracy is reported.

## Reproducing

```bash
# Final R5 dev retrieval
.venv/bin/python -m task3_rag.pipeline \
  --config r5 --split dev --chunk-size 160 --overlap 32 \
  --alpha 0.7 --forum-penalty 0.03 \
  --out task3_rag/results/retrieval_r5_dev.jsonl \
  --timing task3_rag/results/timing_r5_dev.json \
  --log task3_rag/results/logs/retrieval_r5_dev.jsonl

# Evaluate
.venv/bin/python task3_rag/evaluate.py \
  task3_rag/results/retrieval_r5_dev.jsonl --k 5 \
  --json task3_rag/results/metrics_r5_dev.json

# Generate dev answers
.venv/bin/python -m task3_rag.generate \
  task3_rag/results/retrieval_r5_dev.jsonl --split dev \
  --out task3_rag/results/answers_r5_dev.jsonl \
  --log task3_rag/results/logs/generate_r5_dev.jsonl \
  --timing task3_rag/results/timing_generate_r5_dev.json

# Judge a stratified sample
.venv/bin/python task3_rag/judge.py \
  task3_rag/results/answers_r5_dev.jsonl --sample 50 \
  --out task3_rag/results/judge_r5_dev.jsonl

# Final test predictions
.venv/bin/python -m task3_rag.pipeline \
  --config r5 --split test --chunk-size 160 --overlap 32 \
  --alpha 0.7 --forum-penalty 0.03 \
  --out task3_rag/results/retrieval_r5_test.jsonl \
  --timing task3_rag/results/timing_r5_test.json \
  --log task3_rag/results/logs/retrieval_r5_test.jsonl
.venv/bin/python -m task3_rag.generate \
  task3_rag/results/retrieval_r5_test.jsonl --split test --predictions \
  --out task3_rag/predictions_test.jsonl \
  --log task3_rag/results/logs/generate_r5_test.jsonl \
  --timing task3_rag/results/timing_generate_r5_test.json
```

`run_retrieval_suite.py` was used for the sweeps. It runs several configurations in one process
and writes `retrieval_<name>_<split>.jsonl` plus `timing_<name>_<split>.json`. Use the
individual timing files for reporting, because `timing_dev_all.json` is overwritten by
`--only` runs.

## Limitations

- The judge is an LLM and has noise; the reported correctness is from a stratified 50-question
  sample, not all 150 dev questions.
- The deterministic second hop depends on identifier-shaped anchors. It works for this corpus
  and the dev/test patterns, but a different domain would need a more general link extractor.
- Paraphrase retrieval remains the main retrieval weakness (`0.967` recall; three dev misses
  overall). A cross-encoder reranker would likely help, but `sentence-transformers` was not
  installed in this environment; it is available as an optional extra via
  `uv sync --extra local`.
- The forum penalty and alpha values were tuned on dev; test behavior may differ.
- Test predictions are unlabeled, so the test score cannot be verified locally.
