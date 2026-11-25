import re
from hashlib import sha256
from math import isfinite, sqrt
from time import sleep
from typing import Protocol

import httpx
from pgvector.sqlalchemy import Vector
from sqlalchemy import ColumnElement, Connection, Engine, cast, literal, select
from sqlalchemy.dialects.postgresql import insert

from memfxxk.config import EmbeddingSettings, Settings
from memfxxk.db import embedding_cache, record_embeddings
from memfxxk.text import MixedTokenizer, TokenCounter, Tokenizer, make_token_counter


class EmbeddingProvider(Protocol):
    model: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class FakeEmbeddingProvider:
    def __init__(
        self, model: str = "bge-m3", dim: int = 1024, tokenizer: Tokenizer | None = None
    ) -> None:
        if dim < 1:
            raise ValueError("Embedding dimension must be positive")
        self.model = model
        self.dim = dim
        self.tokenizer = tokenizer if tokenizer is not None else MixedTokenizer()

    def embed(self, texts: list[str]) -> list[list[float]]:
        embeddings = []
        for text in texts:
            vector = [0.0] * self.dim
            for token in self.tokenizer.tokenize(text):
                key = sha256(token.encode("utf-8")).digest()[:8]
                vector[int.from_bytes(key, "big") % self.dim] += 1
            norm = sqrt(sum(value * value for value in vector))
            embeddings.append([value / norm for value in vector] if norm else vector)
        return embeddings


def _truncate(text: str, limit: int, counter: TokenCounter) -> str:
    if counter.count(text) <= limit:
        return text
    low, high = 0, len(text)
    while high - low > 1:
        middle = (low + high) // 2
        if counter.count(text[:middle]) <= limit:
            low = middle
        else:
            high = middle
    return text[:low]


def _validate_embeddings(vectors: list[list[float]], expected_length: int, dim: int) -> None:
    if len(vectors) != expected_length or any(
        len(vector) != dim or not all(isfinite(value) for value in vector) for vector in vectors
    ):
        raise ValueError("Provider returned invalid embedding dimensions, count or values")


class OpenAICompatibleProvider:
    def __init__(self, config: EmbeddingSettings, token_counter: TokenCounter) -> None:
        self.config = config
        self.token_counter = token_counter
        self.model = config.model
        self.dim = config.dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        headers = {}
        if self.config.api_key is not None:
            headers["Authorization"] = f"Bearer {self.config.api_key.get_secret_value()}"
        batch_size = self.config.batch_size
        vectors: list[list[float]] = []
        with httpx.Client(headers=headers, timeout=self.config.timeout) as client:
            for start in range(0, len(texts), batch_size):
                batch = [
                    _truncate(text, self.config.max_input_tokens, self.token_counter)
                    for text in texts[start : start + batch_size]
                ]
                vectors.extend(self._embed_batch(client, batch))
        _validate_embeddings(vectors, len(texts), self.dim)
        return vectors

    def _embed_batch(self, client: httpx.Client, batch: list[str]) -> list[list[float]]:
        url = f"{self.config.base_url.rstrip('/')}/embeddings"
        for attempt in range(self.config.max_retries):
            response = client.post(url, json={"input": batch, "model": self.model})
            if response.status_code != 429 and not 500 <= response.status_code < 600:
                break
            sleep(2**attempt)
        else:
            response = client.post(url, json={"input": batch, "model": self.model})
        response.raise_for_status()
        data = sorted(response.json()["data"], key=lambda item: item["index"])
        if [item["index"] for item in data] != list(range(len(batch))):
            raise ValueError("Provider returned invalid embedding indexes")
        return [item["embedding"] for item in data]


def make_embedding_provider(settings: Settings) -> EmbeddingProvider:
    config = settings.embedding
    if config.provider == "fake":
        return FakeEmbeddingProvider(config.model, config.dim)
    return OpenAICompatibleProvider(config, make_token_counter(settings.token_counter))


class CachedEmbedder:
    def __init__(self, engine: Engine, provider: EmbeddingProvider) -> None:
        self.engine = engine
        self.provider = provider
        self.model = provider.model
        self.dim = provider.dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        keys = [sha256(text.encode("utf-8")).hexdigest() for text in texts]
        distinct = dict(zip(keys, texts, strict=True))
        query = select(embedding_cache.c.text_sha256, embedding_cache.c.embedding).where(
            embedding_cache.c.model == self.model,
            embedding_cache.c.dim == self.dim,
            embedding_cache.c.text_sha256.in_(distinct),
        )
        with self.engine.connect() as connection:
            vectors = {
                key: [float(value) for value in vector] for key, vector in connection.execute(query)
            }

        misses = [key for key in distinct if key not in vectors]
        if misses:
            embedded = self.provider.embed([distinct[key] for key in misses])
            _validate_embeddings(embedded, len(misses), self.dim)
            vectors.update(zip(misses, embedded, strict=True))
            statement = insert(embedding_cache)
            upsert = statement.on_conflict_do_update(
                index_elements=[embedding_cache.c.model, embedding_cache.c.text_sha256],
                set_={"dim": statement.excluded.dim, "embedding": statement.excluded.embedding},
                where=embedding_cache.c.dim != statement.excluded.dim,
            )
            # A consistent ordering avoids conflicting lock order when batches overlap.
            with self.engine.begin() as connection:
                connection.execute(
                    upsert,
                    [
                        dict(
                            model=self.model, text_sha256=key, dim=self.dim, embedding=vectors[key]
                        )
                        for key in sorted(misses)
                    ],
                )
        return [vectors[key] for key in keys]


def embedding_expression(dim: int) -> ColumnElement:
    if type(dim) is not int or dim < 1:
        raise ValueError("Embedding dimension must be a positive integer")
    return cast(record_embeddings.c.embedding, Vector(dim))


def embedding_model_filter(model: str) -> ColumnElement[bool]:
    return record_embeddings.c.model == literal(model, literal_execute=True)


def ensure_embedding_index(connection: Connection, model: str, dim: int) -> str:
    expression = embedding_expression(dim)
    slug = re.sub(r"[^a-z0-9]+", "_", model.lower()).strip("_")[:20] or "model"
    digest = sha256(model.encode("utf-8")).hexdigest()[:12]
    name = f"ix_embedding_hnsw_{slug}_{digest}_{dim}"
    prepare = connection.dialect.identifier_preparer
    compile_options = {"literal_binds": True, "include_table": False}
    indexed = expression.compile(dialect=connection.dialect, compile_kwargs=compile_options)
    predicate = embedding_model_filter(model).compile(
        dialect=connection.dialect, compile_kwargs=compile_options
    )
    connection.exec_driver_sql(
        f"CREATE INDEX IF NOT EXISTS {prepare.quote(name)} "
        f"ON {prepare.format_table(record_embeddings)} USING hnsw "
        f"(({indexed}) vector_cosine_ops) WHERE {predicate}"
    )
    return name
