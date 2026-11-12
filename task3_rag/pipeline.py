"""Retrieval pipeline for Task 3.

Examples (from the repository root):

    python -m task3_rag.pipeline --config r1 --split dev \
        --out task3_rag/results/retrieval_r1_dev.jsonl \
        --timing task3_rag/results/timing_r1_dev.json
    python -m task3_rag.pipeline --config r3 --chunk-size 160 --split dev ...
    python -m task3_rag.pipeline --config r4 --alpha 0.5 --split dev ...
    python -m task3_rag.pipeline --config r5 --split dev ...

Every run writes a retrieval JSONL file and a timing/statistics JSON file.  Retrieval
JSONL lines contain ``id`` and ``retrieved_doc_ids`` (the schema required by
``evaluate.py``) plus the retrieved chunk metadata/text used later by the generator.
"""

from __future__ import annotations

import argparse
import re
import json
import sys
import time
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.llm_client import LLMClient  # noqa: E402

try:
    from .chunking import chunk_stats, corpus_stats, load_corpus, load_questions, make_chunks
    from .retrievers import (
        BM25Retriever,
        DenseRetriever,
        EmbeddingStore,
        HybridRetriever,
        QueryRewriter,
        normalize_rows,
        rank_documents,
        rrf_fuse,
    )
except ImportError:  # pragma: no cover - direct ``python task3_rag/pipeline.py``
    from chunking import chunk_stats, corpus_stats, load_corpus, load_questions, make_chunks
    from retrievers import (
        BM25Retriever,
        DenseRetriever,
        EmbeddingStore,
        HybridRetriever,
        QueryRewriter,
        normalize_rows,
        rank_documents,
        rrf_fuse,
    )

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"


def summarize_llm_log(path: Path) -> dict:
    """Count gateway cache hits, tokens and cost from an LLMClient JSONL log."""
    stats = {
        "log_path": str(path),
        "chat_calls": 0,
        "embedding_calls": 0,
        "chat_cache_hits": 0,
        "embedding_cache_hits": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cost_usd": 0.0,
        "duration_s": 0.0,
    }
    if not path.exists():
        return stats
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        kind = entry.get("type")
        if kind == "chat":
            stats["chat_calls"] += 1
            stats["chat_cache_hits"] += int(bool(entry.get("cache_hit")))
            usage = entry.get("usage") or {}
            stats["prompt_tokens"] += usage.get("prompt_tokens") or 0
            stats["completion_tokens"] += usage.get("completion_tokens") or 0
            stats["cost_usd"] += entry.get("cost_usd") or 0.0
            stats["duration_s"] += entry.get("duration_s") or 0.0
        elif kind == "embedding":
            stats["embedding_calls"] += 1
            stats["embedding_cache_hits"] += int(bool(entry.get("cache_hit")))
            stats["cost_usd"] += entry.get("cost_usd") or 0.0
            stats["duration_s"] += entry.get("duration_s") or 0.0
    return stats


def load_question_file(split: str) -> list[dict]:
    path = DATA_DIR / f"questions_{split}.jsonl"
    return load_questions(path)


ERROR_CODE_RE = re.compile(r"\bPQ-E\d{4}\b", re.I)
INCIDENT_RE = re.compile(r"\bINC-\d{4}-\d{3}\b", re.I)
CONFIG_KEY_RE = re.compile(r"\b[a-z][a-z0-9_]{2,}(?:\.[a-z][a-z0-9_]{2,})+\b")
CLI_FLAG_RE = re.compile(r"(?<![\w-])--[a-z][a-z0-9-]{2,}")
BAD_SUFFIXES = {
    ".com", ".org", ".net", ".io", ".de", ".html", ".md", ".json",
    ".yaml", ".yml", ".toml", ".txt", ".log",
}


def _valid_identifier(identifier: str) -> bool:
    lower = identifier.lower()
    return not (
        any(lower.endswith(suffix) for suffix in BAD_SUFFIXES)
        or lower.startswith("www.")
    )


def mine_identifiers(question: str, docs: list[dict], limit: int = 2) -> list[str]:
    """Mine one or two follow-up identifiers for the two PaderQueue multi-hop patterns.

    * Incident question (contains ``INC-...``): read the incident postmortem and
      retrieve the error code it points at.
    * Error-code question (contains ``PQ-E...``): read the "How to fix" section
      of the error page and retrieve the configuration key (or CLI flag) it
      recommends changing.
    * Other questions: no second hop.

    Restricting the mining to these patterns is important: mining every dotted
    word from every top document adds noisy follow-up queries and lowers recall.
    """
    incident = INCIDENT_RE.search(question)
    error = ERROR_CODE_RE.search(question)

    if incident:
        incident_id = incident.group(0).lower()
        for doc in docs:
            if doc["chunk"].doc_type != "incident":
                continue
            if incident_id not in doc["chunk"].title.lower() and incident_id not in doc["chunk"].text.lower():
                continue
            for code in ERROR_CODE_RE.findall(doc["chunk"].text):
                code = code.lower()
                if code not in question.lower() and _valid_identifier(code):
                    return [code]

    if error:
        error_code = error.group(0).lower()
        candidates: list[str] = []
        for doc in docs:
            if doc["chunk"].doc_type != "error" or error_code not in doc["chunk"].text.lower():
                continue
            text = doc["chunk"].text
            fix = text.split("## How to fix", 1)[-1] if "## How to fix" in text else text
            for regex in (CONFIG_KEY_RE, CLI_FLAG_RE):
                for match in regex.finditer(fix):
                    identifier = match.group(0).lower()
                    if identifier not in question.lower() and _valid_identifier(identifier):
                        if identifier not in candidates:
                            candidates.append(identifier)
            if candidates:
                break
        return candidates[:limit]

    return []


def find_official_doc(chunks, identifier: str, expected_type: str, preferred_version: str = "4.x"):
    """Find the official document that contains an exact identifier.

    Used by R5 for the two-hop chain.  Exact identifier lookup is much more
    reliable than BM25 for identifiers such as ``PQ-E2051`` because forum posts
    and unrelated incidents often mention the same code.
    """
    identifier = identifier.lower()
    matches = []
    seen = set()
    for chunk in chunks:
        if chunk.doc_type != expected_type or chunk.doc_id in seen:
            continue
        if identifier in (chunk.title + " " + chunk.text).lower():
            matches.append(chunk)
            seen.add(chunk.doc_id)
    if not matches:
        return None
    preferred = [chunk for chunk in matches if chunk.version == preferred_version]
    chunk = (preferred or matches)[0]
    return {"doc_id": chunk.doc_id, "chunk": chunk, "score": 1.0}


def preferred_version(question: str) -> str:
    for version in ("2.x", "3.x", "4.x"):
        if version in question:
            return version
    return "4.x"


def build_index(
    *,
    config: str,
    split: str,
    chunk_size: int,
    overlap: int,
    alpha: float,
    forum_penalty: float,
    llm: LLMClient,
    cache_root: Path,
    limit: int | None,
    query_batch_size: int,
    rewrite_cache: Path | None,
) -> tuple[list[dict], dict]:
    questions = load_question_file(split)
    if limit:
        questions = questions[:limit]
    corpus = load_corpus(DATA_DIR / "corpus.jsonl")

    strategy = "fixed" if config == "r2" else "recursive"
    started = time.perf_counter()
    chunks = make_chunks(corpus, strategy=strategy, max_tokens=chunk_size, overlap_tokens=overlap)
    chunking_s = time.perf_counter() - started

    needs_dense = config in {"r2", "r3", "r4", "r5"}
    needs_bm25 = config in {"r1", "r4", "r5"}

    embeddings = None
    embedding_stats = {}
    chunk_embedding_s = 0.0
    if needs_dense:
        store = EmbeddingStore(llm, cache_root=cache_root)
        started = time.perf_counter()
        batch = store.embed([chunk.text for chunk in chunks], batch_size=64)
        chunk_embedding_s = time.perf_counter() - started
        embeddings = batch.vectors
        embedding_stats = {
            "chunk_vectors": int(batch.vectors.shape[0]),
            "dimension": int(batch.vectors.shape[1]) if batch.vectors.ndim == 2 else 0,
            "cache_hits": batch.hits,
            "cache_misses": batch.misses,
            "embedding_time_s": round(batch.embedding_time_s, 3),
            "estimated_tokens": batch.estimated_tokens,
            "backend_time_s": round(chunk_embedding_s, 3),
        }

    bm25 = None
    bm25_build_s = 0.0
    if needs_bm25:
        started = time.perf_counter()
        bm25 = BM25Retriever(chunks)
        bm25_build_s = time.perf_counter() - started

    dense = DenseRetriever(chunks, embeddings) if needs_dense else None
    hybrid = HybridRetriever(chunks, bm25, dense, alpha=alpha) if config in {"r4", "r5"} else None

    # Query rewriting must happen before batch query embedding.
    rewriter = None
    rewrite_stats = {}
    rewrites: dict[str, list[str]] = {}
    rewrite_s = 0.0
    if config == "r5":
        rewriter = QueryRewriter(llm, cache_path=rewrite_cache)
        started = time.perf_counter()
        for question in questions:
            rewrites[question["id"]] = rewriter.rewrite(question["id"], question["question"])
        rewrite_s = time.perf_counter() - started
        rewrite_stats = dict(rewriter.stats)
        rewrite_stats["wall_time_s"] = round(rewrite_s, 3)

    # Batch-embed all query texts once (gateway-friendly, low request count).
    query_texts: list[str] = []
    query_index: dict[str, list[int]] = {}
    if config in {"r2", "r3", "r4", "r5"}:
        if config == "r5":
            for question in questions:
                ids = []
                for text in rewrites[question["id"]]:
                    ids.append(len(query_texts))
                    query_texts.append(text)
                query_index[question["id"]] = ids
        else:
            for question in questions:
                query_index[question["id"]] = [len(query_texts)]
                query_texts.append(question["question"])
        query_store = EmbeddingStore(llm, cache_root=cache_root)
        started = time.perf_counter()
        query_batch = query_store.embed(query_texts, batch_size=query_batch_size)
        query_embedding_s = time.perf_counter() - started
        query_vectors = normalize_rows(query_batch.vectors)
        query_embedding_stats = {
            "n_texts": len(query_texts),
            "cache_hits": query_batch.hits,
            "cache_misses": query_batch.misses,
            "embedding_time_s": round(query_batch.embedding_time_s, 3),
            "wall_time_s": round(query_embedding_s, 3),
            "estimated_tokens": query_batch.estimated_tokens,
        }
    else:
        query_vectors = None
        query_embedding_stats = {}

    type_prior = {"forum": -forum_penalty} if forum_penalty else None

    # R5 second hop: deterministic identifier chain.  For an incident question we
    # look up the postmortem, read the error code it points at, and add that error
    # page.  For an error/fix question we look up the error page, read the setting
    # mentioned in "How to fix", and add that setting page.  Both anchor and target
    # are injected into the final top-5, so the first-hop paraphrases cannot vote
    # the linked document out of the context.
    anchor_docs: dict[str, dict] = {}
    target_docs: dict[str, dict] = {}
    target_identifiers: list[str] = []
    if config == "r5":
        fix_intent_words = ("fix", "do when", "recommend", "change", "setting", "default",
                            "current", "resolve", "mitigate", "solution")
        for question in questions:
            question_text = question["question"]
            incident = INCIDENT_RE.search(question_text)
            error = ERROR_CODE_RE.search(question_text)
            if not incident and not error:
                continue
            if incident:
                anchor = find_official_doc(chunks, incident.group(0), "incident", "4.x")
            else:
                anchor = find_official_doc(chunks, error.group(0), "error", "4.x")
            if anchor is None:
                continue
            anchor_docs[question["id"]] = anchor
            targets = mine_identifiers(question_text, [anchor])
            if not targets:
                continue
            if incident or any(word in question_text.lower() for word in fix_intent_words):
                target_query = targets[0]
                if target_query.startswith("pq-e"):
                    expected_type = "error"
                elif target_query.startswith("--"):
                    expected_type = "cli"
                else:
                    expected_type = "config"
                target = find_official_doc(
                    chunks, target_query, expected_type, preferred_version(question_text)
                )
                if target is not None:
                    target_docs[question["id"]] = target
                    target_identifiers.append(target_query)
        extra_query_stats = {
            "anchors": len(anchor_docs),
            "targets": len(target_docs),
            "questions_with_anchor": len(anchor_docs),
            "questions_with_target": len(target_docs),
            "target_identifiers": target_identifiers,
        }
    else:
        extra_query_stats = {}

    results: list[dict] = []
    score_times: list[float] = []
    retrieval_started = time.perf_counter()
    for question in questions:
        started = time.perf_counter()
        if config == "r1":
            order, scores = bm25.rank_chunks(question["question"], top_n=200)
            docs = rank_documents(chunks, scores, k=5, pool=200, type_prior=type_prior)
        elif config in {"r2", "r3"}:
            vector = query_vectors[query_index[question["id"]][0]]
            order, scores = dense.rank_chunks(vector, top_n=200)
            docs = rank_documents(chunks, scores, k=5, pool=200, type_prior=type_prior)
        elif config == "r4":
            vector = query_vectors[query_index[question["id"]][0]]
            order, scores = hybrid.rank_chunks(question["question"], vector, top_n=200)
            docs = rank_documents(chunks, scores, k=5, pool=200, type_prior=type_prior)
        elif config == "r5":
            rankings = []
            for text_index in query_index[question["id"]]:
                vector = query_vectors[text_index]
                ranking, _ = hybrid.rank_chunks(query_texts[text_index], vector, top_n=200)
                rankings.append(ranking)
            _, fused_scores = rrf_fuse(rankings, k=60, top_n=200)
            fused = np.zeros(len(chunks), dtype=np.float32)
            for index, score in fused_scores.items():
                fused[index] = score
            docs = rank_documents(chunks, fused, k=5, pool=200, type_prior=type_prior)

            injections = [
                doc for doc in (anchor_docs.get(question["id"]), target_docs.get(question["id"]))
                if doc is not None
            ]
            if injections:
                injected_ids = {doc["doc_id"] for doc in injections}
                docs = injections + [doc for doc in docs if doc["doc_id"] not in injected_ids]
                docs = docs[:5]
        else:  # pragma: no cover
            raise ValueError(config)
        score_times.append(time.perf_counter() - started)

        results.append(
            {
                "id": question["id"],
                "retrieved_doc_ids": [doc["doc_id"] for doc in docs],
                "retrieved": [
                    {
                        "doc_id": doc["doc_id"],
                        "score": round(float(doc["score"]), 6),
                        "chunk_id": doc["chunk"].chunk_id,
                        "type": doc["chunk"].doc_type,
                        "title": doc["chunk"].title,
                        "version": doc["chunk"].version,
                        "chunk_text": doc["chunk"].text,
                    }
                    for doc in docs
                ],
            }
        )
    scoring_s = time.perf_counter() - retrieval_started

    online_s = scoring_s + (query_embedding_stats.get("wall_time_s", 0.0) if query_embedding_stats else 0.0)
    if config == "r5":
        online_s += rewrite_s

    timing = {
        "config": config,
        "split": split,
        "n_questions": len(questions),
        "chunk_strategy": strategy,
        "chunk_size_tokens": chunk_size,
        "overlap_tokens": overlap,
        "alpha": alpha if config in {"r4", "r5"} else None,
        "forum_penalty": forum_penalty if config in {"r4", "r5"} else 0.0,
        "chat_model": llm.model,
        "embedding_model": llm.embedding_model,
        "corpus": corpus_stats(corpus),
        "chunks": chunk_stats(chunks),
        "chunking_time_s": round(chunking_s, 3),
        "bm25_build_time_s": round(bm25_build_s, 3),
        "embeddings": embedding_stats,
        "query_embeddings": query_embedding_stats,
        "query_rewrite": rewrite_stats,
        "second_hop": extra_query_stats,
        "scoring_time_s": round(scoring_s, 3),
        "online_time_s": round(online_s, 3),
        "mean_scoring_ms": round(1000 * scoring_s / max(1, len(questions)), 2),
        "mean_online_ms": round(1000 * online_s / max(1, len(questions)), 2),
        "p50_scoring_ms": round(1000 * float(np.percentile(score_times, 50)), 2) if score_times else 0.0,
        "p95_scoring_ms": round(1000 * float(np.percentile(score_times, 95)), 2) if score_times else 0.0,
        "index_build_time_s": round(
            chunking_s + bm25_build_s + chunk_embedding_s, 3
        ),
        "llm_usage": {
            "calls": llm.n_calls,
            "prompt_tokens": llm.total_prompt_tokens,
            "completion_tokens": llm.total_completion_tokens,
            "cost_usd": llm.total_cost_usd,
        },
    }
    return results, timing


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a Task 3 retrieval configuration.")
    parser.add_argument("--config", required=True, choices=["r1", "r2", "r3", "r4", "r5"])
    parser.add_argument("--split", default="dev", choices=["dev", "test"])
    parser.add_argument("--chunk-size", type=int, default=160, help="max chunk tokens (including metadata header)")
    parser.add_argument("--overlap", type=int, default=32, help="overlap tokens")
    parser.add_argument("--alpha", type=float, default=0.5, help="dense weight for hybrid R4/R5")
    parser.add_argument("--forum-penalty", type=float, default=0.0,
                        help="score penalty for forum chunks after document ranking (e.g. 0.03)")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timing", type=Path, required=True)
    parser.add_argument("--log", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--query-batch-size", type=int, default=64)
    parser.add_argument("--rewrite-cache", type=Path, default=None)
    args = parser.parse_args()

    if args.log is None:
        args.log = Path("task3_rag/results/logs") / f"{args.config}_{args.split}_{args.chunk_size}.jsonl"
    args.log.parent.mkdir(parents=True, exist_ok=True)
    llm = LLMClient(log_path=args.log)

    results, timing = build_index(
        config=args.config,
        split=args.split,
        chunk_size=args.chunk_size,
        overlap=args.overlap,
        alpha=args.alpha,
        forum_penalty=args.forum_penalty,
        llm=llm,
        cache_root=Path(".cache/task3_rag"),
        limit=args.limit,
        query_batch_size=args.query_batch_size,
        rewrite_cache=args.rewrite_cache,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as handle:
        for row in results:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    timing["gateway_log"] = summarize_llm_log(args.log)
    timing["llm_usage"] = {
        "calls": llm.n_calls,
        "prompt_tokens": llm.total_prompt_tokens,
        "completion_tokens": llm.total_completion_tokens,
        "cost_usd": round(llm.total_cost_usd, 8),
    }
    args.timing.parent.mkdir(parents=True, exist_ok=True)
    args.timing.write_text(json.dumps(timing, indent=2), encoding="utf-8")

    print(
        f"{args.config} {args.split}: {len(results)} questions, "
        f"{timing['chunks']['n_chunks']} chunks, index {timing['index_build_time_s']}s, "
        f"online {timing['mean_online_ms']} ms/query, "
        f"cost ${timing['llm_usage']['cost_usd']:.6f}"
    )
    print(f"Wrote {args.out}")
    print(f"Wrote {args.timing}")


if __name__ == "__main__":
    main()
