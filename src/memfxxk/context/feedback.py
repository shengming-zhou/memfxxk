from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy import Connection, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import RowMapping

from memfxxk.db import (
    allocation_changes,
    allocation_states,
    context_bundles,
    document_revisions,
    documents,
    events,
    records,
    transaction,
)
from memfxxk.domain import Scope
from memfxxk.domain.context import AdaptiveSettings, ContextReference, ContextSection
from memfxxk.domain.errors import ServiceError
from memfxxk.domain.feedback import (
    AllocationChange,
    AllocationState,
    FeedbackObservation,
    FeedbackReference,
    FeedbackReport,
    LearningSignal,
)
from memfxxk.ids import derived_id
from memfxxk.ingest import NotFound, get_event
from memfxxk.m2 import blocks
from memfxxk.m2.doc_chunks import sections
from memfxxk.manage.common import lock_user_operations
from memfxxk.scope import visible_filter
from memfxxk.text import MixedTokenizer

if TYPE_CHECKING:
    from memfxxk.service import BoundService

_SECTIONS = TypeAdapter(list[ContextSection])
_SIGNALS = TypeAdapter(list[LearningSignal])
_TARGET = {"retrieved": 1.5, "unused": 0.7, "conflict": 1.7, "correction": 1.6}
_MEMORY_OUTPUT = re.compile(r"^(?:\[\d+\] |before: |after: )?id=([0-9a-f-]{36})(?: |$)", re.M)
_DOC_OUTPUT = re.compile(r"\Adoc:([^\s]+) revision=(\d+)\n")


def step(
    state: AllocationState, signals: Sequence[LearningSignal], base: AdaptiveSettings
) -> AllocationState:
    values = defaultdict(list)
    for signal in signals:
        values[signal.priority].append(_TARGET[signal.signal])
    multipliers = state.multipliers.copy()
    for priority, intentions in values.items():
        old = multipliers.get(priority, 1.0)
        goal = sum(intentions) / len(intentions)
        delta = min(0.035, max(-0.035, 0.08 * (goal - old)))
        multipliers[priority] = round(min(1.5, max(0.65, old + delta)), 8)
    return AllocationState(
        version=state.version + 1,
        multipliers=multipliers,
        decay=base.decay,
        expand_items=base.expand_items,
    )


def replay(initial: AllocationState, changes: Iterable[AllocationChange]) -> AllocationState:
    state = initial
    for change in changes:
        base = AdaptiveSettings(decay=change.base_decay, expand_items=change.base_expand_items)
        state = (
            AllocationState(
                version=state.version + 1, decay=base.decay, expand_items=base.expand_items
            )
            if change.reset
            else step(state, change.signals, base)
        )
    return state


def _state_id(scope: Scope) -> UUID:
    if scope.project_id is None:
        raise ServiceError(422, "project_required", "Select a project to track allocator feedback")
    return derived_id(
        "allocation-state", Scope.model_validate(scope.model_dump()).model_dump_json()
    )


def _empty(service: BoundService) -> AllocationState:
    base = service.settings.context.adaptive
    return AllocationState(decay=base.decay, expand_items=base.expand_items)


def state(conn: Connection, service: BoundService) -> AllocationState:
    row = conn.execute(
        select(allocation_states.c.version, allocation_states.c.settings).where(
            visible_filter(allocation_states, service.ctx),
            allocation_states.c.id == _state_id(service.ctx),
        )
    ).one_or_none()
    return AllocationState(version=row.version, **row.settings) if row else _empty(service)


def read_state(service: BoundService) -> AllocationState:
    with service.engine.connect() as conn:
        return state(conn, service)


def history(service: BoundService, after: int, limit: int) -> list[AllocationChange]:
    with service.engine.connect() as conn:
        rows = conn.execute(
            select(allocation_changes)
            .where(
                visible_filter(allocation_changes, service.ctx),
                allocation_changes.c.state_id == _state_id(service.ctx),
                allocation_changes.c.version > after,
            )
            .order_by(allocation_changes.c.version)
            .limit(limit)
        ).mappings()
        return [AllocationChange.model_validate(dict(row)) for row in rows]


def _write(
    conn: Connection,
    service: BoundService,
    new: AllocationState,
    *,
    signals: list[LearningSignal],
    reset: bool = False,
    bundle_id: UUID | None = None,
) -> AllocationState:
    scope = service.ctx.model_dump(exclude={"actor"})
    state_id = _state_id(service.ctx)
    row = insert(allocation_states).values(
        id=state_id, **scope, version=new.version, settings=new.model_dump(exclude={"version"})
    )
    conn.execute(
        row.on_conflict_do_update(
            index_elements=[allocation_states.c.id],
            set_={"settings": row.excluded.settings, "version": row.excluded.version},
        )
    )
    conn.execute(
        allocation_changes.insert().values(
            **scope,
            state_id=state_id,
            version=new.version,
            bundle_id=bundle_id,
            signals=_SIGNALS.dump_python(signals, mode="json"),
            base_decay=new.decay,
            base_expand_items=new.expand_items,
            reset=reset,
        )
    )
    return new


def reset(service: BoundService) -> AllocationState:
    service.require_write()
    with transaction(service.engine) as conn:
        lock_user_operations(conn, service.ctx)
        emptied = _empty(service)
        emptied.version = state(conn, service).version + 1
        return _write(conn, service, emptied, signals=[], reset=True)


def reference(item: ContextReference) -> FeedbackReference:
    return FeedbackReference(kind=item.kind, ref_id=item.ref_id, section_index=item.section_index)


def _matching_reference(
    item: ContextReference, observation: FeedbackObservation
) -> LearningSignal | None:
    if item.effective_priority is None:
        return None
    if observation.signal in ("retrieved", "conflict") and item.decision not in (
        "truncated",
        "dropped",
    ):
        return None
    if observation.signal == "unused" and (
        item.decision == "dropped" or item.resolution not in ("summary", "full")
    ):
        return None
    if item.decision_reason in (
        "source_unavailable",
        "duplicate_candidate",
        "covered_by_document_section",
        "host_history_overlap",
        "lesson_requires_review",
    ):
        return None
    return LearningSignal(
        signal=observation.signal, target=observation.target, priority=item.effective_priority
    )


def _document(conn: Connection, scope: Scope, item: ContextReference) -> tuple[str, str] | None:
    row = conn.execute(
        select(documents.c.path, document_revisions.c.content)
        .join(document_revisions, document_revisions.c.document_id == documents.c.id)
        .where(
            visible_filter(documents, scope),
            documents.c.id == UUID(item.ref_id),
            document_revisions.c.revision == item.revision,
        )
    ).one_or_none()
    if row is None:
        return None
    pieces = list(sections(row.content))
    index = item.section_index
    return row.path, pieces[index][1] if index is not None and index < len(pieces) else ""


def _content(conn: Connection, service: BoundService, item: ContextReference) -> str:
    ctx = service.ctx
    if item.kind == "document":
        document = _document(conn, ctx, item)
        return document[1] if document else ""
    if item.kind == "memory":
        return (
            conn.scalar(
                select(records.c.content).where(
                    visible_filter(records, ctx),
                    records.c.memory_id == UUID(item.ref_id),
                    records.c.version == item.version,
                    records.c.status == "active",
                )
            )
            or ""
        )
    if item.kind == "block":
        try:
            return blocks.view(conn, ctx, blocks._block(conn, ctx, UUID(item.ref_id))).value
        except NotFound:
            return ""
    if item.kind == "event":
        try:
            event = get_event(conn, ctx, UUID(item.ref_id))
        except NotFound:
            return ""
        return event.content or ""
    return ""


def _phrases(text: str, tokenizer: MixedTokenizer) -> set[tuple[str, ...]]:
    terms = tokenizer.tokenize(text[:32_000])
    return set(zip(terms, terms[1:], terms[2:]))


def _detect(
    conn: Connection,
    service: BoundService,
    bundle: RowMapping,
    assistant_id: UUID,
    items: list[ContextReference],
) -> list[FeedbackObservation]:
    assistant = get_event(conn, service.ctx, assistant_id)
    if (
        assistant.kind != "message"
        or assistant.role != "assistant"
        or assistant.redacted
        or not assistant.session_id
        or not assistant.turn_id
        or assistant.recorded_at < bundle["created_at"]
        or bundle["session_id"] not in (None, assistant.session_id)
    ):
        raise NotFound
    outputs = conn.scalars(
        select(events.c.payload).where(
            visible_filter(events, service.ctx),
            events.c.session_id == assistant.session_id,
            events.c.turn_id == assistant.turn_id,
            events.c.session_seq < assistant.session_seq,
            events.c.recorded_at >= bundle["created_at"],
            events.c.kind == "tool_result",
            ~events.c.redacted,
        )
    )
    memory_ids: set[str] = set()
    document_ids: set[tuple[str, int]] = set()
    for output in outputs:
        body = output.get("output")
        if output.get("outcome") != "success" or not isinstance(body, str):
            continue
        if output.get("name") in ("memory_get", "memory_search"):
            memory_ids.update(_MEMORY_OUTPUT.findall(body))
            if match := _DOC_OUTPUT.search(body):
                document_ids.add((match[1], int(match[2])))
    answer = assistant.content or ""
    tokenizer = MixedTokenizer()
    phrases = _phrases(answer, tokenizer)
    detected = []
    for item in items:
        target = reference(item)
        fetched = item.kind == "memory" and item.ref_id in memory_ids
        if item.kind == "document" and document_ids and (doc := _document(conn, service.ctx, item)):
            fetched = (doc[0], item.revision) in document_ids
        if fetched:
            detected.append(FeedbackObservation(signal="retrieved", target=target))
        elif (
            answer
            and item.decision != "dropped"
            and item.resolution in ("summary", "full")
            and item.ref_id not in answer
        ):
            source = _content(conn, service, item)
            if source and not (phrases & _phrases(source, tokenizer)):
                detected.append(FeedbackObservation(signal="unused", target=target))
    return detected


def report(service: BoundService, request: FeedbackReport) -> AllocationState:
    service.require_write()
    with transaction(service.engine) as conn:
        lock_user_operations(conn, service.ctx)
        before = state(conn, service)
        bundle = (
            conn.execute(
                select(context_bundles).where(
                    visible_filter(context_bundles, service.ctx),
                    context_bundles.c.id == request.bundle_id,
                    context_bundles.c.project_id == service.ctx.project_id,
                )
            )
            .mappings()
            .one_or_none()
        )
        if bundle is None:
            raise NotFound
        if bundle["budget"].get("allocator") != "adaptive":
            raise ServiceError(422, "adaptive_bundle_required", "Select an adaptive context bundle")
        sections_ = _SECTIONS.validate_python(bundle["items"])
        items = [item for section in sections_ for item in (*section.items, *section.dropped)]
        by_identity = {}
        for item in items:
            key = reference(item)
            if key not in by_identity or by_identity[key].decision == "dropped":
                by_identity[key] = item
        requested = request.observations
        if any(entry.target not in by_identity for entry in requested):
            raise NotFound
        if request.assistant_event_id is not None:
            requested = [
                *requested,
                *_detect(
                    conn, service, bundle, request.assistant_event_id, list(by_identity.values())
                ),
            ]
        previous = conn.scalars(
            select(allocation_changes.c.signals).where(
                visible_filter(allocation_changes, service.ctx),
                allocation_changes.c.state_id == _state_id(service.ctx),
                allocation_changes.c.bundle_id == request.bundle_id,
            )
        )
        seen = {
            (row.signal, row.target)
            for group in previous
            for row in _SIGNALS.validate_python(group)
        }
        new_signals = []
        for observation in dict.fromkeys(requested):
            key = (observation.signal, observation.target)
            if key not in seen and (
                signal := _matching_reference(by_identity[observation.target], observation)
            ):
                new_signals.append(signal)
        if not new_signals:
            return before
        return _write(
            conn,
            service,
            step(before, new_signals, service.settings.context.adaptive),
            signals=new_signals,
            bundle_id=request.bundle_id,
        )
