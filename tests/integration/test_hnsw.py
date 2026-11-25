import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from typer.testing import CliRunner

from memfxxk.cli import app
from memfxxk.db import record_embeddings
from memfxxk.embedding import embedding_expression, embedding_model_filter

pytestmark = pytest.mark.integration


def test_index_ensure_is_idempotent_and_matches_nearest_neighbor_query(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = "test-model/v1"
    dimension = 8
    monkeypatch.setenv("MEMFXXK_DATABASE_URL", engine.url.render_as_string(hide_password=False))
    monkeypatch.setenv("MEMFXXK_EMBEDDING__MODEL", model)
    monkeypatch.setenv("MEMFXXK_EMBEDDING__DIM", str(dimension))

    for _ in range(2):
        result = CliRunner().invoke(app, ["index", "ensure"])
        assert result.exit_code == 0, result.output

    with engine.begin() as connection:
        index_name = connection.execute(
            text(
                "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema() "
                "AND tablename = 'record_embeddings' AND indexdef LIKE '%USING hnsw%'"
            )
        ).scalar_one()
        connection.exec_driver_sql("SET LOCAL enable_seqscan = off")
        query = (
            select(record_embeddings.c.record_id)
            .where(embedding_model_filter(model))
            .order_by(
                embedding_expression(dimension).cosine_distance([1.0] + [0.0] * (dimension - 1))
            )
            .limit(1)
        )
        statement = query.compile(
            dialect=connection.dialect, compile_kwargs={"literal_binds": True}
        )
        plan = connection.exec_driver_sql(f"EXPLAIN {statement}").scalars().all()
    assert any(index_name in line for line in plan), plan
