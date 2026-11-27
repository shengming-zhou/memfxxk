from dataclasses import dataclass
from math import isfinite
from typing import Protocol
from uuid import UUID

from sqlalchemy import ColumnElement, Connection, String, and_, cast, func, or_, select
from sqlalchemy.dialects.postgresql import TSQUERY

from memfxxk.db import memory_preferences, record_embeddings, records
from memfxxk.documents import resolved_document_id
from memfxxk.domain import Scope
from memfxxk.domain.retrieval import SearchRequest
from memfxxk.embedding import embedding_expression, embedding_model_filter
from memfxxk.m2.preferences import preference_order
from memfxxk.scope import visible_filter
from memfxxk.text import build_tsquery_literal


@dataclass(frozen=True, slots=True)
class Candidate:
    id: UUID
    generation: int
    score: float


def search_filters(ctx: Scope, request: SearchRequest) -> list[ColumnElement[bool]]:
    recorded_at = request.as_of_recorded or func.now()
    valid_at = request.as_of_valid or func.now()
    effective = func.coalesce(records.c.valid_from, records.c.event_time)
    newer = records.alias("newer_version")
    newer_effective = func.coalesce(newer.c.valid_from, newer.c.event_time)
    later_version = (
        select(newer.c.id)
        .where(
            newer.c.memory_id == records.c.memory_id,
            visible_filter(newer, ctx),
            newer.c.recorded_at <= recorded_at,
            newer_effective <= valid_at,
            or_(
                newer_effective > effective,
                and_(newer_effective == effective, newer.c.version > records.c.version),
            ),
        )
        .correlate(records)
        .exists()
    )
    filters = [
        visible_filter(records, ctx),
        or_(
            and_(records.c.type != "fact", records.c.is_current),
            and_(
                records.c.type == "fact",
                effective <= valid_at,
                or_(records.c.valid_to.is_(None), records.c.valid_to > valid_at),
                ~later_version,
            ),
        ),
    ]
    if request.as_of_recorded is not None:
        filters.append(records.c.recorded_at <= recorded_at)
    if not request.include_inactive:
        filters.append(records.c.status == "active")
        filters.append(
            ~select(newer.c.id)
            .where(
                newer.c.memory_id == records.c.memory_id,
                newer.c.is_current,
                newer.c.status == "inactive",
                newer.c.metadata["temporal_tombstone"].as_boolean().is_not(True),
                visible_filter(newer, ctx),
            )
            .correlate(records)
            .exists()
        )
    pos = records.c.source_pos
    filters.append(
        or_(
            records.c.type != "doc_chunk",
            pos["document_id"].as_string()
            == cast(resolved_document_id(ctx, pos["path"].as_string()), String),
        )
    )
    if request.types is not None:
        filters.append(records.c.type.in_(request.types))
    if request.session_id is not None:
        filters.append(records.c.session_id == request.session_id)
    if request.time_from is not None:
        filters.append(records.c.event_time >= request.time_from)
    if request.time_to is not None:
        filters.append(records.c.event_time <= request.time_to)
    if request.tags:
        tags = (
            select(memory_preferences.c.tags)
            .where(
                memory_preferences.c.memory_id == records.c.memory_id,
                visible_filter(memory_preferences, ctx),
            )
            .order_by(*preference_order())
            .limit(1)
            .scalar_subquery()
        )
        filters.append(
            tags.op("@>")(
                cast(
                    sorted({tag.strip().casefold() for tag in request.tags}),
                    memory_preferences.c.tags.type,
                )
            )
        )
    return filters


class LexicalIndex(Protocol):
    def search(
        self,
        connection: Connection,
        tokens: list[str],
        filters: list[ColumnElement[bool]],
        limit: int,
    ) -> list[Candidate]: ...


class PgTsLexical:
    def search(
        self,
        connection: Connection,
        tokens: list[str],
        filters: list[ColumnElement[bool]],
        limit: int,
    ) -> list[Candidate]:
        query_literal = build_tsquery_literal(dict.fromkeys(tokens))
        if query_literal is None:
            return []
        query = cast(query_literal, TSQUERY)
        rank = func.ts_rank_cd(records.c.lexemes, query, 32)
        statement = (
            select(records.c.id, records.c.generation, rank)
            .where(records.c.lexemes.op("@@")(query), *filters)
            .order_by(rank.desc(), records.c.id)
            .limit(limit)
        )
        return [
            Candidate(record_id, generation, score)
            for record_id, generation, score in connection.execute(statement)
        ]


class VectorIndex(Protocol):
    def search(
        self,
        connection: Connection,
        vector: list[float],
        filters: list[ColumnElement[bool]],
        limit: int,
    ) -> list[Candidate]: ...


@dataclass(frozen=True, slots=True)
class PgVectorIndex:
    model: str
    dim: int
    ef_search: int

    def enable_iterative_scan(self, connection: Connection) -> None:
        connection.execute(
            select(
                func.set_config("hnsw.ef_search", str(self.ef_search), True),
                func.set_config("hnsw.iterative_scan", "relaxed_order", True),
            )
        )

    def search(
        self,
        connection: Connection,
        vector: list[float],
        filters: list[ColumnElement[bool]],
        limit: int,
    ) -> list[Candidate]:
        distance = embedding_expression(self.dim).cosine_distance(vector)
        statement = (
            select(records.c.id, records.c.generation, distance)
            .join(record_embeddings, record_embeddings.c.record_id == records.c.id)
            .where(
                embedding_model_filter(self.model), record_embeddings.c.dim == self.dim, *filters
            )
            .order_by(distance)
            .limit(limit)
        )
        candidates = [
            Candidate(record_id, generation, 1 - value)
            for record_id, generation, value in connection.execute(statement)
            if value is not None and isfinite(value)
        ]
        # Iterative scans in relaxed order can return a few rows slightly out of order.
        candidates.sort(key=lambda candidate: (-candidate.score, candidate.id))
        return candidates
