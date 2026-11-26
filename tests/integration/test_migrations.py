from collections.abc import Callable
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError

from memfxxk.db import metadata, qdrant_replicas

pytestmark = pytest.mark.integration


def test_initial_migration_can_be_downgraded_and_reapplied(
    engine: Engine, make_migration_config: Callable[[Connection], Config]
) -> None:
    with engine.begin() as connection:
        config = make_migration_config(connection)
        schema = config.attributes["version_table_schema"]
        expected_tables = set(metadata.tables)

        assert set(inspect(connection).get_table_names(schema)) == expected_tables | {
            "alembic_version"
        }
        command.downgrade(config, "base")
        assert set(inspect(connection).get_table_names(schema)) == {"alembic_version"}
        command.upgrade(config, "head")
        assert set(inspect(connection).get_table_names(schema)) == expected_tables | {
            "alembic_version"
        }


def test_qdrant_downgrade_protects_tracked_remote_points(
    engine: Engine, make_migration_config: Callable[[Connection], Config]
) -> None:
    placement = uuid4()
    with engine.begin() as connection:
        connection.execute(
            qdrant_replicas.insert().values(
                id=placement,
                record_id=uuid4(),
                endpoint="http://qdrant.local",
                collection="test_collection",
                model="fake",
                dim=8,
                generation=1,
            )
        )
    with pytest.raises(DBAPIError, match="Remove Qdrant collections"):
        with engine.begin() as connection:
            command.downgrade(make_migration_config(connection), "0003")

    with engine.begin() as connection:
        assert connection.scalar(select(qdrant_replicas.c.id)) == placement
        connection.execute(qdrant_replicas.delete().where(qdrant_replicas.c.id == placement))
        config = make_migration_config(connection)
        command.downgrade(config, "0003")
        command.upgrade(config, "head")
