"""Markdown 感知的文档分段，提供稳定的字符偏移和标题路径。"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import re


@dataclass(frozen=True)
class Chunk:
    id: str
    start: int
    end: int
    headings: tuple[str, ...]
    text: str

    def to_dict(self) -> dict:
        return asdict(self)


def split_document(text: str, *, chunk_size: int = 12000, overlap: int = 1200) -> list[Chunk]:
    """按标题/段落切分；超长段落按句子和字符窗口切分。"""
    if not text:
        return []
    blocks: list[tuple[int, str, tuple[str, ...]]] = []
    headings: list[str] = []
    pos = 0
    for raw in re.split(r"(\n\s*\n)", text):
        if not raw:
            continue
        start = pos
        pos += len(raw)
        m = re.match(r"\s{0,3}#{1,6}\s+(.+?)\s*$", raw.strip())
        if m:
            level = len(raw.lstrip().split(" ", 1)[0])
            headings = headings[: max(0, level - 1)] + [m.group(1).strip()]
        elif raw.strip():
            blocks.append((start, raw.strip(), tuple(headings)))

    chunks: list[Chunk] = []
    current: list[str] = []
    current_start = 0
    current_headings: tuple[str, ...] = ()
    current_len = 0

    def flush() -> None:
        nonlocal current, current_start, current_headings, current_len
        if not current:
            return
        value = "\n\n".join(current).strip()
        chunks.append(Chunk(f"chunk-{len(chunks):04d}", current_start, current_start + len(value), current_headings, value))
        current, current_len = [], 0

    for start, block, block_headings in blocks:
        pieces = [block]
        if len(block) > chunk_size:
            pieces = [p.strip() for p in re.split(r"(?<=[。！？.!?])\s+", block) if p.strip()]
        for piece in pieces:
            if current and current_len + len(piece) + 2 > chunk_size:
                previous = "\n\n".join(current)
                flush()
                tail = previous[-overlap:] if overlap else ""
                if tail:
                    current = [tail]
                    current_len = len(tail)
                    current_start = max(0, start - len(tail))
            if not current:
                current_start = start
                current_headings = block_headings
            current.append(piece)
            current_len += len(piece) + 2
    flush()
    return chunks


def locate(chunks: list[Chunk], query: str, *, top_k: int = 3) -> list[Chunk]:
    terms = {t.lower() for t in re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]{2,}", query or "")}
    scored = []
    for chunk in chunks:
        hay = (" ".join(chunk.headings) + " " + chunk.text).lower()
        score = sum(1 for term in terms if term in hay)
        scored.append((score, chunk.start, chunk))
    return [c for score, _, c in sorted(scored, key=lambda x: (-x[0], x[1]))[:top_k] if score > 0] or chunks[:top_k]

