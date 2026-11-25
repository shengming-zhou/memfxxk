"""Optional backends for evidence-only comparisons."""

from __future__ import annotations

import os
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from importlib.util import find_spec
from itertools import batched
from pathlib import Path
from typing import TYPE_CHECKING, Self

from memfxxk.ingest import _turn_part

if TYPE_CHECKING:
    from mem0 import Memory
    from qdrant_client import QdrantClient

    from memfxxk.embedding import EmbeddingProvider
    from memfxxk.eval.longmemeval.dataset import Question, Turn

MEM0_VERSION = "2.2.1"


@dataclass(frozen=True, slots=True)
class EvidenceHit:
    memory_id: str | None
    session_id: str | None
    text: str
    score: float | None


def render_evidence_turn(turn: Turn) -> str:
    """Apply the same turn formatting used by the memfxxk evidence index."""
    return "\n".join(
        _turn_part({"kind": "message", "role": item.role, "content": item.content, "payload": {}})
        for item in turn.messages
    )


class _SharedEmbedder:
    def __init__(self, embedding: EmbeddingProvider) -> None:
        self.embedding = embedding
        self.pending: dict[str, list[float]] = {}

    def embed(self, text: str, memory_action: str | None = None) -> list[float]:
        if memory_action == "add" and text in self.pending:
            return self.pending[text]
        return self.embedding.embed([text])[0]


class _DisabledChat:
    def generate_response(self, *_args: object, **_kwargs: object) -> None:
        raise RuntimeError("Chat model calls are disabled for the evidence-only Mem0 run")


def _open_mem0(work_dir: Path, embedding: EmbeddingProvider) -> tuple[Memory, QdrantClient]:
    try:
        installed = version("mem0ai")
    except PackageNotFoundError:
        installed = None
    if installed != MEM0_VERSION:
        raise RuntimeError(
            f"Mem0 evidence runs require the eval-only dependency mem0ai=={MEM0_VERSION}. "
            f"Run with uv run --locked --with mem0ai=={MEM0_VERSION}."
        )
    if any(find_spec(name) is not None for name in ("fastembed", "spacy")):
        raise RuntimeError(
            "Run the Mem0 evidence baseline in an environment without the optional "
            "fastembed or spaCy packages. Mem0 can download their models on first use."
        )
    # Mem0 reads both settings at import and creates its config directory.
    env = {"MEM0_DIR": str(work_dir.resolve()), "MEM0_TELEMETRY": "false"}
    prior = {key: os.environ.get(key) for key in env}
    try:
        os.environ.update(env)
        from mem0 import Memory
        from mem0.memory import telemetry
        from qdrant_client import QdrantClient

        if telemetry.MEM0_TELEMETRY:
            raise RuntimeError("Mem0 was already imported with telemetry enabled")
        client = QdrantClient(location=":memory:")
        disabled_endpoint = {
            "api_key": "offline-disabled",
            "openai_base_url": "http://127.0.0.1:1/v1",
        }
        try:
            memory = Memory.from_config(
                {
                    "vector_store": {
                        "provider": "qdrant",
                        "config": {
                            "collection_name": "longmemeval",
                            "embedding_model_dims": embedding.dim,
                            "client": client,
                            "path": str(work_dir.resolve()),
                        },
                    },
                    "embedder": {"provider": "openai", "config": disabled_endpoint},
                    "llm": {"provider": "openai", "config": disabled_endpoint},
                    "history_db_path": ":memory:",
                }
            )
        except BaseException:
            client.close()
            raise
    finally:
        for key, value in prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return memory, client


class Mem0EvidenceBackend:
    """Index raw, timestamp-scoped turns through Mem0 OSS add/search."""

    def __init__(self, embedding: EmbeddingProvider, work_dir: Path) -> None:
        self._memory, self._client = _open_mem0(work_dir, embedding)
        self._embedder = _SharedEmbedder(embedding)
        self._memory.embedding_model = self._embedder
        self._memory.llm = _DisabledChat()
        self._prepared: set[str] = set()
        self.manifest = {
            "version": MEM0_VERSION,
            "vector_store": "qdrant-local-in-memory",
            "qdrant_client_version": version("qdrant-client"),
            "mode": "raw-turn",
            "infer": False,
            "rerank": False,
            "keyword_search": False,
            "entity_extraction": False,
            "telemetry": False,
            "threshold": 0.0,
            "embedding_model": embedding.model,
            "embedding_dimensions": embedding.dim,
        }

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._memory.close()
        finally:
            self._client.close()

    def prepare(self, question: Question) -> None:
        from memfxxk.eval.longmemeval.dataset import iter_turns

        if question.question_id in self._prepared:
            return
        cutoff = question.asked_at
        eligible = (turn for turn in iter_turns(question) if turn.event_time <= cutoff)
        for batch in batched(eligible, 64):
            texts = [render_evidence_turn(turn) for turn in batch]
            unique_texts = list(dict.fromkeys(texts))
            vectors = self._embedder.embedding.embed(unique_texts)
            self._embedder.pending.update(zip(unique_texts, vectors, strict=True))
            try:
                for turn, text in zip(batch, texts, strict=True):
                    self._memory.add(
                        text,
                        user_id=question.question_id,
                        metadata={
                            "source_turn": str(turn.memory_id),
                            "session": turn.official_session_id,
                        },
                        infer=False,
                    )
            finally:
                self._embedder.pending.clear()
        self._prepared.add(question.question_id)

    def search(self, question: Question, k: int) -> list[EvidenceHit]:
        if question.question_id not in self._prepared:
            raise ValueError(f"Prepare Mem0 question {question.question_id} before searching")
        response = self._memory.search(
            question.question,
            filters={"user_id": question.question_id},
            top_k=k,
            threshold=0.0,
            rerank=False,
        )
        return [
            EvidenceHit(
                memory_id=item["metadata"]["source_turn"],
                session_id=item["metadata"]["session"],
                text=item["memory"],
                score=item["score"],
            )
            for item in response["results"]
        ]
