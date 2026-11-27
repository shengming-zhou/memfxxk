from __future__ import annotations

import json
import logging
from collections import Counter
from time import perf_counter
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Connection, Select, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import RowMapping

from memfxxk.db import (
    compactions,
    events,
    memory_marks,
    records,
    sessions,
    transaction,
)
from memfxxk.domain import Scope
from memfxxk.domain.context import (
    CommittedCompaction,
    CompactionDraft,
    CompactionMethod,
    PrepareCompaction,
)
from memfxxk.domain.errors import ServiceError
from memfxxk.domain.sessions import SessionState
from memfxxk.embedding import _truncate
from memfxxk.ids import derived_id
from memfxxk.ingest import NotFound, _turn_part
from memfxxk.llm.runner import TaskRunner
from memfxxk.manage.common import lock_user_operations
from memfxxk.prompts.compact import PROMPT, SCHEMA, VERSION, Checkpoint
from memfxxk.scope import visible_filter
from memfxxk.sessions import read_session
from memfxxk.text import make_token_counter

from .preferences import preferences_for
from .storage import derived_record

if TYPE_CHECKING:
    from memfxxk.service import BoundService

logger = logging.getLogger(__name__)


def _summary_id(service: BoundService, session_id: str) -> UUID:
    return derived_id("session-summary", service.ctx.org_id, session_id)


def _state(conn: Connection, service: BoundService, session_id: str) -> SessionState:
    state = read_session(conn, service.ctx, session_id, lock=True)
    if state.scope != Scope.model_validate(service.ctx.model_dump()):
        raise NotFound
    return state


def _current(conn: Connection, service: BoundService, session_id: str) -> RowMapping | None:
    return (
        conn.execute(
            select(records).where(
                records.c.memory_id == _summary_id(service, session_id),
                records.c.status == "active",
                records.c.is_current,
                visible_filter(records, service.ctx),
            )
        )
        .mappings()
        .one_or_none()
    )


def _draft_query(service: BoundService, draft_id: UUID) -> Select:
    return select(compactions).where(
        compactions.c.id == draft_id, visible_filter(compactions, service.ctx)
    )


def _draft(row: RowMapping) -> CompactionDraft:
    return CompactionDraft.model_validate({**row, "draft_id": row["id"]})


def _sources(
    conn: Connection, service: BoundService, session_id: str, start: int, end: int
) -> list[RowMapping]:
    return list(
        conn.execute(
            select(events)
            .where(
                visible_filter(events, service.ctx),
                events.c.session_id == session_id,
                events.c.session_seq.between(start, end),
                ~events.c.redacted,
                events.c.kind.in_(("message", "tool_call", "tool_result")),
            )
            .order_by(events.c.session_seq)
        ).mappings()
    )


def _check_boundary(
    conn: Connection,
    service: BoundService,
    state: SessionState,
    rows: list[RowMapping],
    end: int,
) -> None:
    if (
        not rows
        or rows[-1]["session_seq"] != end
        or rows[-1]["kind"] != "message"
        or rows[-1]["role"] != "assistant"
    ):
        raise ServiceError(422, "incomplete_turn", "Compaction must end after an assistant reply")
    turn_ids = {row["turn_id"] for row in rows if row["turn_id"] is not None}
    if conn.scalar(
        select(events.c.id)
        .where(
            visible_filter(events, service.ctx),
            events.c.session_id == state.session_id,
            events.c.session_seq > end,
            events.c.turn_id.in_(turn_ids),
            ~events.c.redacted,
        )
        .limit(1)
    ):
        raise ServiceError(422, "incomplete_turn", "The cutoff would split a turn")
    pending: Counter[tuple[str | None, str]] = Counter()
    for row in rows:
        if row["kind"] in {"tool_call", "tool_result"}:
            payload = row["payload"]
            key = (row["turn_id"], str(payload.get("tool_call_id") or payload.get("name") or ""))
            pending[key] += 1 if row["kind"] == "tool_call" else -1
            if pending[key] < 0:
                raise ServiceError(
                    422, "incomplete_tool_round", "A tool result has no matching call"
                )
    if any(pending.values()):
        raise ServiceError(422, "incomplete_tool_round", "A tool call is still awaiting its result")


def _protected(conn: Connection, service: BoundService, source_ids: list[UUID]) -> set[UUID]:
    marked = (
        select(memory_marks.c.id)
        .where(
            visible_filter(memory_marks, service.ctx),
            memory_marks.c.memory_id == records.c.memory_id,
            memory_marks.c.mark.in_(("important", "pinned")),
        )
        .exists()
    )
    relevant = list(
        conn.execute(
            select(records.c.memory_id, records.c.source_event_ids, marked.label("marked")).where(
                visible_filter(records, service.ctx),
                records.c.is_current,
                records.c.status == "active",
                records.c.source_event_ids.overlap(source_ids),
            )
        )
    )
    preferences = preferences_for(conn, service.ctx, [row.memory_id for row in relevant])
    return {
        event_id
        for row in relevant
        if row.marked or (preferences.get(row.memory_id, (None, []))[0] or 0) >= 4
        for event_id in row.source_event_ids
    }


def _render(
    service: BoundService,
    request: PrepareCompaction,
    rows: list[RowMapping],
    previous: str,
    protected: set[UUID],
    source_ids: list[UUID],
    request_id: UUID,
    prior_checkpoint: dict | None,
) -> tuple[str, CompactionMethod, Checkpoint | None]:
    counter = make_token_counter(service.settings.token_counter)
    runner = TaskRunner.for_service(service, "compact")
    if runner.config.model or runner.fake is not None:
        payload = json.dumps(
            {
                "target_tokens": request.target_tokens,
                "previous_checkpoint": prior_checkpoint
                if prior_checkpoint is not None
                else previous,
                "events": [
                    {
                        "id": str(row["id"]),
                        "seq": row["session_seq"],
                        "role": row["role"],
                        "kind": row["kind"],
                        "content": row["content"],
                        "payload": row["payload"],
                        "event_time": row["event_time"].isoformat(),
                        "important": row["id"] in protected,
                    }
                    for row in rows
                ],
            },
            ensure_ascii=False,
        )
        if counter.count(PROMPT + payload) > service.settings.m2.compact_input_tokens:
            raise ServiceError(422, "compaction_input_too_large", "Choose a smaller closed range")
        messages = [
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": payload},
        ]
        result = runner.complete(
            "compact",
            service.ctx,
            messages,
            schema=SCHEMA,
            prompt_version=VERSION,
            request_id=request_id,
            source_event_ids=source_ids,
        )
        assert result.parsed is not None
        allowed = set(source_ids)
        fields = {}
        dropped = 0
        rendered = []
        for field in Checkpoint.model_fields:
            claims = []
            for claim in getattr(result.parsed, field):
                if not set(claim.source_event_ids) <= allowed or not claim.text.strip():
                    dropped += 1
                    continue
                line = f"{field.replace('_', ' ')}: {claim.text.strip()}"
                if counter.count("\n".join([*rendered, line])) > request.target_tokens:
                    continue
                rendered.append(line)
                claims.append(claim)
            fields[field] = claims
        if dropped:
            logger.warning(
                "compaction discarded invalid claims count=%d draft=%s", dropped, request_id
            )
        checkpoint = Checkpoint.model_validate(fields)
        summary = "\n".join(rendered) or _truncate(
            "No durable checkpoint entries.", request.target_tokens, counter
        )
        return summary, "llm", checkpoint

    # Local extractive fallback. Favor explicitly important sources, then recent exchanges.
    lines = [
        f"[{'important ' if row['id'] in protected else ''}{row['id']}] {_turn_part(row)}"
        for row in rows
    ]
    ranked = sorted(
        range(len(rows)),
        key=lambda i: (
            rows[i]["id"] in protected,
            rows[i]["id"] in protected and rows[i]["role"] == "user",
            i,
        ),
        reverse=True,
    )
    carry = _truncate(previous, request.target_tokens // 3, counter)
    available = request.target_tokens - counter.count(carry + "\n")
    retained: dict[int, str] = {}
    for index in ranked:
        combined = "\n".join(retained[k] for k in sorted(retained))
        room = available - counter.count(combined + "\n")
        if room <= 0:
            break
        retained[index] = _truncate(lines[index], room, counter)
    fresh = "\n".join(retained[k] for k in sorted(retained))
    summary = _truncate(
        "\n".join(item for item in (carry, fresh) if item), request.target_tokens, counter
    )
    return summary, "extractive_fallback", None


def _recent_users(conn: Connection, service: BoundService, ids: list[UUID]) -> list[UUID]:
    remaining = service.settings.m2.recent_user_tokens
    if not remaining:
        return []
    counter = make_token_counter(service.settings.token_counter)
    retained = []
    for row in conn.execute(
        select(events.c.id, events.c.content)
        .where(
            events.c.id.in_(ids),
            events.c.kind == "message",
            events.c.role == "user",
            ~events.c.redacted,
            visible_filter(events, service.ctx),
        )
        .order_by(events.c.session_seq.desc())
    ):
        cost = counter.count(f"[user] {row.content or ''}")
        if cost > remaining:
            break
        retained.append(row.id)
        remaining -= cost
    return list(reversed(retained))


def prepare(service: BoundService, request: PrepareCompaction) -> CompactionDraft:
    started = perf_counter()
    draft_id = derived_id(
        "compaction",
        service.ctx.org_id,
        request.session_id,
        str(request.revision),
        str(request.through_seq),
        str(request.target_tokens),
    )
    with transaction(service.engine) as conn:
        state = _state(conn, service, request.session_id)
        existing = conn.execute(_draft_query(service, draft_id)).mappings().one_or_none()
        if existing is not None and existing["summary"] is not None:
            if state.revision == request.revision:
                return _draft(existing)
        if state.revision != request.revision:
            raise ServiceError(409, "revision_conflict", "Session revision has changed")
        prior = _current(conn, service, state.session_id)
        if (
            state.summary
            or prior is not None
            and prior["source_pos"]["seq_to"] != state.summary_upto_seq
        ):
            prior = None
        start = (prior["source_pos"]["seq_to"] + 1) if prior is not None else 1
        if not start <= request.through_seq <= state.last_seq:
            raise ServiceError(422, "invalid_compaction_range", "Select a new stored message range")
        rows = _sources(conn, service, state.session_id, start, request.through_seq)
        _check_boundary(conn, service, state, rows, request.through_seq)
        new_ids = [row["id"] for row in rows]
        all_ids = [*prior["source_event_ids"], *new_ids] if prior is not None else new_ids
        previous = prior["content"] if prior is not None else ""
        prior_checkpoint = prior["metadata"].get("checkpoint") if prior is not None else None
        protected = _protected(conn, service, all_ids)
        if prior is not None:
            recall_ids = protected.intersection(prior["source_event_ids"])
            if recall_ids:
                retained = conn.execute(
                    select(events)
                    .where(
                        events.c.id.in_(recall_ids),
                        ~events.c.redacted,
                        visible_filter(events, service.ctx),
                    )
                    .order_by(events.c.session_seq)
                ).mappings()
                rows = [*retained, *rows]
    summary, method, checkpoint = _render(
        service,
        request,
        rows,
        previous,
        protected,
        all_ids,
        draft_id,
        prior_checkpoint,
    )
    tokens = make_token_counter(service.settings.token_counter).count(summary)
    if not summary or not 0 < tokens <= request.target_tokens:
        raise ServiceError(422, "compaction_budget_exhausted", "Summary does not fit the budget")
    with transaction(service.engine) as conn:
        current = _state(conn, service, request.session_id)
        snapshot = _sources(conn, service, state.session_id, start, request.through_seq)
        if current.revision != request.revision or [row["id"] for row in snapshot] != new_ids:
            raise ServiceError(409, "stale_compaction", "Compaction source changed")
        conn.execute(
            insert(compactions)
            .values(
                id=draft_id,
                **state.scope.model_dump(),
                session_id=state.session_id,
                base_revision=request.revision,
                from_seq=start,
                through_seq=request.through_seq,
                source_event_ids=all_ids,
                summary=summary,
                tokens=tokens,
                method=method,
                checkpoint=checkpoint.model_dump(mode="json") if checkpoint else None,
                retained_user_event_ids=_recent_users(conn, service, all_ids)
                if method == "llm"
                else [],
            )
            .on_conflict_do_nothing()
        )
        row = conn.execute(_draft_query(service, draft_id)).mappings().one()
    logger.info(
        "compaction prepared tokens=%d events=%d ms=%.1f",
        tokens,
        len(rows),
        (perf_counter() - started) * 1000,
    )
    return _draft(row)


def commit(service: BoundService, draft_id: UUID) -> CommittedCompaction:
    with transaction(service.engine) as conn:
        lock_user_operations(conn, service.ctx)
        row = conn.execute(_draft_query(service, draft_id)).mappings().one_or_none()
        if row is None:
            raise NotFound
        state = _state(conn, service, row["session_id"])
        if row["committed_revision"] is not None:
            return _committed(row)
        if state.revision != row["base_revision"]:
            raise ServiceError(409, "stale_compaction", "Compaction source changed")
        # Source existence is checked again under the same session lock as deletion and ingest.
        source = _sources(conn, service, state.session_id, 1, row["through_seq"])
        if {event["id"] for event in source} != set(row["source_event_ids"]):
            raise ServiceError(409, "stale_compaction", "Compaction source changed")
        _check_boundary(conn, service, state, source, row["through_seq"])
        memory_id = _summary_id(service, state.session_id)
        version = 1 + (
            conn.scalar(
                select(func.max(records.c.version)).where(
                    records.c.memory_id == memory_id,
                    visible_filter(records, service.ctx),
                )
            )
            or 0
        )
        derived_record(
            conn,
            service.ctx,
            state.scope,
            memory_id=memory_id,
            version=version,
            kind="summary",
            content=row["summary"],
            sources=list(row["source_event_ids"]),
            session_id=state.session_id,
            event_time=source[-1]["event_time"],
            source_pos={
                "session_id": state.session_id,
                "seq_from": 1,
                "seq_to": row["through_seq"],
            },
            metadata={
                "compaction_method": row["method"],
                "checkpoint": row["checkpoint"],
                "retained_user_event_ids": [str(item) for item in row["retained_user_event_ids"]],
            },
        )
        revision = state.revision + 1
        conn.execute(
            sessions.update()
            .where(
                sessions.c.id == state.session_id,
                visible_filter(sessions, service.ctx),
            )
            .values(
                summary=None,
                summary_upto_seq=row["through_seq"],
                revision=revision,
                updated_at=func.now(),
            )
        )
        saved = (
            conn.execute(
                compactions.update()
                .where(
                    compactions.c.id == draft_id,
                    visible_filter(compactions, service.ctx),
                )
                .values(
                    summary=None,
                    memory_id=memory_id,
                    committed_version=version,
                    committed_revision=revision,
                )
                .returning(compactions)
            )
            .mappings()
            .one()
        )
        return _committed(saved)


def _committed(row: RowMapping) -> CommittedCompaction:
    return CommittedCompaction(
        memory_id=row["memory_id"],
        version=row["committed_version"],
        session_revision=row["committed_revision"],
        through_seq=row["through_seq"],
        method=row["method"],
    )
