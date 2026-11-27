from uuid import UUID

from sqlalchemy import Connection, or_, select

from memfxxk.db import entities, entity_edges, entity_mentions
from memfxxk.manage.common import lexemes

from .ingest import normalized


def forget_source(conn: Connection, org_id: str, event_id: UUID) -> None:
    """Follow dependencies of an already-authorized erased event within its tenant."""
    edges = (
        conn.execute(
            select(entity_edges)
            .where(
                entity_edges.c.org_id == org_id,
                or_(
                    entity_edges.c.source_event_ids.contains([event_id]),
                    entity_edges.c.invalidation_event_ids.contains([event_id]),
                ),
            )
            .order_by(entity_edges.c.id)
            .with_for_update()
        )
        .mappings()
        .all()
    )
    for row in edges:
        edge = entity_edges.c.id == row["id"]
        sources = {key: ids for key, ids in row["supports"].items() if str(event_id) not in ids}
        if not sources:
            conn.execute(entity_edges.delete().where(edge, entity_edges.c.org_id == org_id))
            continue
        changes = {
            "supports": sources,
            "source_event_ids": sorted({UUID(key) for ids in sources.values() for key in ids}),
            "generation": row["generation"] + 1,
        }
        if event_id in row["invalidation_event_ids"]:
            changes.update(
                invalidation_event_ids=[],
                invalid_at=row["declared_invalid_at"],
                expired_at=None,
            )
        conn.execute(
            entity_edges.update().where(edge, entity_edges.c.org_id == org_id).values(**changes)
        )

    affected = conn.scalars(
        entity_mentions.delete()
        .where(
            entity_mentions.c.org_id == org_id,
            entity_mentions.c.source_event_ids.contains([event_id]),
        )
        .returning(entity_mentions.c.entity_id)
    ).all()
    for row in (
        conn.execute(
            select(entities)
            .where(
                entities.c.org_id == org_id,
                or_(
                    entities.c.id.in_(affected),
                    entities.c.summary_source_event_ids.contains([event_id]),
                ),
            )
            .order_by(entities.c.id)
            .with_for_update()
        )
        .mappings()
        .all()
    ):
        node = entities.c.id == row["id"]
        remaining = conn.execute(
            select(entity_mentions.c.name, entity_mentions.c.source_event_ids)
            .where(entity_mentions.c.entity_id == row["id"], entity_mentions.c.org_id == org_id)
            .order_by(entity_mentions.c.created_at, entity_mentions.c.episode_id)
        ).all()
        if not remaining:
            conn.execute(entities.delete().where(node, entities.c.org_id == org_id))
            continue
        name = (
            row["name"]
            if any(mention.name == row["name"] for mention in remaining)
            else remaining[0].name
        )
        changes = {
            "source_event_ids": sorted(
                {key for mention in remaining for key in mention.source_event_ids}
            ),
            "generation": row["generation"] + 1,
        }
        if name != row["name"]:
            changes.update(
                name=name,
                normalized_name=normalized(name),
                name_vector=None,
                embedding_model=None,
            )
        summary = row["summary"]
        if event_id in row["summary_source_event_ids"]:
            summary = ""
            changes.update(summary="", summary_source_event_ids=[])
        changes["lexemes"] = lexemes(f"{name}\n{summary}")
        conn.execute(entities.update().where(node, entities.c.org_id == org_id).values(**changes))
