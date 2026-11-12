"""Retrieval components for Task 3: BM25, dense, hybrid, and query-rewrite fusion.

The gateway embedding model is used through ``LLMClient.embed``.  Embeddings are
cached content-addressed under ``.cache/task3_rag/`` (git-ignored), never inside
``task3_rag/``.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np

try:  # support both ``python -m task3_rag.pipeline`` and direct script execution
    from .chunking import Chunk, count_tokens
except ImportError:  # pragma: no cover
    from chunking import Chunk, count_tokens

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_ROOT = REPO_ROOT / ".cache" / "task3_rag"


# --------------------------------------------------------------------- embeddings


def _safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text)[:120] or "default"


@dataclass
class EmbeddingBatch:
    vectors: np.ndarray
    hits: int
    misses: int
    embedding_time_s: float
    estimated_tokens: int

    @property
    def all_cached(self) -> bool:
        return self.misses == 0


_STORE_CACHE: dict[Path, dict[str, np.ndarray]] = {}


class EmbeddingStore:
    """Content-addressed embedding cache backed by ``.cache/task3_rag``.

    Vectors are kept in one ``vectors.npz`` per embedding model.  The cache is
    content-addressed by a SHA-256 of the text, so identical chunks/queries are
    reused across configurations, and it never lives inside ``task3_rag/``.
    """

    def __init__(self, llm, model: str | None = None, cache_root: Path | None = None) -> None:
        self.llm = llm
        self.model = model or getattr(llm, "embedding_model", None) or "default"
        self.dir = (cache_root or CACHE_ROOT) / "embeddings_v2" / _safe_name(self.model)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.store_path = self.dir / "vectors.npz"
        cached = _STORE_CACHE.get(self.store_path)
        if cached is not None:
            self.vectors = cached
            return
        self.vectors: dict[str, np.ndarray] = {}
        if self.store_path.exists():
            with np.load(self.store_path) as data:
                for key in data.files:
                    self.vectors[key.removeprefix("h_")] = np.asarray(data[key], dtype=np.float32)
        else:
            self._migrate_legacy()
        _STORE_CACHE[self.store_path] = self.vectors

    def _migrate_legacy(self) -> None:
        """Import the old one-file-per-vector cache once, if it exists."""
        legacy = (CACHE_ROOT / "embeddings" / _safe_name(self.model))
        index_path = legacy / "index.json"
        if not index_path.exists():
            return
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return
        for digest, filename in index.items():
            path = legacy / filename
            if path.exists():
                self.vectors[digest] = np.load(path).astype(np.float32)
        if self.vectors:
            self._save()

    @staticmethod
    def _hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _save(self) -> None:
        if not self.vectors:
            return
        tmp = self.dir / "vectors.tmp"
        with tmp.open("wb") as handle:
            np.savez(handle, **{f"h_{digest}": vector for digest, vector in self.vectors.items()})
        tmp.replace(self.store_path)
        _STORE_CACHE[self.store_path] = self.vectors

    def embed(self, texts: Sequence[str], batch_size: int = 64) -> EmbeddingBatch:
        texts = list(texts)
        hashes = [self._hash(text) for text in texts]
        hits = sum(1 for digest in hashes if digest in self.vectors)
        missing_order: list[str] = []
        text_by_hash: dict[str, str] = {}
        for digest, text in zip(hashes, texts):
            if digest not in self.vectors and digest not in text_by_hash:
                text_by_hash[digest] = text
                missing_order.append(digest)
        misses = len(hashes) - hits
        embedding_time = 0.0
        estimated_tokens = sum(count_tokens(text) for text in texts)
        if missing_order:
            started = time.perf_counter()
            vectors = self.llm.embed(
                [text_by_hash[digest] for digest in missing_order], batch_size=batch_size
            )
            embedding_time = time.perf_counter() - started
            vectors = np.asarray(vectors, dtype=np.float32)
            if vectors.shape[0] != len(missing_order):
                raise RuntimeError("embedding backend returned the wrong number of vectors")
            for digest, vector in zip(missing_order, vectors):
                self.vectors[digest] = vector.astype(np.float32)
            self._save()

        loaded: dict[str, np.ndarray] = {}
        result = []
        for digest in hashes:
            if digest not in loaded:
                loaded[digest] = self.vectors[digest]
            result.append(loaded[digest])
        return EmbeddingBatch(
            vectors=np.stack(result) if result else np.zeros((0, 0), dtype=np.float32),
            hits=hits,
            misses=misses,
            embedding_time_s=embedding_time,
            estimated_tokens=estimated_tokens,
        )


def normalize_rows(matrix: np.ndarray) -> np.ndarray:
    matrix = np.asarray(matrix, dtype=np.float32)
    if matrix.size == 0:
        return matrix
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


# -------------------------------------------------------------------------- BM25


TOKEN_RE = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*", re.IGNORECASE)
SEPARATOR_RE = re.compile(r"[._-]+")


def tokenize(text: str) -> list[str]:
    """Tokenize technical text while keeping identifiers searchable.

    ``PQ-E4663`` is indexed both as the full code and as ``pq``/``e4663``;
    ``spool.buffer_bytes`` also contributes ``spool``, ``buffer`` and ``bytes``;
    ``2.x`` contributes ``2.x``, ``2`` and ``x``.  This makes exact identifiers
    match while still allowing partial matches.
    """
    tokens: list[str] = []
    for raw in TOKEN_RE.findall(text.lower()):
        tokens.append(raw)
        if SEPARATOR_RE.search(raw):
            tokens.extend(part for part in SEPARATOR_RE.split(raw) if part)
    return tokens


class BM25Retriever:
    def __init__(self, chunks: Sequence[Chunk]) -> None:
        from rank_bm25 import BM25Okapi

        self.chunks = list(chunks)
        self.tokenized = [tokenize(chunk.text) for chunk in self.chunks]
        started = time.perf_counter()
        self.bm25 = BM25Okapi(self.tokenized)
        self.build_time_s = time.perf_counter() - started

    def scores(self, query: str) -> np.ndarray:
        return np.asarray(self.bm25.get_scores(tokenize(query)), dtype=np.float32)

    def rank_chunks(self, query: str, top_n: int = 200) -> tuple[list[int], np.ndarray]:
        scores = self.scores(query)
        order = np.argsort(-scores)[:top_n]
        return [int(index) for index in order], scores


# ------------------------------------------------------------------------- dense


class DenseRetriever:
    def __init__(self, chunks: Sequence[Chunk], vectors: np.ndarray) -> None:
        self.chunks = list(chunks)
        self.vectors = normalize_rows(vectors)

    def scores(self, query_vector: np.ndarray) -> np.ndarray:
        query_vector = np.asarray(query_vector, dtype=np.float32)
        norm = np.linalg.norm(query_vector)
        if norm:
            query_vector = query_vector / norm
        return (self.vectors @ query_vector).astype(np.float32)

    def rank_chunks(self, query_vector: np.ndarray, top_n: int = 200) -> tuple[list[int], np.ndarray]:
        scores = self.scores(query_vector)
        order = np.argsort(-scores)[:top_n]
        return [int(index) for index in order], scores


# ------------------------------------------------------------------------ hybrid


def minmax(scores: np.ndarray) -> np.ndarray:
    if scores.size == 0:
        return scores
    low, high = float(np.min(scores)), float(np.max(scores))
    if high - low <= 1e-12:
        return np.zeros_like(scores, dtype=np.float32)
    return ((scores - low) / (high - low)).astype(np.float32)


class HybridRetriever:
    """Min-max normalised convex combination of BM25 and dense chunk scores."""

    def __init__(
        self,
        chunks: Sequence[Chunk],
        bm25: BM25Retriever,
        dense: DenseRetriever,
        alpha: float = 0.5,
    ) -> None:
        if not 0.0 <= alpha <= 1.0:
            raise ValueError("alpha must be in [0, 1]")
        self.chunks = list(chunks)
        self.bm25 = bm25
        self.dense = dense
        self.alpha = alpha

    def combined_scores(self, query: str, query_vector: np.ndarray) -> np.ndarray:
        sparse = minmax(self.bm25.scores(query))
        dense = minmax(self.dense.scores(query_vector))
        return (self.alpha * dense + (1.0 - self.alpha) * sparse).astype(np.float32)

    def rank_chunks(
        self, query: str, query_vector: np.ndarray, top_n: int = 200
    ) -> tuple[list[int], np.ndarray]:
        scores = self.combined_scores(query, query_vector)
        order = np.argsort(-scores)[:top_n]
        return [int(index) for index in order], scores


# --------------------------------------------------------------- document ranking


def rank_documents(
    chunks: Sequence[Chunk],
    scores: np.ndarray,
    *,
    k: int = 5,
    pool: int = 200,
    type_prior: dict[str, float] | None = None,
) -> list[dict]:
    """Rank unique documents by their best chunk, optionally with a type prior."""
    order = np.argsort(-scores)[:pool]
    best: dict[str, tuple[float, int]] = {}
    for raw_index in order:
        index = int(raw_index)
        chunk = chunks[index]
        if chunk.doc_id not in best or float(scores[index]) > best[chunk.doc_id][0]:
            best[chunk.doc_id] = (float(scores[index]), index)
    type_prior = type_prior or {}
    items = [
        (doc_id, score + type_prior.get(chunks[index].doc_type, 0.0), index)
        for doc_id, (score, index) in best.items()
    ]
    items.sort(key=lambda item: (-item[1], item[0]))
    return [
        {
            "doc_id": doc_id,
            "score": score,
            "chunk_index": index,
            "chunk": chunks[index],
        }
        for doc_id, score, index in items[:k]
    ]


def rrf_fuse(
    rankings: Iterable[Sequence[int]],
    *,
    k: int = 60,
    top_n: int = 200,
    weights: Sequence[float] | None = None,
) -> tuple[list[int], dict[int, float]]:
    """Reciprocal-rank fusion of several chunk rankings.

    ``weights`` optionally gives each ranking a different vote.  R5 uses a
    larger weight for the mined second-hop identifier query: otherwise the
    four first-hop paraphrases outvote the one query that actually links to the
    missing document.
    """
    scores: dict[int, float] = defaultdict(float)
    for position, ranking in enumerate(rankings):
        weight = float(weights[position]) if weights is not None else 1.0
        for rank, index in enumerate(ranking[:top_n], start=1):
            scores[int(index)] += weight / (k + rank)
    order = sorted(scores, key=lambda index: (-scores[index], index))
    return order, dict(scores)


# ---------------------------------------------------------------- query rewriting

REWRITE_PROMPT = """You rewrite a user question into up to 3 concise search queries for a documentation search engine.

Rules:
- Keep exact identifiers unchanged: error codes such as PQ-E4663, configuration keys such as spool.buffer_bytes, CLI flags such as --include-internal, and versions such as 2.x.
- For a multi-hop question, create one query for each fact needed, plus one combined query.
- For paraphrases, include the likely technical terms and synonyms.
- Do not answer the question. Output JSON only.

Question: {question}

Respond with JSON only in this format:
{{"queries": ["query 1", "query 2", "query 3"]}}"""


def parse_queries(text: str, fallback: str) -> list[str]:
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        try:
            data = json.loads(match.group(0))
            queries = [str(item).strip() for item in data.get("queries", []) if str(item).strip()]
            if queries:
                return queries[:3]
        except (json.JSONDecodeError, AttributeError):
            pass
    return [fallback]


class QueryRewriter:
    """LLM query decomposition with a JSONL cache under ``.cache/task3_rag``."""

    def __init__(self, llm, cache_path: Path | None = None, prompt_version: str = "v1") -> None:
        self.llm = llm
        self.cache_path = cache_path or (CACHE_ROOT / "rewrites" / f"rewrites_{prompt_version}.jsonl")
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache: dict[tuple[str, str], list[str]] = {}
        if self.cache_path.exists():
            for line in self.cache_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                record = json.loads(line)
                self.cache[(record["id"], record["question"])] = record["queries"]
        self.stats = {
            "questions": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "chat_calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cost_usd": 0.0,
            "time_s": 0.0,
        }

    def rewrite(self, question_id: str, question: str) -> list[str]:
        key = (question_id, question)
        self.stats["questions"] += 1
        if key in self.cache:
            self.stats["cache_hits"] += 1
            return self.cache[key]

        self.stats["cache_misses"] += 1
        prompt = REWRITE_PROMPT.format(question=question)
        started = time.perf_counter()
        prompt_before = self.llm.total_prompt_tokens
        completion_before = self.llm.total_completion_tokens
        cost_before = self.llm.total_cost_usd
        calls_before = self.llm.n_calls
        queries = [question]
        for attempt in range(2):
            suffix = "" if attempt == 0 else "\n\n(Reply with valid JSON only.)"
            try:
                reply = self.llm.chat([{"role": "user", "content": prompt + suffix}])
            except Exception:
                continue
            queries = parse_queries(reply, question)
            if queries:
                break
        elapsed = time.perf_counter() - started
        self.stats["time_s"] += elapsed
        self.stats["chat_calls"] += self.llm.n_calls - calls_before
        self.stats["prompt_tokens"] += self.llm.total_prompt_tokens - prompt_before
        self.stats["completion_tokens"] += self.llm.total_completion_tokens - completion_before
        self.stats["cost_usd"] += self.llm.total_cost_usd - cost_before

        # Always keep the original question in the fusion, even if the rewrite is poor.
        deduped = [question]
        for query in queries:
            if query.lower() != question.lower() and query not in deduped:
                deduped.append(query)
        deduped = deduped[:4]
        self.cache[key] = deduped
        with self.cache_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {"id": question_id, "question": question, "queries": deduped},
                    ensure_ascii=False,
                )
                + "\n"
            )
        return deduped
