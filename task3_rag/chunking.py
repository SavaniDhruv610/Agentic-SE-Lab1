"""Corpus loading and chunking for Task 3.

Two strategies are used:

* ``fixed``      – fixed-size token windows with overlap (R2 baseline)
* ``recursive``  – heading/paragraph/sentence-aware recursive splitting with overlap (R3)

Every chunk starts with a compact metadata header (doc id, title, type, version)
so the generator can tell an authoritative config/error page from a forum thread.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

try:  # local cached encoder: the common helper is correct but slow in tight loops
    import tiktoken

    _ENCODING = tiktoken.get_encoding("cl100k_base")

    def count_tokens(text: str) -> int:
        return len(_ENCODING.encode(text))
except Exception:  # pragma: no cover - tiktoken is an allowed dependency
    from common.llm_client import count_tokens  # type: ignore

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"


@dataclass(frozen=True)
class Document:
    doc_id: str
    type: str
    title: str
    version: str
    text: str


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    doc_id: str
    doc_type: str
    title: str
    version: str
    text: str
    body: str
    chunk_index: int

    @property
    def metadata(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "type": self.doc_type,
            "title": self.title,
            "version": self.version,
            "chunk_index": self.chunk_index,
        }


def load_jsonl(path: str | Path) -> list[dict]:
    with Path(path).open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def load_corpus(path: str | Path | None = None) -> list[Document]:
    records = load_jsonl(path or DATA_DIR / "corpus.jsonl")
    return [
        Document(
            doc_id=record["doc_id"],
            type=record.get("type", ""),
            title=record.get("title", ""),
            version=str(record.get("version", "") or ""),
            text=record["text"],
        )
        for record in records
    ]


def load_questions(path: str | Path) -> list[dict]:
    return load_jsonl(path)


# --------------------------------------------------------------------------- helpers


def _tail_tokens(text: str, max_tokens: int) -> str:
    """Return the longest suffix of ``text`` that fits into ``max_tokens``."""
    if max_tokens <= 0 or not text.strip():
        return ""
    words = text.split()
    for start in range(len(words)):
        candidate = " ".join(words[start:])
        if count_tokens(candidate) <= max_tokens:
            return candidate
    return ""


_RECURSIVE_SEPARATORS = [
    r"(?=\n#{1,4} )",       # headings
    r"\n\s*\n+",             # paragraph boundaries
    r"(?<=[.!?])\s+",        # sentences
    r"\n+",                  # hard line breaks
    r"\s+",                  # words
]


def _split_recursive(text: str, max_tokens: int, separators: list[str]) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if count_tokens(text) <= max_tokens:
        return [text]
    for index, separator in enumerate(separators):
        parts = [part.strip() for part in re.split(separator, text) if part.strip()]
        if len(parts) <= 1:
            continue
        result: list[str] = []
        for part in parts:
            if count_tokens(part) > max_tokens:
                result.extend(_split_recursive(part, max_tokens, separators[index + 1 :]))
            else:
                result.append(part)
        return result
    return [text]


def _merge_pieces(pieces: Iterable[str], max_tokens: int, overlap_tokens: int) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        piece_tokens = count_tokens(piece)
        if current and current_tokens + piece_tokens > max_tokens:
            text = "\n\n".join(current).strip()
            chunks.append(text)
            tail = _tail_tokens(text, overlap_tokens)
            current = [tail] if tail else []
            current_tokens = count_tokens(tail) if tail else 0
        current.append(piece)
        current_tokens += piece_tokens
    if current:
        chunks.append("\n\n".join(current).strip())
    return [chunk for chunk in chunks if chunk]


def _fixed_pieces(text: str, max_tokens: int, overlap_tokens: int) -> list[str]:
    words = text.split()
    if not words:
        return []
    pieces: list[str] = []
    current: list[str] = []
    for word in words:
        candidate = " ".join(current + [word])
        if current and count_tokens(candidate) > max_tokens:
            text_piece = " ".join(current)
            pieces.append(text_piece)
            tail = _tail_tokens(text_piece, overlap_tokens)
            current = tail.split() if tail else []
        current.append(word)
    if current:
        pieces.append(" ".join(current))
    return pieces


def _header(document: Document) -> str:
    version = f" | version: {document.version}" if document.version else ""
    return f"[{document.doc_id}] {document.title} (type: {document.type}{version})\n"


def make_chunks(
    documents: Iterable[Document],
    *,
    strategy: str = "recursive",
    max_tokens: int = 160,
    overlap_tokens: int = 32,
) -> list[Chunk]:
    """Split documents into chunks and prepend metadata to every chunk.

    ``max_tokens`` is the budget for the whole chunk including the metadata header.
    """
    if strategy not in {"fixed", "recursive"}:
        raise ValueError(f"unknown chunking strategy: {strategy}")
    if max_tokens <= 0:
        raise ValueError("max_tokens must be positive")
    overlap_tokens = max(0, min(overlap_tokens, max_tokens // 2))

    chunks: list[Chunk] = []
    for document in documents:
        header = _header(document)
        body_budget = max(24, max_tokens - count_tokens(header))
        body = document.text.strip()
        if strategy == "fixed":
            pieces = _fixed_pieces(body, body_budget, overlap_tokens)
        else:
            pieces = _merge_pieces(
                _split_recursive(body, body_budget, _RECURSIVE_SEPARATORS),
                body_budget,
                overlap_tokens,
            )
        if not pieces:
            pieces = [""]
        for index, piece in enumerate(pieces):
            chunks.append(
                Chunk(
                    chunk_id=f"{document.doc_id}:{index:03d}",
                    doc_id=document.doc_id,
                    doc_type=document.type,
                    title=document.title,
                    version=document.version,
                    text=f"{header}\n{piece}".strip(),
                    body=piece,
                    chunk_index=index,
                )
            )
    return chunks


def corpus_stats(documents: list[Document]) -> dict:
    import statistics
    from collections import Counter

    lengths = sorted(len(document.text) for document in documents)
    types = Counter(document.type for document in documents)
    return {
        "n_documents": len(documents),
        "n_by_type": dict(sorted(types.items())),
        "chars_min": lengths[0] if lengths else 0,
        "chars_median": statistics.median(lengths) if lengths else 0,
        "chars_mean": round(statistics.mean(lengths), 1) if lengths else 0,
        "chars_p90": lengths[int(0.9 * len(lengths))] if lengths else 0,
        "chars_p95": lengths[int(0.95 * len(lengths))] if lengths else 0,
        "chars_max": lengths[-1] if lengths else 0,
    }


def chunk_stats(chunks: list[Chunk]) -> dict:
    import statistics
    from collections import Counter

    counts = [count_tokens(chunk.text) for chunk in chunks]
    per_doc = Counter(chunk.doc_id for chunk in chunks)
    return {
        "n_chunks": len(chunks),
        "chunks_per_doc_mean": round(len(chunks) / max(1, len(per_doc)), 2),
        "chunks_per_doc_max": max(per_doc.values()) if per_doc else 0,
        "tokens_min": min(counts) if counts else 0,
        "tokens_median": statistics.median(counts) if counts else 0,
        "tokens_mean": round(statistics.mean(counts), 1) if counts else 0,
        "tokens_p95": sorted(counts)[int(0.95 * len(counts))] if counts else 0,
        "tokens_max": max(counts) if counts else 0,
    }
