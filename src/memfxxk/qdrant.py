from __future__ import annotations

from collections.abc import Iterable
from hashlib import sha256
from typing import Any
from uuid import UUID

import httpx
from pydantic import BaseModel, FiniteFloat, StrictInt, TypeAdapter, ValidationError
from sqlalchemy import Connection, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine

from memfxxk.config import QdrantSettings
from memfxxk.db import jobs, qdrant_replicas, record_embeddings, records, transaction
from memfxxk.ids import derived_id
from memfxxk.jobs.queue import Job, enqueue, owned_claim

SYNC_KIND = "sync_qdrant"


class QdrantError(RuntimeError):
    pass


class _Payload(BaseModel):
    generation: StrictInt


class QdrantHit(BaseModel):
    id: UUID
    score: FiniteFloat
    payload: _Payload


_hits = TypeAdapter(list[QdrantHit])


class QdrantClient:
    def __init__(
        self, settings: QdrantSettings, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        self.settings = settings
        self.base_url = str(settings.base_url).rstrip("/")
        self.endpoint = sha256(self.base_url.encode()).hexdigest()
        self._transport = transport

    def collection(self, model: str, dim: int) -> str:
        digest = sha256(model.encode()).hexdigest()[:16]
        return f"{self.settings.collection_prefix}_{digest}_{dim}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        mutate_points: bool = False,
    ) -> httpx.Response:
        key = self.settings.api_key
        headers = {"api-key": key.get_secret_value()} if key is not None else {}
        params = {"wait": "true", "ordering": "strong"} if mutate_points else None
        try:
            with httpx.Client(
                base_url=self.base_url,
                headers=headers,
                timeout=self.settings.timeout,
                transport=self._transport,
            ) as client:
                return client.request(method, path, json=body, params=params)
        except httpx.RequestError as exc:
            raise QdrantError("Qdrant is unreachable") from exc

    @staticmethod
    def _result(response: httpx.Response) -> Any:
        if not response.is_success:
            raise QdrantError(f"Qdrant returned HTTP {response.status_code}")
        try:
            return response.json()["result"]
        except (ValueError, KeyError, TypeError) as exc:
            raise QdrantError("Qdrant returned an invalid response") from exc

    def ensure(self, collection: str, dim: int) -> None:
        path = f"collections/{collection}"
        response = self._request("GET", path)
        if response.status_code == 404:
            created = self._request(
                "PUT", path, body={"vectors": {"size": dim, "distance": "Cosine"}}
            )
            if created.is_success:
                if self._result(created) is not True:
                    raise QdrantError("Qdrant did not confirm collection creation")
                return
            # An independent worker can create the collection after the first GET.
            response = self._request("GET", path)
            if response.status_code == 404:
                self._result(created)
        try:
            vectors = self._result(response)["config"]["params"]["vectors"]
            if vectors["size"] != dim or vectors["distance"] != "Cosine":
                raise QdrantError("Qdrant collection uses a different vector configuration")
        except (KeyError, TypeError) as exc:
            raise QdrantError("Qdrant collection must use an unnamed dense vector") from exc

    def query(
        self, collection: str, vector: list[float], ids: Iterable[UUID], limit: int
    ) -> list[QdrantHit]:
        response = self._request(
            "POST",
            f"collections/{collection}/points/query",
            body={
                "query": vector,
                "limit": limit,
                "with_payload": ["generation"],
                "with_vector": False,
                "filter": {"must": [{"has_id": [str(record_id) for record_id in ids]}]},
            },
        )
        try:
            return _hits.validate_python(self._result(response)["points"])
        except (KeyError, TypeError, ValidationError) as exc:
            raise QdrantError("Qdrant returned invalid candidates") from exc

    def put(self, collection: str, record_id: UUID, generation: int, vector: list[float]) -> None:
        path = f"collections/{collection}/points"
        body = {
            "points": [
                {"id": str(record_id), "payload": {"generation": generation}, "vector": vector}
            ]
        }
        response = self._request("PUT", path, body=body, mutate_points=True)
        if response.status_code == 404:
            self.ensure(collection, len(vector))
            response = self._request("PUT", path, body=body, mutate_points=True)
        self._completed(response)

    def delete(self, collection: str, record_id: UUID) -> None:
        response = self._request(
            "POST",
            f"collections/{collection}/points/delete",
            body={"points": [str(record_id)]},
            mutate_points=True,
        )
        if response.status_code != 404:
            self._completed(response)

    def _completed(self, response: httpx.Response) -> None:
        result = self._result(response)
        if not isinstance(result, dict) or result.get("status") != "completed":
            raise QdrantError("Qdrant did not confirm the write")


def _enqueue_sync(conn: Connection, placement_id: UUID, endpoint: str) -> bool:
    return (
        enqueue(
            conn,
            SYNC_KIND,
            {"placement_id": str(placement_id), "endpoint": endpoint},
            dedupe_key=f"qdrant:{placement_id}",
        )
        is not None
    )


def queue_projection(
    conn: Connection, client: QdrantClient, record_id: UUID, model: str, dim: int
) -> None:
    collection = client.collection(model, dim)
    placement_id = derived_id("qdrant-replica", str(record_id), client.endpoint, collection)
    statement = insert(qdrant_replicas).values(
        id=placement_id,
        record_id=record_id,
        collection=collection,
        endpoint=client.endpoint,
        model=model,
        dim=dim,
    )
    conn.execute(
        statement.on_conflict_do_update(
            index_elements=[qdrant_replicas.c.id], set_={"generation": None}
        )
    )
    _enqueue_sync(conn, placement_id, client.endpoint)


def queue_removal(conn: Connection, record_ids: Iterable[UUID]) -> None:
    ids = list(record_ids)
    if ids:
        for row in conn.execute(
            select(qdrant_replicas.c.id, qdrant_replicas.c.endpoint)
            .where(qdrant_replicas.c.record_id.in_(ids))
            .order_by(qdrant_replicas.c.id)
        ):
            _enqueue_sync(conn, row.id, row.endpoint)


def requeue_endpoint(conn: Connection, client: QdrantClient) -> int:
    return sum(
        _enqueue_sync(conn, placement_id, client.endpoint)
        for placement_id in conn.scalars(
            select(qdrant_replicas.c.id)
            .where(qdrant_replicas.c.endpoint == client.endpoint)
            .order_by(qdrant_replicas.c.id)
        )
    )


def sync_projection(engine: Engine, client: QdrantClient, job: Job) -> None:
    placement_id = UUID(job.payload["placement_id"])
    with transaction(engine) as conn:
        target = (
            qdrant_replicas.c.id == placement_id,
            qdrant_replicas.c.endpoint == client.endpoint,
        )
        placement = conn.execute(select(qdrant_replicas).where(*target)).mappings().one_or_none()
        if placement is None:
            return
        # Serialize external writes with PostgreSQL updates, deletion and job lease recovery.
        record_id = placement["record_id"]
        row = conn.execute(
            select(records.c.id, records.c.generation)
            .where(records.c.id == record_id)
            .with_for_update()
        ).one_or_none()
        if conn.scalar(select(jobs.c.id).where(owned_claim(job)).with_for_update()) is None:
            return
        vector = (
            conn.scalar(
                select(record_embeddings.c.embedding).where(
                    record_embeddings.c.record_id == record_id,
                    record_embeddings.c.model == placement["model"],
                    record_embeddings.c.dim == placement["dim"],
                )
            )
            if row is not None
            else None
        )
        if vector is None or not any(vector):
            client.delete(placement["collection"], record_id)
            if row is None:
                conn.execute(qdrant_replicas.delete().where(*target))
            else:
                conn.execute(qdrant_replicas.update().where(*target).values(generation=None))
            return
        client.put(
            placement["collection"], record_id, row.generation, [float(value) for value in vector]
        )
        conn.execute(qdrant_replicas.update().where(*target).values(generation=row.generation))
