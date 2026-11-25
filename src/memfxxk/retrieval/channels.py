from dataclasses import dataclass
from math import isfinite
from typing import Protocol
from uuid import UUID

from sqlalchemy import ColumnElement, Connection, cast, func, select
from sqlalchemy.dialects.postgresql import TSQUERY

from memfxxk.db import record_embeddings, records
from memfxxk.domain import Scope
from memfxxk.domain.retrieval import SearchRequest
from memfxxk.embedding import embedding_expression, embedding_model_filter
from memfxxk.scope import visible_filter
from memfxxk.text import build_tsquery_literal


@dataclass(frozen=True, slots=True)
class Candidate:
    id: UUID
    generation: int
    score: float


def search_filters(ctx: Scope, request: SearchRequest) -> list[ColumnElement[bool]]:
    filters = [visible_filter(records, ctx), records.c.is_current]
    if not request.include_inactive:
        filters.append(records.c.status == "active")
    if request.types is not None:
        filters.append(records.c.type.in_(request.types))
    if request.session_id is not None:
        filters.append(records.c.session_id == request.session_id)
    if request.time_from is not None:
        filters.append(records.c.event_time >= request.time_from)
    if request.time_to is not None:
        filters.append(records.c.event_time <= request.time_to)
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
