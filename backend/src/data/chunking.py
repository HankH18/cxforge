"""KB markdown parsing and chunking.

Each ``fixtures/kb/<slug>.md`` file is YAML front matter (``slug``, ``title``,
``category``, optional ``keywords``) delimited by ``---``, then a markdown
body (per the fixture contract). ``chunk_text`` splits the body into
overlapping word windows so a fact sitting near a chunk boundary still
appears whole in at least one chunk.

``keywords`` is a curated list of the natural, often colloquial, phrasings a
real customer would type for that doc's topic ("how long", "money back",
"not a bot"). It exists purely to widen the vocabulary available to the
embedder at index time (see ``data.seed._seed_kb``) — real support KBs are
routinely tagged this way precisely because customer phrasing rarely echoes
a doc's title or prose. It is never shown to a requester and never stored in
``kb_chunks.text``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from data.models import KBCategory


@dataclass(frozen=True)
class KBDoc:
    slug: str
    title: str
    category: KBCategory
    body: str
    keywords: tuple[str, ...] = field(default_factory=tuple)


def parse_kb_doc(path: Path) -> KBDoc:
    """Parse one KB markdown file into its front matter and body."""
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---"):
        raise ValueError(f"{path}: missing YAML front matter (must start with '---')")
    parts = raw.split("---", 2)
    if len(parts) < 3:
        raise ValueError(f"{path}: malformed front matter (need opening and closing '---')")
    _, front_matter_raw, body = parts
    front_matter = yaml.safe_load(front_matter_raw) or {}
    for required_field in ("slug", "title", "category"):
        if required_field not in front_matter:
            raise ValueError(f"{path}: front matter missing required field {required_field!r}")
    raw_keywords = front_matter.get("keywords") or []
    if not isinstance(raw_keywords, list) or not all(isinstance(k, str) for k in raw_keywords):
        raise ValueError(f"{path}: 'keywords' front matter must be a list of strings")
    return KBDoc(
        slug=front_matter["slug"],
        title=front_matter["title"],
        category=front_matter["category"],
        body=body.strip(),
        keywords=tuple(raw_keywords),
    )


def chunk_text(text: str, *, chunk_size: int = 180, overlap: int = 40) -> list[str]:
    """Split ``text`` into overlapping word-windowed chunks.

    Word-count windows (rather than raw character slicing) keep chunks
    readable and avoid splitting mid-word; the overlap means a sentence
    straddling a window boundary is still whole in the following chunk.
    """
    if overlap >= chunk_size:
        raise ValueError("overlap must be smaller than chunk_size")
    words = text.split()
    if not words:
        return []

    step = chunk_size - overlap
    chunks: list[str] = []
    start = 0
    while start < len(words):
        chunks.append(" ".join(words[start : start + chunk_size]))
        if start + chunk_size >= len(words):
            break
        start += step
    return chunks
