from uuid import UUID

from sqlalchemy import ColumnElement, Connection, case, or_, select
from sqlalchemy.engine import RowMapping

from memfxxk.db import events, memory_relations, records
from memfxxk.domain import Scope
from memfxxk.domain.retrieval import SearchRequest
from memfxxk.scope import visible_filter

_SEED_LIMIT = 4
_NEIGHBOR_LIMIT = 12
_CANDIDATE_LIMIT = 24


def graph_neighbors(
    conn: Connection,
    ctx: Scope,
    request: SearchRequest,
    seeds: list[RowMapping],
    filters: list[ColumnElement[bool]],
) -> list[tuple[RowMapping, list[UUID]]]:
    frontier = [(row, [row["memory_id"]]) for row in seeds[:_SEED_LIMIT]]
    visited = {path[0] for _, path in frontier}
    found: list[tuple[RowMapping, list[UUID]]] = []
    for _ in range(request.graph_hops):
        following = []
        for row, path in frontier:
            for neighbor in _neighbors(conn, ctx, request, row, filters, visited):
                memory_id = neighbor["memory_id"]
                if memory_id in visited:
                    continue
                visited.add(memory_id)
                following.append((neighbor, [*path, memory_id]))
                if len(found) + len(following) == _CANDIDATE_LIMIT:
                    return [*found, *following]
        found.extend(following)
        frontier = following
    return found


def _neighbors(
    conn: Connection,
    ctx: Scope,
    request: SearchRequest,
    row: RowMapping,
    filters: list[ColumnElement[bool]],
    visited: set[UUID],
) -> list[RowMapping]:
    edges = memory_relations.c
    source = row["memory_id"]
    endpoint = case(
        (edges.from_memory_id == source, edges.to_memory_id), else_=edges.from_memory_id
    )
    links = (
        select(endpoint)
        .select_from(memory_relations.join(events, events.c.id == edges.source_event_id))
        .where(
            edges.org_id == ctx.org_id,
            or_(edges.from_memory_id == source, edges.to_memory_id == source),
            visible_filter(events, ctx),
            events.c.redacted.is_(False),
        )
    )
    if request.as_of_recorded is not None:
        links = links.where(edges.created_at <= request.as_of_recorded)
    explicit = records.c.memory_id.in_(links)
    entities = row["metadata"].get("entities") or []
    shared = [
        records.c.metadata["entities"].contains([entity])
        for entity in entities[:8]
        if isinstance(entity, str)
    ]
    statement = (
        select(records)
        .where(*filters, records.c.memory_id.not_in(visited), or_(explicit, *shared))
        .order_by(explicit.desc(), records.c.event_time.desc(), records.c.id)
        .limit(_NEIGHBOR_LIMIT)
    )
    return list(conn.execute(statement).mappings())
