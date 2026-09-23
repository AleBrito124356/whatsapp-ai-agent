"""Knowledge base loader and bilingual BM25 retriever.

Markdown files in ``kb/`` are split into sections by heading. Each section is
scored with Okapi BM25 over its body plus two small boosts: query terms that
appear in the section heading, and terms in the article's H1 title (carried
into every section of that article). Pure Python, no embeddings API, instant
and deterministic, which is what a small-business FAQ needs.

The knowledge base is written in Spanish, but customers also write in English.
Retrieval bridges the gap in two cheap, inspectable steps:

1. Normalisation: lowercase, accents removed, light ES/EN plural stemming
   ("precios" -> "precio", "tarjetas" -> "tarjeta", "cards" -> "card").
2. Query expansion from ``kb/_lexicon.json``, an editable bilingual glossary
   ("haircut" -> "corte cabello", "how much" -> "precio",
   "where" -> "direccion ubicacion"...). Expanded terms count a bit less than
   the customer's own words.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

_TOKEN_RE = re.compile(r"[a-z0-9]+")
# Very common words in ES/EN that carry little retrieval signal, including
# question words: "¿cuánto dura?" vs "¿cuánto cuesta?" differ in the verb.
_STOPWORDS = {
    "de", "la", "el", "en", "y", "a", "los", "las", "un", "una", "que", "es",
    "por", "para", "con", "del", "al", "se", "su", "sus", "lo", "como", "mas", "o",
    "me", "mi", "mis", "tu", "le", "les", "hay", "son", "esta", "este", "estan",
    "cual", "cuales", "cuanto", "cuanta", "cuantos", "donde", "cuando", "puedo",
    "tienen", "hacen", "ustedes", "usted", "si", "no", "ya", "muy", "todo", "todos",
    "the", "of", "and", "to", "in", "is", "for", "an", "on", "with", "at", "it",
    "this", "that", "you", "your", "we", "our", "do", "does", "how", "what",
    "where", "when", "which", "who", "why", "are", "can", "i", "my", "me", "there",
    "any", "much", "be", "have", "has", "if", "or", "by", "from", "about", "get",
    "hola", "hi", "hello", "please", "favor", "gracias", "thanks",
    # Price verbs: their meaning is carried by the lexicon ("cuesta" -> "precio"),
    # and on their own they match any sentence that mentions a price.
    "cuesta", "cuestan", "vale", "valen", "cobran",
}
_LEXICON_FILE = "_lexicon.json"

# Scoring weights (tuned on tests/data/faq_eval.json).
HEADING_BOOST = 1.2
DOC_TITLE_BOOST = 0.35
EXPANSION_WEIGHT = 0.7


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return text.lower()


def stem(token: str) -> str:
    """Light, symmetric ES/EN plural stripping (applied to queries and docs)."""
    if len(token) <= 3 or token.isdigit():
        return token
    for suffix, repl in (("ciones", "cion"), ("siones", "sion")):
        if token.endswith(suffix):
            return token[: -len(suffix)] + repl
    if token.endswith("es") and len(token) > 4 and token[-3] in "lrndj":
        return token[:-2]
    if token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _words(text: str) -> list[str]:
    return _TOKEN_RE.findall(normalize(text))


def _tokenize(text: str) -> list[str]:
    # Single letters ("a. m.") and bare numbers ("9:00", "$12") are noise for
    # retrieval and would inflate the length of table-heavy sections.
    return [stem(t) for t in _words(text) if t not in _STOPWORDS and len(t) > 1 and not t.isdigit()]


@dataclass
class Chunk:
    source: str
    title: str
    text: str
    tokens: list[str] = field(default_factory=list)
    doc_title: str = ""
    heading_tokens: frozenset = frozenset()
    doc_tokens: frozenset = frozenset()


class KnowledgeBase:
    def __init__(self, kb_dir: Path):
        self.kb_dir = Path(kb_dir)
        self.lexicon: dict[str, list[str]] = self._load_lexicon()
        self.chunks: list[Chunk] = self._load()
        self._build_index()

    # ------------------------------------------------------------- loading
    def _load_lexicon(self) -> dict[str, list[str]]:
        path = self.kb_dir / _LEXICON_FILE
        if not path.exists():
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
        lexicon: dict[str, list[str]] = {}
        for key, values in raw.items():
            if key.startswith("_"):
                continue  # comments
            # Keys are matched on stemmed words, so "mondays" finds "monday".
            lexicon[" ".join(stem(w) for w in _words(key))] = [v for value in values for v in _words(value)]
        return lexicon

    def _load(self) -> list[Chunk]:
        chunks: list[Chunk] = []
        for path in sorted(self.kb_dir.glob("*.md")):
            chunks.extend(self._split(path))
        return chunks

    @staticmethod
    def _split(path: Path) -> list[Chunk]:
        raw = path.read_text(encoding="utf-8")
        chunks: list[Chunk] = []
        doc_title = path.stem
        title = path.stem
        buffer: list[str] = []

        def flush() -> None:
            body_lines = [line for line in buffer if not line.startswith("#")]
            if not "\n".join(body_lines).strip():
                return  # a heading with nothing under it
            text = "\n".join(buffer).strip()
            chunks.append(
                Chunk(
                    source=path.name,
                    title=title,
                    text=text,
                    tokens=_tokenize("\n".join(body_lines)),
                    doc_title=doc_title,
                    # An article's intro already gets the H1 boost below; giving
                    # it the heading boost too would let a vague overview beat
                    # the specific section ("Dirección") that answers the question.
                    heading_tokens=frozenset() if title == doc_title else frozenset(_tokenize(title)),
                    doc_tokens=frozenset(_tokenize(doc_title)),
                )
            )

        for line in raw.splitlines():
            if line.startswith("#"):
                flush()
                buffer = []
                title = line.lstrip("#").strip()
                if line.startswith("# "):
                    doc_title = title
            buffer.append(line)
        flush()
        return chunks

    # --------------------------------------------------------------- index
    def _build_index(self) -> None:
        self.n_docs = len(self.chunks)
        self.doc_freq: Counter[str] = Counter()
        for chunk in self.chunks:
            for token in set(chunk.tokens) | chunk.heading_tokens | chunk.doc_tokens:
                self.doc_freq[token] += 1
        total_len = sum(len(c.tokens) for c in self.chunks)
        self.avg_len = (total_len / self.n_docs) if self.n_docs else 0.0
        self.idf: dict[str, float] = {
            token: math.log(1 + (self.n_docs - df + 0.5) / (df + 0.5))
            for token, df in self.doc_freq.items()
        }
        self._freqs = [Counter(c.tokens) for c in self.chunks]

    # -------------------------------------------------------------- query
    def expand(self, query: str) -> dict[str, float]:
        """Query terms with weights: the customer's words 1.0, lexicon terms less."""
        words = _words(query)
        weights: dict[str, float] = {}
        for word in words:
            if word not in _STOPWORDS and len(word) > 1 and not word.isdigit():
                weights[stem(word)] = 1.0
        text = " " + " ".join(stem(w) for w in words) + " "
        for phrase, expansions in self.lexicon.items():
            if f" {phrase} " in text:
                for term in expansions:
                    if term in _STOPWORDS:
                        continue
                    token = stem(term)
                    weights[token] = max(weights.get(token, 0.0), EXPANSION_WEIGHT)
        return weights

    # -------------------------------------------------------------- search
    def search(self, query: str, k: int = 4, k1: float = 1.5, b: float = 0.75) -> list[Chunk]:
        if not self.chunks:
            return []
        q = self.expand(query)
        if not q:
            return []
        scored: list[tuple[float, int, Chunk]] = []
        for i, chunk in enumerate(self.chunks):
            freqs = self._freqs[i]
            dl = len(chunk.tokens) or 1
            score = 0.0
            for token, weight in q.items():
                idf = self.idf.get(token)
                if idf is None:
                    continue
                tf = freqs.get(token)
                if tf:
                    denom = tf + k1 * (1 - b + b * dl / (self.avg_len or 1))
                    score += weight * idf * (tf * (k1 + 1)) / denom
                if token in chunk.heading_tokens:
                    score += weight * idf * HEADING_BOOST
                if token in chunk.doc_tokens:
                    score += weight * idf * DOC_TITLE_BOOST
            if score > 0:
                scored.append((score, -i, chunk))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [chunk for _, _, chunk in scored[:k]]

    def context(self, query: str, k: int = 4) -> str:
        parts = []
        for chunk in self.search(query, k=k):
            parts.append(f"### {chunk.title} ({chunk.source})\n{chunk.text}")
        return "\n\n".join(parts)
