"""Split saved minutes (Markdown) into passages for embedding (REQ-58).

Passages never cross a heading, so each one belongs to exactly one section ("決議事項", ...),
which is kept alongside it. Long sections are packed paragraph by paragraph up to ``max_chars``;
a paragraph that alone exceeds it is cut with ``overlap`` characters repeated across the cut so a
sentence split in two can still be found. 400 characters of Chinese stays under the embedding
model's 512-token input limit together with the title/section prefix.
"""
import re
from dataclasses import dataclass

_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")


@dataclass
class Chunk:
    index: int
    section: str | None
    text: str


def split_sections(markdown: str) -> list[tuple[str | None, str]]:
    """(heading, body) pairs in document order; text before the first heading has heading None."""
    sections, heading, lines = [], None, []
    for line in markdown.splitlines():
        match = _HEADING.match(line)
        if match:
            sections.append((heading, "\n".join(lines)))
            heading, lines = match.group(2).strip(), []
        else:
            lines.append(line)
    sections.append((heading, "\n".join(lines)))
    return [(h, body.strip()) for h, body in sections if body.strip()]


def _cut(text: str, max_chars: int, overlap: int) -> list[str]:
    step = max(1, max_chars - overlap)
    return [text[i:i + max_chars] for i in range(0, max(1, len(text) - overlap), step)]


def chunk_markdown(markdown: str, *, max_chars: int = 400, overlap: int = 60) -> list[Chunk]:
    if overlap >= max_chars:
        raise ValueError("overlap must be smaller than max_chars")
    chunks: list[Chunk] = []
    for section, body in split_sections(markdown):
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
        current = ""
        for para in paragraphs:
            if len(para) > max_chars:
                if current:
                    chunks.append(Chunk(len(chunks), section, current))
                    current = ""
                chunks.extend(Chunk(len(chunks) + i, section, piece)
                              for i, piece in enumerate(_cut(para, max_chars, overlap)))
            elif current and len(current) + 2 + len(para) > max_chars:
                chunks.append(Chunk(len(chunks), section, current))
                current = para
            else:
                current = f"{current}\n\n{para}" if current else para
        if current:
            chunks.append(Chunk(len(chunks), section, current))
    return chunks
