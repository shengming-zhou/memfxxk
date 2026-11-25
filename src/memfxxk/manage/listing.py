from __future__ import annotations

import json
from base64 import b64decode, urlsafe_b64encode
from binascii import Error
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import and_, cast, false, func, or_, select, tuple_
from sqlalchemy.dialects.postgresql import TSQUERY

from memfxxk.db import memory_marks, memory_relations, records
from memfxxk.domain import Scope
from memfxxk.domain.manage import ListRequest, MemoryItem, MemoryPage
from memfxxk.scope import visible_filter
from memfxxk.text import MixedTokenizer, build_tsquery_literal

from .common import ManagementError

if TYPE_CHECKING:
    from memfxxk.service import BoundService


def _decode_cursor(value: str) -> tuple[datetime, UUID]:
    try:
        raw_timestamp, raw_id = json.loads(b64decode(value, altchars=b"-_", validate=True))
        timestamp = datetime.fromisoformat(raw_timestamp)
        if timestamp.utcoffset() is None:
            raise ValueError("Cursor requires a timezone")
        return timestamp, UUID(raw_id)
    except (ValueError, TypeError, AttributeError, Error) as error:
        raise ManagementError(422, "invalid_cursor", "The memory cursor is invalid") from error


def list_memories(service: BoundService, filters: ListRequest) -> MemoryPage:
    ctx = service.ctx
    related = records.alias("related")
    from_endpoint = memory_relations.c.from_memory_id
    to_endpoint = memory_relations.c.to_memory_id
    relation_count = (
        select(func.count())
        .select_from(memory_relations.join(related, related.c.is_current))
        .where(
            memory_relations.c.org_id == ctx.org_id,
            visible_filter(related, ctx),
            or_(
                and_(from_endpoint == records.c.memory_id, to_endpoint == related.c.memory_id),
                and_(to_endpoint == records.c.memory_id, from_endpoint == related.c.memory_id),
            ),
        )
        .correlate(records)
        .scalar_subquery()
    )
    query = select(records, relation_count.label("relation_count")).where(
        visible_filter(records, ctx), records.c.is_current, records.c.status == filters.status
    )
    if filters.types:
        query = query.where(records.c.type.in_(filters.types))
    if filters.origin is not None:
        query = query.where(records.c.origin == filters.origin)
    if filters.session_id is not None:
        query = query.where(records.c.session_id == filters.session_id)
    if filters.created_from is not None:
        query = query.where(records.c.recorded_at >= filters.created_from)
    if filters.created_to is not None:
        query = query.where(records.c.recorded_at <= filters.created_to)
    if filters.scope_level is not None:
        selected = f"{filters.scope_level}_id"
        for column in (records.c.user_id, records.c.agent_id, records.c.project_id):
            if column.name == selected:
                value = getattr(ctx, selected)
                query = query.where(column == value if value is not None else false())
            else:
                query = query.where(column.is_(None))
    if filters.q is not None:
        terms = build_tsquery_literal(MixedTokenizer().tokenize(filters.q))
        query = query.where(
            records.c.lexemes.bool_op("@@")(cast(terms, TSQUERY)) if terms else false()
        )
    for mark in dict.fromkeys(filters.marks or []):
        query = query.where(
            select(memory_marks.c.id)
            .where(
                visible_filter(memory_marks, ctx),
                memory_marks.c.memory_id == records.c.memory_id,
                memory_marks.c.mark == mark,
            )
            .exists()
        )
    if filters.cursor is not None:
        query = query.where(
            tuple_(records.c.recorded_at, records.c.id) < _decode_cursor(filters.cursor)
        )

    # Read items and applicable marks from the same statement snapshot.
    mark_values = (
        select(func.array_agg(memory_marks.c.mark.distinct()))
        .where(visible_filter(memory_marks, ctx), memory_marks.c.memory_id == records.c.memory_id)
        .correlate(records)
        .scalar_subquery()
    )
    query = query.add_columns(mark_values.label("marks")).order_by(
        records.c.recorded_at.desc(), records.c.id.desc()
    )
    with service.engine.connect() as conn:
        rows = conn.execute(query.limit(filters.limit + 1)).mappings().all()
    page = rows[: filters.limit]
    cursor = None
    if len(rows) > filters.limit:
        final = page[-1]
        cursor = urlsafe_b64encode(
            json.dumps([final["recorded_at"].isoformat(), str(final["id"])]).encode()
        ).decode("ascii")
    return MemoryPage(
        items=[
            MemoryItem(
                **{**row, "marks": sorted(row["marks"] or [])},
                snippet=row["content"][:240],
                scope=Scope.model_validate(dict(row)),
            )
            for row in page
        ],
        next_cursor=cursor,
    )
