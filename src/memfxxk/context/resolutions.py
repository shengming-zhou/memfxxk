from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy import Connection, select
from sqlalchemy.dialects.postgresql import insert

from memfxxk.db import content_resolutions, documents, jobs, records, transaction
from memfxxk.documents import _resolve, _revision
from memfxxk.domain import RequestContext, Scope
from memfxxk.domain.documents import document_path
from memfxxk.domain.errors import ServiceError
from memfxxk.domain.resolutions import (
    ContentResolutions,
    ContentSection,
    ResolutionPair,
    ResolutionSource,
    ResolutionTarget,
    TextResolution,
)
from memfxxk.ids import derived_id
from memfxxk.ingest import NotFound
from memfxxk.jobs.queue import Job, enqueue, owned_claim
from memfxxk.llm.runner import TaskRunner
from memfxxk.m2 import blocks
from memfxxk.m2.amem import _sources
from memfxxk.m2.doc_chunks import sections
from memfxxk.manage.common import current_record, lock_user_operations
from memfxxk.prompts import resolution_summarize
from memfxxk.scope import visible_filter
from memfxxk.text import TokenCounter, make_token_counter

if TYPE_CHECKING:
    from memfxxk.service import BoundService, MemoryService

JOB = "refresh_resolutions"
TASK = "resolution_summarize"
_PAIRS = TypeAdapter(list[ResolutionPair])
_SENTENCE = re.compile(r"(?<=[。！？!?])|(?<=[.])\s+|\n+")
_INTRO = re.compile(r"\A(?: {0,3}#{1,6}[^\n]*\n\s*|(?:user|assistant):\s*)+", re.I)


def fit(text: str, limit: int, counter: TokenCounter) -> str:
    if counter.count(text) <= limit:
        return text.strip()
    low, high = 0, len(text)
    while low + 1 < high:
        mid = (low + high) // 2
        if counter.count(text[:mid]) <= limit:
            low = mid
        else:
            high = mid
    return text[:low].strip()


def limits(long: int) -> tuple[int, int]:
    summary = min(512, max(1, long * 17 // 100))
    return summary, min(24, summary)


def fallback(text: str, counter: TokenCounter) -> ResolutionPair:
    summary_limit, line_limit = limits(counter.count(text))
    prose = _INTRO.sub("", text.strip()).strip() or text.strip()
    first = _SENTENCE.split(prose, maxsplit=1)[0]
    summary, line = fit(prose, summary_limit, counter), fit(first, line_limit, counter)
    return ResolutionPair(
        summary=TextResolution(
            text=summary, tokens=counter.count(summary), method="extractive_fallback"
        ),
        line=TextResolution(text=line, tokens=counter.count(line), method="extractive_fallback"),
    )


def _model_pair(
    service: MemoryService,
    ctx: RequestContext,
    job: Job,
    text: str,
    sources: list[UUID],
    counter: TokenCounter,
) -> ResolutionPair:
    result = fallback(text, counter)
    # Opt in per task so a shared T16 model does not add a paid call to every write.
    if (
        TASK not in service.settings.llm.tasks
        or not service.settings.task_model(TASK).model
        or not sources
    ):
        return result
    count = counter.count(text)
    if count < 20 or count > service.settings.m2.compact_input_tokens:
        return result
    summary_limit, line_limit = limits(count)
    try:
        parsed = (
            TaskRunner.for_service(service, TASK)
            .complete(
                TASK,
                ctx,
                [
                    {"role": "system", "content": resolution_summarize.PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "original": text,
                                "summary_tokens": summary_limit,
                                "line_tokens": line_limit,
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                schema=resolution_summarize.SCHEMA,
                prompt_version=resolution_summarize.VERSION,
                source_event_ids=sources,
                job_id=job.id,
            )
            .parsed
        )
    except ServiceError:
        return result
    if parsed is None:
        return result
    summary = "\n".join(part.strip() for part in parsed.summary)
    line = parsed.line.strip()
    for level, value, copied, limit in (
        (
            "summary",
            summary,
            bool(parsed.summary) and all(x.strip() in text for x in parsed.summary),
            summary_limit,
        ),
        ("line", line, line in text, line_limit),
    ):
        if value and copied and counter.count(value) <= limit:
            setattr(
                result,
                level,
                TextResolution(text=value, tokens=counter.count(value), method="llm_extract"),
            )
    return result


@dataclass(frozen=True, slots=True)
class Snapshot:
    generation: int
    units: list[tuple[str, str]]
    source_event_ids: list[UUID]


def _snapshot(
    conn: Connection,
    ctx: RequestContext,
    kind: ResolutionSource,
    identifier: UUID,
    source_ids: list[UUID],
    *,
    lock: bool = False,
) -> Snapshot:
    if kind == "block":
        row = blocks._block(conn, ctx, identifier, lock=lock)
        block = blocks.view(conn, ctx, row)
        return Snapshot(block.revision, [(block.label, block.value)], block.source_event_ids)
    table = records if kind == "record" else documents
    stmt = select(table).where(visible_filter(table, ctx), table.c.id == identifier)
    if kind == "record":
        stmt = stmt.where(records.c.is_current, records.c.status == "active")
    row = conn.execute(stmt.with_for_update() if lock else stmt).mappings().one_or_none()
    if row is None:
        raise NotFound
    if kind == "record":
        return Snapshot(
            row["generation"],
            [(row["title"] or "", row["content"])],
            sorted(_sources(conn, ctx, row)),
        )
    revision = _revision(conn, row, None)
    return Snapshot(row["head_revision"], list(sections(revision["content"])), source_ids)


def queue_resolution(
    conn: Connection,
    scope: Scope,
    kind: ResolutionSource,
    identifier: UUID,
    generation: int,
    source_event_ids: list[UUID] | None = None,
) -> None:
    enqueue(
        conn,
        JOB,
        {
            "kind": kind,
            "id": str(identifier),
            "generation": generation,
            "scope": scope.model_dump(),
            "source_event_ids": list(map(str, source_event_ids or [])),
        },
        dedupe_key=f"resolution:{kind}:{identifier}:{generation}",
    )


def queue_embedded(service: MemoryService, batch: list[Job]) -> None:
    ids = {UUID(job.payload["record_id"]): job.payload["generation"] for job in batch}
    with transaction(service.engine) as conn:
        rows = conn.execute(
            select(
                records.c.id,
                records.c.generation,
                *(records.c[k] for k in Scope.model_fields),
            ).where(
                records.c.id.in_(ids),
                records.c.is_current,
                records.c.status == "active",
                records.c.type != "doc_chunk",
            )
        ).mappings()
        for row in rows:
            scope = Scope.model_validate(dict(row))
            if row["generation"] == ids[row["id"]]:
                queue_resolution(conn, scope, "record", row["id"], row["generation"])


def _identity(kind: ResolutionSource, identifier: UUID) -> UUID:
    return derived_id("content-resolutions", kind, str(identifier))


def read_pairs(
    conn: Connection,
    scope: Scope,
    kind: ResolutionSource,
    identifier: UUID,
    generation: int,
) -> list[ResolutionPair] | None:
    values = conn.scalar(
        select(content_resolutions.c.variants).where(
            visible_filter(content_resolutions, scope),
            content_resolutions.c.generation == generation,
            content_resolutions.c.id == _identity(kind, identifier),
        )
    )
    return _PAIRS.validate_python(values) if values is not None else None


def refresh(service: MemoryService, job: Job) -> None:
    kind, identifier = job.payload["kind"], UUID(job.payload["id"])
    scope = Scope(**job.payload["scope"])
    ctx = RequestContext(**scope.model_dump(), actor={"type": "system"})
    source_ids = [UUID(key) for key in job.payload["source_event_ids"]]
    try:
        with service.engine.connect() as conn:
            source = _snapshot(conn, ctx, kind, identifier, source_ids)
            if (
                source.generation != job.payload["generation"]
                or read_pairs(conn, ctx, kind, identifier, source.generation) is not None
            ):
                return
    except NotFound:
        return
    counter = make_token_counter(service.settings.token_counter)
    variants = [
        _model_pair(service, ctx, job, text, source.source_event_ids, counter).model_dump(
            mode="json"
        )
        for _, text in source.units
    ]
    try:
        with transaction(service.engine) as conn:
            lock_user_operations(conn, ctx)
            live = _snapshot(conn, ctx, kind, identifier, source_ids, lock=True)
            if (
                source != live
                or source.source_event_ids
                and not blocks.available_sources(conn, ctx, set(source.source_event_ids))
                or conn.scalar(select(jobs.c.id).where(owned_claim(job)).with_for_update()) is None
            ):
                return
            row = insert(content_resolutions).values(
                id=_identity(kind, identifier),
                **scope.model_dump(),
                **{f"{kind}_id": identifier},
                generation=source.generation,
                variants=variants,
                source_event_ids=source.source_event_ids,
            )
            conn.execute(
                row.on_conflict_do_update(
                    index_elements=[content_resolutions.c.id],
                    set_={
                        name: row.excluded[name]
                        for name in ("generation", "variants", "source_event_ids")
                    },
                )
            )
    except NotFound:
        return


def get(service: BoundService, target: ResolutionTarget) -> ContentResolutions:
    ctx = service.ctx
    counter = make_token_counter(service.settings.token_counter)
    with service.engine.connect() as conn:
        if target.kind == "document":
            assert target.path is not None
            row = _resolve(conn, ctx, document_path(target.path))
            identifier = display_id = row["id"]
        else:
            assert target.id is not None
            display_id = target.id
            identifier = (
                current_record(conn, ctx, target.id)["id"] if target.kind == "record" else target.id
            )
        source = _snapshot(conn, ctx, target.kind, identifier, [])
        pairs = read_pairs(conn, ctx, target.kind, identifier, source.generation)
    return ContentResolutions(
        id=display_id,
        generation=source.generation,
        sections=[
            ContentSection(
                index=index,
                heading=heading,
                full=TextResolution(
                    text=original, tokens=counter.count(original), method="original"
                ),
                **(pairs[index] if pairs else fallback(original, counter)).model_dump(),
            )
            for index, (heading, original) in enumerate(source.units)
        ],
    )


def forget_source(conn: Connection, org_id: str, event_id: UUID) -> None:
    conn.execute(
        content_resolutions.delete().where(
            content_resolutions.c.org_id == org_id,
            content_resolutions.c.source_event_ids.contains([event_id]),
        )
    )
