import logging

from sqlalchemy import ColumnElement, Connection, and_, select

from memfxxk.db import qdrant_replicas, record_embeddings, records
from memfxxk.embedding import embedding_model_filter
from memfxxk.qdrant import QdrantClient, QdrantError
from memfxxk.retrieval.channels import Candidate, PgVectorIndex

logger = logging.getLogger(__name__)


class QdrantVectorIndex:
    def __init__(self, client: QdrantClient, fallback: PgVectorIndex) -> None:
        self.client = client
        self.fallback = fallback
        self.collection = client.collection(fallback.model, fallback.dim)

    def search(
        self,
        connection: Connection,
        vector: list[float],
        filters: list[ColumnElement[bool]],
        limit: int,
    ) -> list[Candidate]:
        scan_limit = self.client.settings.max_prefilter_ids
        eligible = connection.execute(
            select(records.c.id, records.c.generation, qdrant_replicas.c.generation)
            .join(record_embeddings, record_embeddings.c.record_id == records.c.id)
            .outerjoin(
                qdrant_replicas,
                and_(
                    qdrant_replicas.c.record_id == records.c.id,
                    qdrant_replicas.c.endpoint == self.client.endpoint,
                    qdrant_replicas.c.collection == self.collection,
                ),
            )
            .where(
                *filters,
                embedding_model_filter(self.fallback.model),
                record_embeddings.c.dim == self.fallback.dim,
            )
            .limit(scan_limit + 1)
        ).all()
        if not eligible:
            return []
        if len(eligible) > scan_limit or any(gen != indexed for _, gen, indexed in eligible):
            return self.fallback.search(connection, vector, filters, limit)

        generations = {record_id: generation for record_id, generation, _ in eligible}
        try:
            points = self.client.query(self.collection, vector, generations, limit)
        except QdrantError as exc:
            logger.warning("Qdrant candidate search failed, using pgvector: %s", exc)
        else:
            candidates = {
                point.id: Candidate(point.id, point.payload.generation, point.score)
                for point in points
                if point.payload.generation == generations.get(point.id)
            }
            if len(candidates) >= min(limit, len(generations)):
                return sorted(candidates.values(), key=lambda row: (-row.score, row.id))[:limit]
        return self.fallback.search(connection, vector, filters, limit)
