from collections.abc import Callable

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import Connection, Engine

from memfxxk.db import metadata

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
