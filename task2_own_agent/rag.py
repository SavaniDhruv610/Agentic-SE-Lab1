"""Retrieval-augmented generation over the UPB Agentic SE style guide.

The index is built from every Markdown document in ``style_guide/``.  Documents are
split into paragraph-sized chunks, each chunk carries the id of its source document
(e.g. ``SG-13``), and retrieval uses cosine similarity over embeddings from the
gateway.  A BM25 fallback is available for offline development, but the normal mode
uses vectors.

The public API required by the task is:

    rag = StyleGuideRAG(style_dir, llm)
    rag.build()
    chunks = rag.search("night mode colors", k=4)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np

try:
    from common.llm_client import count_tokens
except ImportError:  # allow running directly from this folder
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from common.llm_client import count_tokens


STATUS_RE = re.compile(r"^\s*Status:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
CATEGORY_RE = re.compile(r"^\s*Category:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
TITLE_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
DOC_ID_RE = re.compile(r"^(SG-\d+)")


@dataclass
class Chunk:
    """A retrievable piece of one style-guide document."""

    text: str
    doc_id: str
    title: str
    status: str = "ACTIVE"
    category: str = ""
    source_path: str = ""
    chunk_index: int = 0

    @property
    def deprecated(self) -> bool:
        return self.status.upper().startswith("DEPRECATED")

    def header(self) -> str:
        return f"[{self.doc_id}] {self.title} — {self.status}"


@dataclass
class _Document:
    doc_id: str
    title: str
    status: str
    category: str
    text: str
    path: str


class StyleGuideRAG:
    """In-memory style-guide index with cosine-similarity retrieval."""

    def __init__(
        self,
        style_dir: str | Path,
        llm,
        *,
        max_chunk_tokens: int = 320,
        backend: str = "auto",
        deprecated_penalty: float = 0.25,
    ) -> None:
        self.style_dir = Path(style_dir)
        self.llm = llm
        self.max_chunk_tokens = max_chunk_tokens
        self.backend = backend  # "auto", "embeddings", or "bm25"
        self.deprecated_penalty = deprecated_penalty
        self.documents: list[_Document] = []
        self.chunks: list[Chunk] = []
        self._vectors: np.ndarray | None = None
        self._bm25 = None
        self._active_backend: str | None = None

    # ---------------------------------------------------------------- loading

    def load_documents(self) -> list[_Document]:
        """Read all Markdown files from the style-guide directory."""
        self.documents = []
        for path in sorted(self.style_dir.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            match = DOC_ID_RE.match(path.name)
            doc_id = match.group(1) if match else path.stem
            title_match = TITLE_RE.search(text)
            title = title_match.group(1).strip() if title_match else path.stem
            status_match = STATUS_RE.search(text)
            status = status_match.group(1).strip() if status_match else "ACTIVE"
            category_match = CATEGORY_RE.search(text)
            category = category_match.group(1).strip() if category_match else ""
            self.documents.append(
                _Document(doc_id, title, status, category, text, path.name)
            )
        return self.documents

    def split_documents(self) -> list[Chunk]:
        """Split loaded documents into chunks, keeping every document's metadata."""
        chunks: list[Chunk] = []
        for doc in self.documents:
            for index, text in enumerate(self._split_document(doc.text)):
                chunks.append(
                    Chunk(
                        text=text,
                        doc_id=doc.doc_id,
                        title=doc.title,
                        status=doc.status,
                        category=doc.category,
                        source_path=doc.path,
                        chunk_index=index,
                    )
                )
        self.chunks = chunks
        return chunks

    def _split_document(self, text: str) -> list[str]:
        """Heading-aware paragraph chunking with a token budget per chunk."""
        title_match = TITLE_RE.search(text)
        title = title_match.group(1).strip() if title_match else ""
        status_match = STATUS_RE.search(text)
        status = status_match.group(1).strip() if status_match else "ACTIVE"
        category_match = CATEGORY_RE.search(text)
        category = category_match.group(1).strip() if category_match else ""

        # Remove the metadata block from the body; it is prepended as a header.
        body_lines = []
        for line in text.splitlines():
            if TITLE_RE.match(line) or STATUS_RE.match(line) or CATEGORY_RE.match(line):
                continue
            body_lines.append(line)
        body = "\n".join(body_lines).strip()

        blocks = [b.strip() for b in re.split(r"\n\s*\n", body) if b.strip()]
        header = f"{title}\nStatus: {status}\nCategory: {category}\n\n"
        header_tokens = count_tokens(header)

        pieces: list[str] = []
        current: list[str] = []
        current_tokens = header_tokens
        for block in blocks:
            block_parts = self._split_long_block(block, max(80, self.max_chunk_tokens - header_tokens))
            for part in block_parts:
                part_tokens = count_tokens(part)
                if current and current_tokens + part_tokens > self.max_chunk_tokens:
                    pieces.append(header + "\n\n".join(current))
                    current = [part]
                    current_tokens = header_tokens + part_tokens
                else:
                    current.append(part)
                    current_tokens += part_tokens
        if current:
            pieces.append(header + "\n\n".join(current))
        return pieces or [header.strip()]

    def _split_long_block(self, block: str, max_tokens: int) -> list[str]:
        if count_tokens(block) <= max_tokens:
            return [block]
        # Split oversized blocks (usually code examples) on line boundaries.
        parts: list[str] = []
        current: list[str] = []
        for line in block.splitlines():
            candidate = "\n".join(current + [line])
            if current and count_tokens(candidate) > max_tokens:
                parts.append("\n".join(current))
                current = [line]
            else:
                current.append(line)
        if current:
            parts.append("\n".join(current))
        return parts

    # --------------------------------------------------------------- indexing

    def build(self) -> "StyleGuideRAG":
        """Load, split and embed (or BM25-index) all style-guide chunks."""
        self.load_documents()
        self.split_documents()
        if not self.chunks:
            raise RuntimeError(f"No style-guide documents found in {self.style_dir}")

        if self.backend == "bm25":
            self._build_bm25()
            return self

        try:
            vectors = self.llm.embed([c.text for c in self.chunks])
            vectors = np.asarray(vectors, dtype=np.float32)
            if vectors.ndim != 2 or vectors.shape[0] != len(self.chunks):
                raise ValueError("embedding backend returned an unexpected shape")
            norms = np.linalg.norm(vectors, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            self._vectors = vectors / norms
            self._active_backend = "embeddings"
        except Exception:
            if self.backend == "embeddings":
                raise
            self._build_bm25()
        return self

    def _build_bm25(self) -> None:
        from rank_bm25 import BM25Okapi

        tokenized = [self._tokenize(c.text) for c in self.chunks]
        self._bm25 = BM25Okapi(tokenized)
        self._active_backend = "bm25"

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        return re.findall(r"[a-z0-9#]+", text.lower())

    @property
    def active_backend(self) -> str | None:
        return self._active_backend

    # -------------------------------------------------------------- retrieval

    def search(self, query: str, k: int = 4) -> list[Chunk]:
        """Return the ``k`` most similar chunks for a query."""
        if not self.chunks:
            self.build()
        query = (query or "").strip()
        if not query:
            return []
        k = max(1, int(k))

        if self._active_backend == "embeddings" and self._vectors is not None:
            query_vec = np.asarray(self.llm.embed([query]), dtype=np.float32)[0]
            norm = np.linalg.norm(query_vec)
            if norm:
                query_vec = query_vec / norm
            scores = self._vectors @ query_vec
        elif self._bm25 is not None:
            scores = np.asarray(self._bm25.get_scores(self._tokenize(query)), dtype=np.float32)
        else:
            raise RuntimeError("Index is not built.")

        scores = scores.astype(np.float64, copy=True)
        penalty = self.deprecated_penalty * max(float(scores.max()) if scores.size else 0.0, 1e-9)
        for i, chunk in enumerate(self.chunks):
            if chunk.deprecated:
                scores[i] -= penalty

        order = np.argsort(-scores)[:k]
        return [self.chunks[int(i)] for i in order]


def format_search_results(chunks: Iterable[Chunk], max_tokens: int = 1000) -> str:
    """Format chunks for an observation, enforcing the style-guide token budget.

    The budget applies to the whole observation, including the short footer, so the
    result is always at most ``max_tokens`` tokens even when counting the footer.
    """
    footer_template = "\n\n[style-guide context: {used}/{total} tokens]"
    reserve = count_tokens(footer_template.format(used=max_tokens, total=max_tokens))
    budget = max(1, max_tokens - reserve)

    selected: list[str] = []
    used = 0
    for chunk in chunks:
        piece = chunk.text.strip()
        if chunk.doc_id not in piece:
            piece = f"[{chunk.doc_id}] {piece}"
        tokens = count_tokens(piece)
        if selected and used + tokens > budget:
            continue
        if tokens > budget and not selected:
            # Extremely defensive: truncate a single oversized chunk by words.
            words = piece.split()
            while words and count_tokens(" ".join(words)) > budget:
                words.pop()
            piece = " ".join(words) + "\n[... truncated ...]"
            tokens = count_tokens(piece)
        selected.append(piece)
        used += tokens
        if used >= budget:
            break
    body = "\n\n---\n\n".join(selected) if selected else "No matching style-guide chunks found."
    footer = footer_template.format(used=used, total=max_tokens)
    result = body + footer
    if count_tokens(result) > max_tokens:
        # Keep the hard guarantee even for unusual tokenizers.
        while selected and count_tokens(result) > max_tokens:
            selected.pop()
            used = sum(count_tokens(p) for p in selected)
            body = "\n\n---\n\n".join(selected) if selected else "No matching style-guide chunks found."
            result = body + footer_template.format(used=used, total=max_tokens)
    return result
