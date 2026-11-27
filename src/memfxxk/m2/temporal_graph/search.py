from collections import Counter
from dataclasses import dataclass
from math import log1p
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import Connection, case, cast, func, or_, select
from sqlalchemy.dialects.postgresql import TSQUERY
from sqlalchemy.engine import RowMapping

from memfxxk.context.story_time import at as story_at
from memfxxk.context.story_time import get as story_time
from memfxxk.db import entities, entity_edges, entity_mentions, records
from memfxxk.domain import Scope
from memfxxk.domain.retrieval import GraphFact, SearchRequest
from memfxxk.m2.mem0 import _cosine
from memfxxk.scope import visible_filter
from memfxxk.text import MixedTokenizer, build_tsquery_literal

from .ingest import CANDIDATE_LIMIT, MIN_SIMILARITY

if TYPE_CHECKING:
    from memfxxk.service import BoundService


@dataclass(frozen=True, slots=True)
class GraphMatch:
    row: RowMapping
    path: list[UUID]
    fact: GraphFact | None = None


def _period(ctx: Scope, request: SearchRequest) -> tuple[Any, list]:
    valid = request.as_of_valid or request.time_to or func.now()
    recorded = request.as_of_recorded or func.now()
    start = func.coalesce(entity_edges.c.valid_at, entity_edges.c.asserted_at)
    end = case(
        (entity_edges.c.expired_at > recorded, entity_edges.c.declared_invalid_at),
        else_=entity_edges.c.invalid_at,
    )
    source = entities.alias("visible_graph_source")
    target = entities.alias("visible_graph_target")
    filters = [
        visible_filter(entity_edges, ctx),
        entity_edges.c.created_at <= recorded,
        start <= valid,
        or_(end.is_(None), end > valid),
        select(source.c.id)
        .where(source.c.id == entity_edges.c.source_id, visible_filter(source, ctx))
        .exists(),
        select(target.c.id)
        .where(target.c.id == entity_edges.c.target_id, visible_filter(target, ctx))
        .exists(),
    ]
    if request.as_of_story is not None:
        filters.append(story_at(ctx, "edge", entity_edges.c.id, request.as_of_story))
    return end, filters


def _bm25(rows: list[RowMapping], terms: list[str], text: Any) -> list[RowMapping]:
    if not rows:
        return []
    tokenizer = MixedTokenizer()
    counts = {row["id"]: Counter(tokenizer.tokenize(text(row))) for row in rows}
    size = {key: words.total() for key, words in counts.items()}
    avg_size = sum(size.values()) / len(rows) or 1.0
    idf = {}
    for term in set(terms):
        frequency = sum(term in words for words in counts.values())
        idf[term] = log1p((len(rows) - frequency + 0.5) / (frequency + 0.5))

    def score(row: RowMapping) -> float:
        key = row["id"]
        denom = 1.2 * (0.25 + 0.75 * size[key] / avg_size)
        return sum(
            weight * counts[key][term] * 2.2 / (counts[key][term] + denom)
            for term, weight in idf.items()
            if counts[key][term] > 0
        )

    return sorted(rows, key=lambda row: (-score(row), row["id"]))[:CANDIDATE_LIMIT]


def _channels(
    conn: Connection,
    table: Any,
    filters: list,
    terms: list[str],
    vector: list[float],
    model: str,
    text: Any,
    extra_columns: tuple = (),
) -> tuple[list[RowMapping], list[RowMapping]]:
    statement = select(table, *extra_columns).where(*filters)
    lexical = []
    query = build_tsquery_literal(dict.fromkeys(terms))
    if query:
        tsquery = cast(query, TSQUERY)
        scanned = (
            conn.execute(
                statement.where(table.c.lexemes.op("@@")(tsquery))
                .order_by(func.ts_rank_cd(table.c.lexemes, tsquery).desc(), table.c.id)
                .limit(CANDIDATE_LIMIT * 5)
            )
            .mappings()
            .all()
        )
        lexical = _bm25(list(scanned), terms, text)
    semantic = []
    if any(vector):
        embedding = table.c.name_vector if table is entities else table.c.fact_vector
        distance = embedding.cosine_distance(vector)
        semantic = (
            conn.execute(
                statement.where(
                    table.c.embedding_model == model,
                    func.vector_dims(embedding) == len(vector),
                    distance <= 1 - MIN_SIMILARITY,
                )
                .order_by(distance, table.c.id)
                .limit(CANDIDATE_LIMIT)
            )
            .mappings()
            .all()
        )
    return lexical, list(semantic)


def _rrf(
    groups: list[list[RowMapping]], k: int
) -> tuple[dict[UUID, RowMapping], dict[UUID, float]]:
    rows = {}
    score = {}
    for group in groups:
        for rank, row in enumerate(group, start=1):
            key = row["id"]
            rows[key] = row
            score[key] = score.get(key, 0.0) + 1 / (k + rank)
    return rows, score


def _walk(
    conn: Connection,
    request: SearchRequest,
    end: Any,
    filters: list,
    start: set[UUID],
) -> tuple[list[RowMapping], dict[UUID, int]]:
    frontier = start
    distance = dict.fromkeys(start, 0)
    rows = {}
    for step in range(1, request.graph_hops + 1):
        if not frontier:
            break
        found = (
            conn.execute(
                select(entity_edges, end.label("effective_invalid_at"))
                .where(
                    *filters,
                    or_(
                        entity_edges.c.source_id.in_(frontier),
                        entity_edges.c.target_id.in_(frontier),
                    ),
                )
                .order_by(entity_edges.c.asserted_at.desc(), entity_edges.c.id)
                .limit(CANDIDATE_LIMIT * 2)
            )
            .mappings()
            .all()
        )
        new = set()
        for row in found:
            rows[row["id"]] = row
            for key in (row["source_id"], row["target_id"]):
                if key not in distance:
                    distance[key] = step
                    new.add(key)
        frontier = new
    return list(rows.values()), distance


def _ordered(
    rows: dict[UUID, RowMapping],
    scores: dict[UUID, float],
    mode: str,
    distance: dict[UUID, int],
    vector: list[float],
) -> list[RowMapping]:
    def proximity(row: RowMapping) -> int:
        return min(distance.get(row["source_id"], 100), distance.get(row["target_id"], 100))

    if mode != "mmr" or not any(vector):

        def rank(key: UUID) -> tuple[int, float, UUID]:
            row = rows[key]
            if mode == "node_distance":
                priority = proximity(row)
            elif mode == "episode_mentions":
                priority = -len(row["supports"])
            else:
                priority = 0
            return priority, -scores[key], key

        return [rows[key] for key in sorted(rows, key=rank)]

    chosen = []
    remaining = set(rows)
    vectors = {
        key: [float(item) for item in row["fact_vector"]]
        for key, row in rows.items()
        if row["fact_vector"] is not None and len(row["fact_vector"]) == len(vector)
    }
    while remaining and len(chosen) < CANDIDATE_LIMIT:

        def mmr(key: UUID) -> tuple[float, UUID]:
            candidate = vectors.get(key)
            if candidate is None:
                return -scores[key], key
            redundancy = max(
                (_cosine(candidate, vectors[old]) for old in chosen if old in vectors), default=0
            )
            return -0.5 * (_cosine(vector, candidate) - redundancy), key

        key = min(remaining, key=mmr)
        chosen.append(key)
        remaining.remove(key)
    return [rows[key] for key in chosen]


def graph_matches(
    conn: Connection,
    service: "BoundService",
    request: SearchRequest,
    seeds: list[RowMapping],
    base_filters: list,
    terms: list[str],
    vector: list[float],
) -> list[GraphMatch]:
    if request.types is not None and "turn" not in request.types:
        return []
    scope = service.ctx
    model = service.settings.embedding.model
    token_filter = terms if "lexical" in request.channels else []
    node_filters = [visible_filter(entities, scope)]
    if request.as_of_recorded is not None:
        node_filters.append(entities.c.created_at <= request.as_of_recorded)
    node_lexical, node_semantic = _channels(
        conn,
        entities,
        node_filters,
        token_filter,
        vector,
        model,
        lambda row: f"{row['name']} {row['summary']}",
    )
    mention_rows = conn.execute(
        select(entity_mentions.c.entity_id, entity_mentions.c.episode_id).where(
            visible_filter(entity_mentions, scope),
            entity_mentions.c.episode_id.in_([seed["id"] for seed in seeds]),
        )
    ).all()
    seed_by_record = {seed["id"]: seed["memory_id"] for seed in seeds}
    seed_by_entity = {
        mention.entity_id: seed_by_record[mention.episode_id] for mention in reversed(mention_rows)
    }
    start = {row["id"] for row in node_lexical + node_semantic} | seed_by_entity.keys()
    end, period = _period(scope, request)
    edge_lexical, edge_semantic = _channels(
        conn,
        entity_edges,
        period,
        token_filter,
        vector,
        model,
        lambda row: row["fact"],
        (end.label("effective_invalid_at"),),
    )
    bfs, distance = _walk(conn, request, end, period, set(start))
    edge_rows, scores = _rrf([edge_lexical, edge_semantic, bfs], service.settings.search.rrf_k)
    ranked = _ordered(
        edge_rows, scores, service.settings.strategies.graph_reranker, distance, vector
    )
    episodic = {UUID(episode) for edge in ranked[:CANDIDATE_LIMIT] for episode in edge["supports"]}
    exact_mentions = conn.execute(
        select(entity_mentions.c.episode_id, entity_mentions.c.entity_id)
        .where(
            visible_filter(entity_mentions, scope),
            entity_mentions.c.entity_id.in_([row["id"] for row in node_lexical + node_semantic]),
        )
        .order_by(entity_mentions.c.asserted_at.desc(), entity_mentions.c.episode_id)
        .limit(CANDIDATE_LIMIT)
    ).all()
    episodic.update(item.episode_id for item in exact_mentions)
    episodes = {
        row["id"]: row
        for row in conn.execute(
            select(records).where(records.c.id.in_(episodic), *base_filters)
        ).mappings()
    }
    returned = set()
    result = []
    for edge in ranked[:CANDIDATE_LIMIT]:
        support = {
            UUID(episode): sources
            for episode, sources in edge["supports"].items()
            if UUID(episode) in episodes
        }
        reference = GraphFact(
            edge_id=edge["id"],
            relation_type=edge["relation_type"],
            fact=edge["fact"],
            valid_at=edge["valid_at"],
            invalid_at=edge["effective_invalid_at"],
            source_event_ids=sorted({UUID(key) for group in support.values() for key in group}),
            generation=edge["generation"],
            prompt_versions=edge["metadata"].get("prompt_versions", []),
            story_time=story_time(conn, scope, "edge", edge["id"])
            if request.as_of_story is not None
            else None,
        )
        seed = seed_by_entity.get(edge["source_id"]) or seed_by_entity.get(edge["target_id"])
        for episode_id in support:
            row = episodes[episode_id]
            if row["memory_id"] in returned:
                continue
            key = row["memory_id"]
            result.append(
                GraphMatch(row, [seed, key] if seed and seed != key else [key], reference)
            )
            returned.add(key)
    for mention in exact_mentions if request.as_of_story is None else []:
        row = episodes.get(mention.episode_id)
        if row is not None and row["memory_id"] not in returned:
            key = row["memory_id"]
            seed = seed_by_entity.get(mention.entity_id)
            result.append(GraphMatch(row, [seed, key] if seed and seed != key else [key]))
            returned.add(key)
    return result
