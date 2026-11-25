import re
from time import perf_counter
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Connection, select

from memfxxk.db import memory_marks
from memfxxk.db import records as record_table
from memfxxk.domain.retrieval import (
    MemoryResponse,
    SearchHit,
    SearchRequest,
    SearchResponse,
    SearchTimings,
)
from memfxxk.embedding import CachedEmbedder, EmbeddingProvider, make_embedding_provider
from memfxxk.retrieval.channels import (
    Candidate,
    LexicalIndex,
    PgTsLexical,
    PgVectorIndex,
    VectorIndex,
    search_filters,
)
from memfxxk.retrieval.fusion import NoopReranker, Reranker, fuse
from memfxxk.retrieval.records import marks_for, read_memory, reference, related_for, snippet
from memfxxk.scope import visible_filter
from memfxxk.text import MixedTokenizer

if TYPE_CHECKING:
    from memfxxk.service import BoundService

__all__ = [
    "LexicalIndex",
    "NoopReranker",
    "PgTsLexical",
    "PgVectorIndex",
    "Reranker",
    "VectorIndex",
    "fuse",
    "get_memory",
    "search",
]


def _milliseconds(started: float) -> float:
    return (perf_counter() - started) * 1000


def search(
    service: "BoundService",
    request: SearchRequest,
    *,
    embedder: EmbeddingProvider | None = None,
    reranker: Reranker | None = None,
) -> SearchResponse:
    started = perf_counter()
    settings = service.settings.search
    tokens = MixedTokenizer().tokenize(request.query)
    channels = set(request.channels)
    timings = SearchTimings()
    filters = search_filters(service.ctx, request)
    vector: list[float] = []
    if request.query.strip() and channels.intersection(("vector", "important")):
        stage = perf_counter()
        if embedder is None:
            embedder = CachedEmbedder(service.engine, make_embedding_provider(service.settings))
        vector = embedder.embed([request.query])[0]
        timings.embed = _milliseconds(stage)

    lexical_candidates: list[Candidate] = []
    vector_candidates: list[Candidate] = []
    important_candidates: list[Candidate] = []
    with service.engine.connect() as connection:
        if "lexical" in channels:
            stage = perf_counter()
            lexical_candidates = PgTsLexical().search(
                connection, tokens, filters, settings.n_lexical
            )
            timings.lexical = _milliseconds(stage)

        if embedder is not None and any(vector):
            index = PgVectorIndex(embedder.model, embedder.dim, settings.hnsw_ef_search)
            stage = perf_counter()
            index.enable_iterative_scan(connection)
            if "vector" in channels:
                vector_candidates = index.search(connection, vector, filters, settings.n_vector)
                timings.vector = _milliseconds(stage)
            else:
                timings.important += _milliseconds(stage)
            if "important" in channels:
                stage = perf_counter()
                visible_important = (
                    select(memory_marks.c.id)
                    .where(
                        memory_marks.c.memory_id == record_table.c.memory_id,
                        memory_marks.c.mark == "important",
                        visible_filter(memory_marks, service.ctx),
                    )
                    .exists()
                )
                important = index.search(
                    connection, vector, [*filters, visible_important], settings.m_important
                )
                lexical_ids = {candidate.id for candidate in lexical_candidates}
                important_candidates = [
                    candidate
                    for candidate in important
                    if candidate.score >= settings.important_min_sim or candidate.id in lexical_ids
                ]
                timings.important += _milliseconds(stage)

        hits = _build_hits(
            connection,
            service,
            request,
            tokens,
            timings,
            reranker or NoopReranker(),
            lexical_candidates,
            vector_candidates,
            important_candidates,
        )

    timings.total = max(0, _milliseconds(started) - timings.embed)
    return SearchResponse(hits=hits, query_tokens=tokens, timings_ms=timings)


def _build_hits(
    connection: Connection,
    service: "BoundService",
    request: SearchRequest,
    tokens: list[str],
    timings: SearchTimings,
    reranker: Reranker,
    lexical: list[Candidate],
    vector: list[Candidate],
    important: list[Candidate],
) -> list[SearchHit]:
    ids = {candidate.id for group in (lexical, vector, important) for candidate in group}
    if not ids:
        return []

    stage = perf_counter()
    filters = search_filters(service.ctx, request)
    statement = select(record_table).where(record_table.c.id.in_(ids), *filters)
    rows = {row["id"]: row for row in connection.execute(statement).mappings()}

    def fresh(candidates: list[Candidate]) -> list[Candidate]:
        return [
            candidate
            for candidate in candidates
            if candidate.id in rows and rows[candidate.id]["generation"] == candidate.generation
        ]

    settings = service.settings.search
    scores = fuse(fresh(lexical), fresh(vector), fresh(important), settings)
    marks = marks_for(connection, service.ctx, [rows[key]["memory_id"] for key in scores])
    matches = (
        re.compile("|".join(map(re.escape, dict.fromkeys(tokens))), re.IGNORECASE)
        if tokens
        else None
    )
    ranked = sorted(scores, key=lambda key: (-scores[key].final, key))
    hits = []
    for key in ranked:
        row = rows[key]
        content = row["content"]
        excerpt = snippet(content, matches)
        hits.append(
            SearchHit(
                **reference(row).model_dump(),
                snippet=excerpt,
                content_truncated=excerpt != content,
                marks=marks.get(row["memory_id"], []),
                score=scores[key],
            )
        )
    by_memory_id = {hit.memory_id: key for hit, key in zip(hits, ranked, strict=True)}
    selected = reranker.rerank(request, hits)[: request.k]
    timings.fuse = _milliseconds(stage)
    stage = perf_counter()
    if request.expand_relations and selected:
        related = related_for(
            connection,
            service.ctx,
            [hit.memory_id for hit in selected],
            settings.max_related,
            matches,
        )
        for hit in selected:
            hit.related = related.get(hit.memory_id, [])
    timings.expand = _milliseconds(stage)
    if not selected:
        return []

    remaining = connection.execute(
        select(record_table.c.id, record_table.c.generation).where(
            record_table.c.id.in_([by_memory_id[hit.memory_id] for hit in selected]), *filters
        ),
    )
    fresh_ids = {key for key, generation in remaining if rows[key]["generation"] == generation}
    return [hit for hit in selected if by_memory_id[hit.memory_id] in fresh_ids]


def get_memory(
    service: "BoundService",
    memory_id: UUID,
    version: int | None = None,
    window: int = 0,
) -> MemoryResponse:
    if window < 0:
        raise ValueError("window must be nonnegative")
    with service.engine.connect() as connection:
        return read_memory(connection, service.ctx, memory_id, version, window)
