from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any
from unicodedata import normalize
from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy import Connection, Select, cast, func, select, tuple_
from sqlalchemy.dialects.postgresql import TSQUERY, insert
from sqlalchemy.engine import RowMapping

from memfxxk.db import entities, entity_edges, entity_mentions, jobs, records, transaction
from memfxxk.domain import RequestContext, Scope
from memfxxk.embedding import CachedEmbedder
from memfxxk.ids import derived_id
from memfxxk.jobs.queue import Job, owned_claim
from memfxxk.llm.runner import TaskRunner
from memfxxk.llm.settings import Task
from memfxxk.m2.facts import _source_query
from memfxxk.manage.common import lexemes, lock_user_operations
from memfxxk.prompts import edge_extract, edge_resolve, entity_extract, entity_resolve, node_summary
from memfxxk.scope import visible_filter
from memfxxk.text import MixedTokenizer, build_tsquery_literal

if TYPE_CHECKING:
    from memfxxk.service import MemoryService

RECENT_EPISODES = 4
CANDIDATE_LIMIT = 10
MIN_SIMILARITY = 0.6


def normalized(value: str) -> str:
    return " ".join(normalize("NFKC", value).casefold().split())


@dataclass(frozen=True, slots=True)
class Episode:
    row: RowMapping
    ctx: RequestContext
    messages: list[RowMapping]
    earlier: list[RowMapping]

    @property
    def observed_at(self) -> datetime:
        return self.row["event_time"]

    @property
    def source_ids(self) -> set[UUID]:
        return {row["id"] for row in self.messages}


@dataclass(frozen=True, slots=True)
class Node:
    id: UUID
    mention: entity_extract.Mention
    vector: list[float]
    prior: RowMapping | None = None

    @property
    def canonical_name(self) -> str:
        return self.prior["name"] if self.prior is not None else self.mention.name.strip()


@dataclass(frozen=True, slots=True)
class Edge:
    draft: edge_extract.Relationship
    source: UUID
    target: UUID
    vector: list[float]
    duplicate: RowMapping | None
    contradicted: list[RowMapping] = field(default_factory=list)


def _turn(job: Job, scope: Scope) -> Select:
    return select(records).where(
        visible_filter(records, scope),
        records.c.id == UUID(job.payload["record_id"]),
        records.c.generation == job.payload["generation"],
        records.c.is_current,
        records.c.status == "active",
        records.c.type == "turn",
    )


def _load_episode(service: MemoryService, job: Job) -> Episode | None:
    scope = Scope.model_validate(job.payload["scope"])
    ctx = RequestContext(**scope.model_dump(), actor={"type": "system"})
    with service.engine.connect() as conn:
        row = conn.execute(_turn(job, ctx)).mappings().one_or_none()
        if row is None:
            return None
        revealed = list(conn.execute(_source_query(scope, row["source_event_ids"])).mappings())
        if len(revealed) != len(row["source_event_ids"]):
            return None
        messages = [item for item in revealed if item["kind"] == "message" and item["content"]]
        if not messages or messages[-1]["role"] != "assistant":
            return None
        earlier = (
            conn.execute(
                select(records.c.content, records.c.event_time, records.c.source_event_ids)
                .where(
                    visible_filter(records, ctx),
                    *_exact_scope(records, ctx),
                    records.c.type == "turn",
                    records.c.is_current,
                    records.c.status == "active",
                    tuple_(records.c.event_time, records.c.id)
                    < tuple_(row["event_time"], row["id"]),
                )
                .order_by(records.c.event_time.desc(), records.c.id.desc())
                .limit(RECENT_EPISODES)
            )
            .mappings()
            .all()
        )
    return Episode(row, ctx, messages, list(reversed(earlier)))


def _exact_scope(table: Any, ctx: Scope) -> list:
    return [table.c[name].is_not_distinct_from(getattr(ctx, name)) for name in Scope.model_fields]


def _episode_data(episode: Episode) -> dict[str, Any]:
    return {
        "reference_time": episode.observed_at.isoformat(),
        "current_episode": [
            {"id": str(row["id"]), "role": row["role"], "text": row["content"]}
            for row in episode.messages
        ],
        "earlier_episodes": [
            {"time": row["event_time"].isoformat(), "text": row["content"]}
            for row in episode.earlier
        ],
    }


def _call[T](
    service: MemoryService,
    job: Job,
    episode: Episode,
    task: Task,
    prompt: str,
    version: str,
    schema: TypeAdapter[T],
    data: dict[str, Any],
    extra_sources: set[UUID] | None = None,
) -> T:
    sources = episode.source_ids | {
        key for row in episode.earlier for key in row["source_event_ids"]
    }
    result = TaskRunner.for_service(service, task).complete(
        task,
        episode.ctx,
        [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
        ],
        schema=schema,
        prompt_version=version,
        source_event_ids=sorted(sources | (extra_sources or set())),
        job_id=job.id,
    )
    assert result.parsed is not None
    return result.parsed


def _entity_candidates(
    conn: Connection,
    ctx: RequestContext,
    name: str,
    vector: list[float],
    model: str,
) -> list[RowMapping]:
    base = select(entities).where(visible_filter(entities, ctx), *_exact_scope(entities, ctx))
    lexical_terms = build_tsquery_literal(dict.fromkeys(MixedTokenizer().tokenize(name)))
    exact = (
        conn.execute(
            base.where(entities.c.normalized_name == normalized(name))
            .order_by(entities.c.created_at, entities.c.id)
            .limit(CANDIDATE_LIMIT)
        )
        .mappings()
        .all()
    )
    textual = []
    if lexical_terms:
        query = cast(lexical_terms, TSQUERY)
        textual = (
            conn.execute(
                base.where(entities.c.lexemes.op("@@")(query))
                .order_by(func.ts_rank_cd(entities.c.lexemes, query).desc(), entities.c.id)
                .limit(CANDIDATE_LIMIT)
            )
            .mappings()
            .all()
        )
    distance = entities.c.name_vector.cosine_distance(vector)
    semantic = (
        conn.execute(
            base.where(
                entities.c.embedding_model == model,
                func.vector_dims(entities.c.name_vector) == len(vector),
                distance <= 1 - MIN_SIMILARITY,
            )
            .order_by(distance, entities.c.id)
            .limit(CANDIDATE_LIMIT)
        )
        .mappings()
        .all()
    )
    merged = {row["id"]: row for row in [*exact, *textual, *semantic]}
    return list(merged.values())[:CANDIDATE_LIMIT]


def _nodes(
    service: MemoryService,
    job: Job,
    episode: Episode,
    embedder: CachedEmbedder,
    input_data: dict[str, Any],
) -> dict[int, Node]:
    response = _call(
        service,
        job,
        episode,
        "entity_extract",
        entity_extract.PROMPT,
        entity_extract.VERSION,
        entity_extract.SCHEMA,
        input_data,
    )
    kept = {
        i: mention
        for i, mention in enumerate(response.entities)
        if normalized(mention.name) and set(mention.source_event_ids) <= episode.source_ids
    }
    if not kept:
        return {}
    model = embedder.model
    vectors = dict(zip(kept, embedder.embed([item.name for item in kept.values()]), strict=True))
    with service.engine.connect() as conn:
        related = {
            index: _entity_candidates(conn, episode.ctx, mention.name, vectors[index], model)
            for index, mention in kept.items()
        }
    existing = {row["id"]: row for candidates in related.values() for row in candidates}
    stable_ids = {key: i for i, key in enumerate(existing)}
    matches = {}
    if existing:
        resolutions = _call(
            service,
            job,
            episode,
            "entity_resolve",
            entity_resolve.PROMPT,
            entity_resolve.VERSION,
            entity_resolve.SCHEMA,
            {
                **input_data,
                "extracted_nodes": [
                    {
                        "id": index,
                        "name": mention.name,
                        "entity_type": mention.entity_type,
                        "candidates": [
                            {
                                "candidate_id": stable_ids[row["id"]],
                                "name": row["name"],
                                "entity_type": row["entity_type"],
                                "summary": row["summary"],
                            }
                            for row in related[index]
                        ],
                    }
                    for index, mention in kept.items()
                ],
            },
            {
                key
                for row in existing.values()
                for key in [*row["source_event_ids"], *row["summary_source_event_ids"]]
            },
        )
        for resolution in resolutions.resolutions:
            for item in related.get(resolution.id, []):
                if stable_ids[item["id"]] == resolution.duplicate_candidate_id:
                    matches[resolution.id] = item
                    break
    nodes = {}
    for index, mention in kept.items():
        prior = matches.get(index)
        new_id = derived_id(
            "temporal-entity",
            str(episode.row["id"]),
            normalized(mention.name),
            normalized(mention.entity_type),
            str(mention.source_event_ids[0]),
        )
        nodes[index] = Node(
            prior["id"] if prior is not None else new_id, mention, vectors[index], prior
        )
    return nodes


def _edge_data(row: RowMapping, index: int) -> dict[str, Any]:
    return {
        "idx": index,
        "fact": row["fact"],
        "relation_type": row["relation_type"],
        "valid_at": (row["valid_at"] or row["asserted_at"]).isoformat(),
        "invalid_at": row["invalid_at"].isoformat() if row["invalid_at"] else None,
    }


def _edge_candidates(
    service: MemoryService,
    ctx: RequestContext,
    source: UUID,
    target: UUID,
    relation_type: str,
) -> tuple[list[RowMapping], list[RowMapping]]:
    with service.engine.connect() as conn:
        base = (
            select(entity_edges)
            .where(visible_filter(entity_edges, ctx), *_exact_scope(entity_edges, ctx))
            .order_by(entity_edges.c.asserted_at.desc(), entity_edges.c.id)
            .limit(CANDIDATE_LIMIT)
        )
        pair = (
            conn.execute(
                base.where(
                    entity_edges.c.source_id == source,
                    entity_edges.c.target_id == target,
                    entity_edges.c.relation_type == relation_type,
                )
            )
            .mappings()
            .all()
        )
        competing = (
            conn.execute(
                base.where(
                    entity_edges.c.source_id == source,
                    entity_edges.c.target_id != target,
                    entity_edges.c.relation_type == relation_type,
                )
            )
            .mappings()
            .all()
        )
    return list(pair), list(competing)


def _edges(
    service: MemoryService,
    job: Job,
    episode: Episode,
    nodes: dict[int, Node],
    embedder: CachedEmbedder,
    input_data: dict[str, Any],
) -> list[Edge]:
    response = _call(
        service,
        job,
        episode,
        "edge_extract",
        edge_extract.PROMPT,
        edge_extract.VERSION,
        edge_extract.SCHEMA,
        {
            **input_data,
            "entities": [
                {"id": i, "name": node.mention.name, "canonical_name": node.canonical_name}
                for i, node in nodes.items()
            ],
        },
        {
            key
            for node in nodes.values()
            if node.prior is not None
            for key in node.prior["source_event_ids"]
        },
    )
    drafts = [
        draft
        for draft in response.edges
        if draft.source in nodes
        and draft.target in nodes
        and nodes[draft.source].id != nodes[draft.target].id
        and normalized(draft.fact)
        and set(draft.source_event_ids) <= episode.source_ids
        and (draft.invalid_at is None or draft.invalid_at > (draft.valid_at or episode.observed_at))
    ]
    embedded = embedder.embed([draft.fact for draft in drafts])
    plans = []
    for draft, vector in zip(drafts, embedded, strict=True):
        source, target = nodes[draft.source].id, nodes[draft.target].id
        pair, competing = _edge_candidates(
            service, episode.ctx, source, target, draft.relation_type
        )
        duplicate = next(
            (
                row
                for row in pair
                if normalized(row["fact"]) == normalized(draft.fact)
                and row["declared_invalid_at"] == draft.invalid_at
                and (draft.valid_at is None or row["valid_at"] == draft.valid_at)
            ),
            None,
        )
        contradicted = []
        if duplicate is None and (pair or competing):
            all_edges = [*pair, *competing]
            resolution = _call(
                service,
                job,
                episode,
                "edge_resolve",
                edge_resolve.PROMPT,
                edge_resolve.VERSION,
                edge_resolve.SCHEMA,
                {
                    "new_edge": draft.model_dump(mode="json"),
                    "same_pair": [_edge_data(row, i) for i, row in enumerate(pair)],
                    "competing_state": [
                        _edge_data(row, i) for i, row in enumerate(competing, start=len(pair))
                    ],
                },
                {
                    key
                    for row in all_edges
                    for key in [*row["source_event_ids"], *row["invalidation_event_ids"]]
                },
            )
            duplicate = next(
                (pair[i] for i in resolution.duplicate_facts if 0 <= i < len(pair)), None
            )
            contradicted = [
                all_edges[i]
                for i in dict.fromkeys(resolution.contradicted_facts)
                if 0 <= i < len(all_edges) and all_edges[i] is not duplicate
            ]
        plans.append(Edge(draft, source, target, vector, duplicate, contradicted))
    return plans


def _summaries(
    service: MemoryService,
    job: Job,
    episode: Episode,
    nodes: dict[int, Node],
    edges: list[Edge],
    input_data: dict[str, Any],
) -> dict[int, node_summary.Summary]:
    older_sources = {
        key
        for node in nodes.values()
        if node.prior is not None
        for key in node.prior["summary_source_event_ids"]
    }
    known_sources = episode.source_ids | older_sources
    response = _call(
        service,
        job,
        episode,
        "node_summary",
        node_summary.PROMPT,
        node_summary.VERSION,
        node_summary.SCHEMA,
        {
            **input_data,
            "entities": [
                {
                    "id": i,
                    "name": node.canonical_name,
                    "prior_summary": node.prior["summary"] if node.prior is not None else "",
                    "prior_source_event_ids": [
                        str(key) for key in node.prior["summary_source_event_ids"]
                    ]
                    if node.prior is not None
                    else [],
                }
                for i, node in nodes.items()
            ],
            "new_edges": [edge.draft.model_dump(mode="json") for edge in edges],
        },
        older_sources,
    )
    return {
        summary.id: summary
        for summary in response.summaries
        if summary.id in nodes and set(summary.source_event_ids) <= known_sources
    }


def _adopt_node(
    conn: Connection,
    episode: Episode,
    node: Node,
    summary: node_summary.Summary | None,
    model: str,
) -> None:
    scope = episode.ctx.model_dump(exclude={"actor"})
    if node.prior is None:
        conn.execute(
            insert(entities)
            .values(
                id=node.id,
                **scope,
                name=node.canonical_name,
                normalized_name=normalized(node.canonical_name),
                entity_type=normalized(node.mention.entity_type),
                name_vector=node.vector,
                embedding_model=model,
                lexemes=lexemes(node.canonical_name),
                source_event_ids=list(dict.fromkeys(node.mention.source_event_ids)),
            )
            .on_conflict_do_nothing()
        )
    mention = insert(entity_mentions).values(
        **scope,
        entity_id=node.id,
        episode_id=episode.row["id"],
        name=node.mention.name.strip(),
        source_event_ids=list(dict.fromkeys(node.mention.source_event_ids)),
        asserted_at=episode.observed_at,
    )
    conn.execute(
        mention.on_conflict_do_update(
            index_elements=[entity_mentions.c.entity_id, entity_mentions.c.episode_id],
            set_={
                "source_event_ids": mention.excluded.source_event_ids,
                "name": mention.excluded.name,
            },
        )
    )
    current = (
        conn.execute(
            select(entities)
            .where(visible_filter(entities, episode.ctx), entities.c.id == node.id)
            .with_for_update()
        )
        .mappings()
        .one()
    )
    if node.prior is not None and current["generation"] != node.prior["generation"]:
        raise RuntimeError("Graph entity changed during extraction")
    changes: dict[str, Any] = {
        "source_event_ids": list(
            dict.fromkeys([*current["source_event_ids"], *node.mention.source_event_ids])
        ),
        "generation": current["generation"] + 1,
    }
    if summary is not None:
        changes.update(
            summary=summary.summary.strip(),
            summary_source_event_ids=list(dict.fromkeys(summary.source_event_ids)),
            lexemes=lexemes(f"{current['name']}\n{summary.summary}"),
        )
    conn.execute(
        entities.update()
        .where(visible_filter(entities, episode.ctx), entities.c.id == node.id)
        .values(**changes)
    )


def _adopt_edge(conn: Connection, episode: Episode, edge: Edge, model: str) -> None:
    draft = edge.draft
    key = str(episode.row["id"])
    new_start = draft.valid_at or episode.observed_at
    earliest_stop = draft.invalid_at
    expires_from: list[UUID] = []
    for candidate in edge.contradicted:
        old = _current_edge(conn, episode.ctx, candidate["id"])
        old_start = old["valid_at"] or old["asserted_at"]
        old_stop = old["invalid_at"]
        if old_stop is not None and old_stop <= new_start:
            continue
        if earliest_stop is not None and earliest_stop <= old_start:
            continue
        if old_start > new_start:
            earliest_stop = old_start
            expires_from = list(old["source_event_ids"])
            continue
        conn.execute(
            entity_edges.update()
            .where(
                visible_filter(entity_edges, episode.ctx),
                entity_edges.c.id == old["id"],
            )
            .values(
                invalid_at=new_start,
                expired_at=func.now(),
                invalidation_event_ids=draft.source_event_ids,
                generation=old["generation"] + 1,
            )
        )
    supports = {key: [str(source_id) for source_id in dict.fromkeys(draft.source_event_ids)]}
    if edge.duplicate is not None:
        old = _current_edge(conn, episode.ctx, edge.duplicate["id"])
        conn.execute(
            entity_edges.update()
            .where(
                visible_filter(entity_edges, episode.ctx),
                entity_edges.c.id == old["id"],
            )
            .values(
                source_event_ids=list(
                    dict.fromkeys([*old["source_event_ids"], *draft.source_event_ids])
                ),
                supports={
                    **old["supports"],
                    key: list(dict.fromkeys([*old["supports"].get(key, []), *supports[key]])),
                },
                generation=old["generation"] + 1,
            )
        )
        return
    scope = episode.ctx.model_dump(exclude={"actor"})
    edge_id = derived_id(
        "temporal-edge",
        key,
        str(edge.source),
        str(edge.target),
        draft.relation_type,
        draft.fact,
        new_start.isoformat(),
    )
    conn.execute(
        insert(entity_edges)
        .values(
            id=edge_id,
            **scope,
            source_id=edge.source,
            target_id=edge.target,
            relation_type=draft.relation_type,
            fact=draft.fact.strip(),
            fact_vector=edge.vector,
            embedding_model=model,
            lexemes=lexemes(draft.fact),
            valid_at=draft.valid_at,
            invalid_at=earliest_stop,
            declared_invalid_at=draft.invalid_at,
            asserted_at=episode.observed_at,
            expired_at=func.now() if expires_from else None,
            source_event_ids=list(dict.fromkeys(draft.source_event_ids)),
            supports=supports,
            invalidation_event_ids=expires_from,
        )
        .on_conflict_do_nothing()
    )


def _current_edge(conn: Connection, ctx: RequestContext, edge_id: UUID) -> RowMapping:
    return (
        conn.execute(
            select(entity_edges).where(
                visible_filter(entity_edges, ctx), entity_edges.c.id == edge_id
            )
        )
        .mappings()
        .one()
    )


def _check_edge_versions(conn: Connection, ctx: RequestContext, edges: list[Edge]) -> None:
    expected = {
        row["id"]: row["generation"]
        for edge in edges
        for row in [*edge.contradicted, edge.duplicate]
        if row is not None
    }
    if not expected:
        return
    current = dict(
        conn.execute(
            select(entity_edges.c.id, entity_edges.c.generation)
            .where(visible_filter(entity_edges, ctx), entity_edges.c.id.in_(expected))
            .order_by(entity_edges.c.id)
            .with_for_update()
        ).all()
    )
    if current != expected:
        raise RuntimeError("Graph relation changed during extraction")


def project_episode(service: MemoryService, job: Job, embedder: CachedEmbedder) -> None:
    episode = _load_episode(service, job)
    if episode is None:
        return
    data = _episode_data(episode)
    nodes = _nodes(service, job, episode, embedder, data)
    if not nodes:
        return
    edges = _edges(service, job, episode, nodes, embedder, data)
    summaries = _summaries(service, job, episode, nodes, edges, data)
    with transaction(service.engine) as conn:
        lock_user_operations(conn, episode.ctx)
        if conn.execute(_turn(job, episode.ctx).with_for_update()).first() is None:
            return
        if conn.scalar(select(jobs.c.id).where(owned_claim(job)).with_for_update()) is None:
            return
        _check_edge_versions(conn, episode.ctx, edges)
        represented = set()
        for index, node in nodes.items():
            if node.id in represented:
                continue
            _adopt_node(conn, episode, node, summaries.get(index), embedder.model)
            represented.add(node.id)
        for edge in edges:
            _adopt_edge(conn, episode, edge, embedder.model)
