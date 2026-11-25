import re
from collections.abc import Iterator
from datetime import datetime
from uuid import UUID

from sqlalchemy import ColumnElement, Connection

from memfxxk.db import records
from memfxxk.domain import RequestContext, Scope
from memfxxk.ids import derived_id
from memfxxk.m2.storage import derived_record
from memfxxk.scope import visible_filter

_HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_MAX_CHARS = 2400


def sections(content: str) -> Iterator[tuple[str, str]]:
    headings: list[tuple[int, str]] = []
    lines: list[str] = []
    fence: str | None = None
    for line in content.splitlines():
        match = _FENCE.match(line)
        if match and fence is None:
            fence = match[1]
        elif (
            fence
            and line.lstrip().startswith(fence)
            and not line.strip()[len(fence) :].strip(fence[0])
        ):
            fence = None
        elif fence is None and (heading := _HEADING.match(line)):
            if lines:
                yield " / ".join(title for _, title in headings), "\n".join(lines)
            level = len(heading[1])
            headings = [(depth, title) for depth, title in headings if depth < level]
            headings.append((level, heading[2]))
            lines = []
        lines.append(line)
    if lines:
        yield " / ".join(title for _, title in headings), "\n".join(lines)


def chunk_filter(ctx: Scope, document_id: UUID) -> tuple[ColumnElement[bool], ...]:
    return (
        records.c.type == "doc_chunk",
        records.c.source_pos["document_id"].as_string() == str(document_id),
        visible_filter(records, ctx),
    )


def replace_chunks(
    conn: Connection,
    ctx: RequestContext,
    scope: Scope,
    *,
    document_id: UUID,
    path: str,
    revision: int,
    text: str,
    source_id: UUID,
    recorded_at: datetime,
    on_demand: bool,
) -> None:
    conn.execute(
        records.update()
        .where(
            *chunk_filter(ctx, document_id),
            records.c.is_current,
        )
        .values(is_current=False, generation=records.c.generation + 1)
    )
    if not on_demand:
        return
    for index, (heading, section) in enumerate(sections(text)):
        for offset in range(0, len(section), _MAX_CHARS):
            part = section[offset : offset + _MAX_CHARS].strip()
            if part:
                derived_record(
                    conn,
                    ctx,
                    scope,
                    memory_id=derived_id(
                        "doc-chunk", str(document_id), str(revision), str(index), str(offset)
                    ),
                    version=1,
                    kind="doc_chunk",
                    content=f"{path} · {heading}\n{part}",
                    sources=[source_id],
                    event_time=recorded_at,
                    source_pos={
                        "document_id": str(document_id),
                        "path": path,
                        "revision": revision,
                        "heading": heading,
                        "offset": offset,
                    },
                )
