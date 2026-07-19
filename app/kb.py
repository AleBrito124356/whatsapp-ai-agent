"""Knowledge base loader and BM25 retriever.

Markdown files in ``kb/`` are split into sections by heading. A query is scored
against every section with Okapi BM25 (pure Python, no embeddings API needed),
and the top sections are handed to the LLM as grounding context.

This keeps retrieval deterministic, offline-friendly and instant, which is what
you want for a small business FAQ that fits comfortably in memory.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

_TOKEN_RE = re.compile(r"[a-z0-9]+")
# Very common words in ES/EN that carry little retrieval signal.
_STOPWORDS = {
    "de", "la", "el", "en", "y", "a", "los", "las", "un", "una", "que", "es",
    "por", "para", "con", "del", "al", "se", "su", "lo", "como", "mas", "o",
    "the", "of", "and", "to", "in", "is", "for", "a", "an", "on", "with", "at",
    "it", "this", "that", "you", "your", "we", "our", "do", "how", "what",
}


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return text.lower()


def _tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(_normalize(text)) if t not in _STOPWORDS]


@dataclass
class Chunk:
    source: str
    title: str
    text: str
    tokens: list[str] = field(default_factory=list)


class KnowledgeBase:
    def __init__(self, kb_dir: Path):
        self.kb_dir = Path(kb_dir)
        self.chunks: list[Chunk] = self._load()
        self._build_index()

    # ------------------------------------------------------------- loading
    def _load(self) -> list[Chunk]:
        chunks: list[Chunk] = []
        for path in sorted(self.kb_dir.glob("*.md")):
            chunks.extend(self._split(path))
        return chunks

    @staticmethod
    def _split(path: Path) -> list[Chunk]:
        raw = path.read_text(encoding="utf-8")
        chunks: list[Chunk] = []
        title = path.stem
        buffer: list[str] = []

        def flush() -> None:
            body = "\n".join(buffer).strip()
            if body:
                chunks.append(
                    Chunk(
                        source=path.name,
                        title=title,
                        text=body,
                        tokens=_tokenize(title + " " + body),
                    )
                )

        for line in raw.splitlines():
            if line.startswith("#"):
                flush()
                buffer = []
                title = line.lstrip("#").strip()
            buffer.append(line)
        flush()
        return chunks

    # --------------------------------------------------------------- index
    def _build_index(self) -> None:
        self.n_docs = len(self.chunks)
        self.doc_freq: Counter[str] = Counter()
        for chunk in self.chunks:
            for token in set(chunk.tokens):
                self.doc_freq[token] += 1
        total_len = sum(len(c.tokens) for c in self.chunks)
        self.avg_len = (total_len / self.n_docs) if self.n_docs else 0.0
        self.idf: dict[str, float] = {}
        for token, df in self.doc_freq.items():
            # Okapi BM25 idf, floored at a small positive value.
            self.idf[token] = math.log(1 + (self.n_docs - df + 0.5) / (df + 0.5))

    # -------------------------------------------------------------- search
    def search(self, query: str, k: int = 4, k1: float = 1.5, b: float = 0.75) -> list[Chunk]:
        if not self.chunks:
            return []
        q_tokens = _tokenize(query)
        if not q_tokens:
            return []
        scored: list[tuple[float, Chunk]] = []
        for chunk in self.chunks:
            freqs = Counter(chunk.tokens)
            dl = len(chunk.tokens) or 1
            score = 0.0
            for token in q_tokens:
                tf = freqs.get(token)
                if not tf:
                    continue
                idf = self.idf.get(token, 0.0)
                denom = tf + k1 * (1 - b + b * dl / (self.avg_len or 1))
                score += idf * (tf * (k1 + 1)) / denom
            if score > 0:
                scored.append((score, chunk))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [chunk for _, chunk in scored[:k]]

    def context(self, query: str, k: int = 4) -> str:
        parts = []
        for chunk in self.search(query, k=k):
            parts.append(f"### {chunk.title} ({chunk.source})\n{chunk.text}")
        return "\n\n".join(parts)
