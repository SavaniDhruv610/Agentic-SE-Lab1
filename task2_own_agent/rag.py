"""Task 2: a small RAG component over the style guide (numpy only, no frameworks).

    rag = StyleGuideRAG(style_guide_dir, embed_fn=llm.embed)
    chunks = rag.search("night mode colors", k=5)       # list[Chunk], best first
    text = rag.format_observation(chunks)               # <= 1,000 tokens of style guide text

Chunking: the documents are short (30-150 words), so normally one document = one chunk. Only a
document longer than `max_chunk_tokens` is split at its `##` headings. Every chunk repeats the
document id, title and status line, because a chunk without "DEPRECATED" in it would look like a
valid rule.

Try it without the agent:
    uv run python task2_own_agent/rag.py "which font sizes are allowed"
"""

from __future__ import annotations

import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from common.llm_client import count_tokens  # noqa: E402

STYLE_GUIDE_DIR = HERE / "style_guide"
CACHE_DIR = HERE / ".cache"
MAX_OBSERVATION_TOKENS = 1000


@dataclass
class Chunk:
    doc_id: str          # e.g. "SG-13"
    title: str
    status: str          # e.g. "ACTIVE (version 2)" or "DEPRECATED – replaced by SG-13"
    text: str            # what is embedded and shown to the model (starts with the header)
    score: float = 0.0

    @property
    def deprecated(self) -> bool:
        return "DEPRECATED" in self.status.upper()


def load_chunks(style_guide_dir: Path = STYLE_GUIDE_DIR, max_chunk_tokens: int = 300) -> list[Chunk]:
    chunks: list[Chunk] = []
    for path in sorted(Path(style_guide_dir).glob("*.md")):
        raw = path.read_text(encoding="utf-8").strip()
        doc_id = re.match(r"SG-\d+", path.name).group(0) if re.match(r"SG-\d+", path.name) else path.stem
        lines = raw.splitlines()
        title = re.sub(r"^#\s*(SG-\d+:\s*)?", "", lines[0]).strip() if lines else path.stem
        status_match = re.search(r"(?m)^Status:\s*(.+)$", raw)
        status = status_match.group(1).strip() if status_match else "unknown"
        header = f"[{doc_id}] {title} | Status: {status}"

        # body = document without its first heading and the Status/Category lines (they are in the header)
        body = "\n".join(line for line in lines[1:] if not re.match(r"^(Status|Category):", line)).strip()
        if count_tokens(f"{header}\n{body}") <= max_chunk_tokens:
            chunks.append(Chunk(doc_id, title, status, f"{header}\n{body}"))
            continue
        for section in re.split(r"(?m)^(?=## )", body):
            section = section.strip()
            if section:
                chunks.append(Chunk(doc_id, title, status, f"{header}\n{section}"))
    return chunks


class StyleGuideRAG:
    def __init__(self, style_guide_dir: Path = STYLE_GUIDE_DIR, embed_fn: Callable[[list[str]], np.ndarray] | None = None,
                 model_name: str = "embedding", use_cache: bool = True) -> None:
        """embed_fn: list of texts -> array (n, dim), e.g. `LLMClient.embed`."""
        if embed_fn is None:
            raise ValueError("embed_fn is required, e.g. StyleGuideRAG(embed_fn=llm.embed)")
        self.chunks = load_chunks(style_guide_dir)
        if not self.chunks:
            raise FileNotFoundError(f"no style guide documents found in {style_guide_dir}")
        self.embed_fn = embed_fn

        texts = [chunk.text for chunk in self.chunks]
        digest = hashlib.sha256("\n".join(texts).encode("utf-8")).hexdigest()[:12]
        cache_file = CACHE_DIR / f"style_guide_{re.sub(r'[^A-Za-z0-9_.-]', '_', model_name)}_{digest}.npy"
        if use_cache and cache_file.exists():
            matrix = np.load(cache_file)
        else:
            matrix = np.asarray(embed_fn(texts), dtype=np.float32)
            if use_cache:
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                np.save(cache_file, matrix)
        self.matrix = self._normalize(matrix)      # rows have length 1, so a dot product is the cosine similarity

    @staticmethod
    def _normalize(vectors: np.ndarray) -> np.ndarray:
        return vectors / np.maximum(np.linalg.norm(vectors, axis=-1, keepdims=True), 1e-12)

    def search(self, query: str, k: int = 5) -> list[Chunk]:
        """The k chunks most similar to the query (cosine similarity), best first."""
        query_vector = self._normalize(np.asarray(self.embed_fn([query]), dtype=np.float32)[0])
        similarities = self.matrix @ query_vector
        order = np.argsort(-similarities, kind="stable")[: max(1, k)]
        return [Chunk(c.doc_id, c.title, c.status, c.text, float(similarities[i]))
                for i in order for c in [self.chunks[i]]]

    @staticmethod
    def format_observation(chunks: list[Chunk], max_tokens: int = MAX_OBSERVATION_TOKENS) -> str:
        """Join chunks (best first) into one text of at most `max_tokens` tokens.

        A chunk that does not fit completely is dropped, never cut in the middle: half a rule is
        worse than no rule.
        """
        parts: list[str] = []
        for chunk in chunks:
            candidate = "\n\n---\n\n".join(parts + [chunk.text])
            if count_tokens(candidate) > max_tokens:
                break
            parts.append(chunk.text)
        return "\n\n---\n\n".join(parts)


if __name__ == "__main__":
    from common.llm_client import LLMClient

    llm = LLMClient(log_path=HERE / "logs" / "rag_demo.jsonl")
    rag = StyleGuideRAG(embed_fn=llm.embed, model_name=llm.embedding_model or "embedding")
    query = " ".join(sys.argv[1:]) or "night mode colors"
    results = rag.search(query, k=5)
    for chunk in results:
        print(f"{chunk.score:.3f}  {chunk.doc_id}  {chunk.title}  ({chunk.status})")
    observation = rag.format_observation(results)
    print(f"\n--- observation ({count_tokens(observation)} tokens) ---\n{observation}")
