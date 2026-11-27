import re
from datetime import UTC, datetime
from time import perf_counter
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import ColumnElement, Connection, select
from sqlalchemy.engine import RowMapping

from memfxxk.db import memory_marks
from memfxxk.db import records as record_table
from memfxxk.domain.retrieval import (
    GraphFact,
    MemoryResponse,
    ScoreDetails,
    SearchHit,
    SearchRequest,
    SearchResponse,
    SearchTimings,
)
from memfxxk.embedding import CachedEmbedder, EmbeddingProvider, make_embedding_provider
from memfxxk.m2.preferences import preferences_for
from memfxxk.retrieval.channels import (
    Candidate,
    LexicalIndex,
    PgTsLexical,
    PgVectorIndex,
    VectorIndex,
    search_filters,
)
from memfxxk.retrieval.fusion import NoopReranker, Reranker, fuse
from memfxxk.retrieval.graph import graph_neighbors
from memfxxk.retrieval.records import marks_for, read_memory, reference, related_for, snippet
from memfxxk.retrieval.temporal import search_time
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
    note_candidates: list[Candidate] = []
    time_candidates: list[Candidate] = []
    with service.engine.connect() as connection:
        if "time" in channels or service.settings.strategies.recall == "hindsight":
            stage = perf_counter()
            time_candidates = search_time(
                connection,
                request.query,
                request.query_time or request.as_of_valid or datetime.now(UTC),
                filters,
                settings.n_time,
            )
            timings.time = _milliseconds(stage)
        if "lexical" in channels:
            stage = perf_counter()
            lexical_candidates = PgTsLexical().search(
                connection, tokens, filters, settings.n_lexical
            )
            timings.lexical = _milliseconds(stage)

        if embedder is not None and any(vector):
            pg_index = PgVectorIndex(embedder.model, embedder.dim, settings.hnsw_ef_search)
            stage = perf_counter()
            pg_index.enable_iterative_scan(connection)
            index: VectorIndex = pg_index
            if settings.vector_backend == "qdrant":
                from memfxxk.qdrant import QdrantClient
                from memfxxk.retrieval.qdrant import QdrantVectorIndex

                index = QdrantVectorIndex(QdrantClient(service.settings.qdrant), pg_index)
            if "vector" in channels:
                vector_candidates = index.search(connection, vector, filters, settings.n_vector)
                if service.settings.strategies.notes == "amem":
                    from memfxxk.db import amem_revisions
                    from memfxxk.m2.amem import search_notes

                    note_filters = list(filters)
                    if request.as_of_recorded is not None:
                        note_filters.append(amem_revisions.c.created_at <= request.as_of_recorded)
                    note_candidates = search_notes(
                        connection, service.ctx, embedder.model, vector, note_filters
                    )
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

        if reranker is None and settings.reranker == "llm":
            from memfxxk.retrieval.model_reranker import ModelReranker

            reranker = ModelReranker(service)
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
            note_candidates,
            vector,
            time_candidates,
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
    notes: list[Candidate],
    query_vector: list[float],
    time_candidates: list[Candidate],
) -> list[SearchHit]:
    ids = {
        candidate.id
        for group in (lexical, vector, important, notes, time_candidates)
        for candidate in group
    }
    graph = service.settings.strategies.graph
    hindsight = service.settings.strategies.recall == "hindsight"
    if not ids and not (graph == "graphiti" and request.graph_hops):
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
    graph_hits = {}

    def add_rank(key: UUID, weight: float, rank: int) -> ScoreDetails:
        score = scores.setdefault(key, ScoreDetails(rrf=0, boost=1, final=0))
        score.rrf += weight / (settings.rrf_k + rank)
        score.final = score.rrf * score.boost
        return score

    for rank, candidate in enumerate(fresh(time_candidates), start=1):
        score = add_rank(candidate.id, settings.time_weight, rank)
        score.time_rank, score.time_score = rank, candidate.score
    for rank, candidate in enumerate(fresh(notes), start=1):
        score = add_rank(candidate.id, settings.vector_weight, rank)
        score.note_rank = rank
        score.note_similarity = candidate.score
    if hindsight and graph != "off" and query_vector:
        start_graph = perf_counter()
        seeds = [rows[candidate.id] for candidate in fresh(vector)[:4]]
        graph_request = request.model_copy(update={"graph_hops": request.graph_hops or 1})
        for rank, (row, path, fact) in enumerate(
            _graph_rows(connection, service, graph_request, seeds, filters, tokens, query_vector),
            start=1,
        ):
            rows[row["id"]] = row
            graph_hits[row["id"]] = (path, fact)
            add_rank(row["id"], settings.vector_weight, rank).graph_rank = rank
        timings.graph = _milliseconds(start_graph)
    if service.settings.strategies.mem0_additive_ranking:
        from memfxxk.m2.mem0_ranking import apply_additive_weights

        apply_additive_weights(scores, rows, tokens)
    memory_ids = [rows[key]["memory_id"] for key in scores]
    marks = marks_for(connection, service.ctx, memory_ids)
    preferences = preferences_for(connection, service.ctx, memory_ids)
    for key, score in scores.items():
        priority, _ = preferences.get(rows[key]["memory_id"], (None, []))
        if priority is not None:
            multiplier = 1 + 0.15 * (priority - 3)
            score.boost *= multiplier
            score.final *= multiplier
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
        priority, tags = preferences.get(row["memory_id"], (None, []))
        path, graph_fact = graph_hits.get(key, ([], None))
        hits.append(
            SearchHit(
                **reference(row).model_dump(),
                snippet=excerpt,
                content_truncated=excerpt != content,
                marks=marks.get(row["memory_id"], []),
                priority=priority,
                tags=tags,
                score=scores[key],
                graph_path=path,
                graph_fact=graph_fact,
            )
        )
    by_memory_id = {hit.memory_id: key for hit, key in zip(hits, ranked, strict=True)}
    timings.fuse = _milliseconds(stage)
    stage = perf_counter()
    selected = reranker.rerank(request, hits)[: request.k]
    timings.rerank = _milliseconds(stage)
    stage = perf_counter()
    if (
        not hindsight
        and request.graph_hops
        and request.k > 1
        and graph != "off"
        and (selected or graph == "graphiti")
    ):
        keep = selected[: max(1, request.k // 2)]
        seeds = [rows[by_memory_id[hit.memory_id]] for hit in keep]
        graph_results = _graph_rows(
            connection, service, request, seeds, filters, tokens, query_vector
        )
        matched_facts = {row["memory_id"]: fact for row, _, fact in graph_results}
        for hit in keep:
            hit.graph_fact = matched_facts.get(hit.memory_id)
        kept_ids = {hit.memory_id for hit in keep}
        neighbors = [
            (row, path, fact)
            for row, path, fact in graph_results
            if row["memory_id"] not in kept_ids
        ]
        graph_ids = [row["memory_id"] for row, _, _ in neighbors]
        graph_marks = marks_for(connection, service.ctx, graph_ids)
        graph_preferences = preferences_for(connection, service.ctx, graph_ids)
        expanded = []
        for row, path, fact in neighbors:
            memory_id = row["memory_id"]
            rows[row["id"]] = row
            by_memory_id[memory_id] = row["id"]
            excerpt = snippet(row["content"], matches)
            priority, tags = graph_preferences.get(memory_id, (None, []))
            expanded.append(
                SearchHit(
                    **reference(row).model_dump(),
                    snippet=excerpt,
                    content_truncated=excerpt != row["content"],
                    marks=graph_marks.get(memory_id, []),
                    graph_path=path,
                    graph_fact=fact,
                    priority=priority,
                    tags=tags,
                    score=ScoreDetails(rrf=0, boost=1, final=0),
                )
            )
        remaining_ids = set(graph_ids)
        backfill = [hit for hit in selected[len(keep) :] if hit.memory_id not in remaining_ids]
        selected = [*keep, *expanded, *backfill][: request.k]
        timings.graph = _milliseconds(stage)
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

    verify = {
        by_memory_id[memory_id]
        for hit in selected
        for memory_id in [hit.memory_id, *hit.graph_path]
    }
    remaining = connection.execute(
        select(record_table.c.id, record_table.c.generation).where(
            record_table.c.id.in_(verify), *filters
        ),
    )
    fresh_ids = {key for key, generation in remaining if rows[key]["generation"] == generation}
    facts = {hit.graph_fact.edge_id: hit.graph_fact for hit in selected if hit.graph_fact}
    if facts:
        from memfxxk.db import entity_edges

        live = dict(
            connection.execute(
                select(entity_edges.c.id, entity_edges.c.generation).where(
                    visible_filter(entity_edges, service.ctx), entity_edges.c.id.in_(facts)
                )
            ).all()
        )
        for hit in selected:
            if hit.graph_fact and live.get(hit.graph_fact.edge_id) != hit.graph_fact.generation:
                hit.graph_fact = None
    return [
        hit
        for hit in selected
        if all(by_memory_id[item] in fresh_ids for item in [hit.memory_id, *hit.graph_path])
    ]


def _graph_rows(
    connection: Connection,
    service: "BoundService",
    request: SearchRequest,
    seeds: list[RowMapping],
    filters: list[ColumnElement[bool]],
    tokens: list[str],
    query_vector: list[float],
) -> list[tuple[RowMapping, list[UUID], GraphFact | None]]:
    if service.settings.strategies.graph == "graphiti":
        from memfxxk.m2.temporal_graph.search import graph_matches

        return [
            (item.row, item.path, item.fact)
            for item in graph_matches(
                connection, service, request, seeds, filters, tokens, query_vector
            )
        ]
    return [
        (row, path, None)
        for row, path in graph_neighbors(connection, service.ctx, request, seeds, filters)
    ]


def get_memory(
    service: "BoundService",
    memory_id: UUID,
    version: int | None = None,
    window: int = 0,
) -> MemoryResponse:
    if window < 0:
        raise ValueError("window must be nonnegative")
    with service.engine.connect() as connection:
        result = read_memory(connection, service.ctx, memory_id, version, window)
        result.priority, result.tags = preferences_for(connection, service.ctx, [memory_id]).get(
            memory_id, (None, [])
        )
        return result
