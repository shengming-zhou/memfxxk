from collections.abc import Callable, Iterator
from pathlib import Path
from secrets import token_urlsafe
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.engine import Connection, Engine, make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.schema import CreateSchema, DropSchema

from memfxxk.api import create_app
from memfxxk.auth import hash_key
from memfxxk.config import EmbeddingSettings, Settings, WorkerSettings
from memfxxk.db import api_keys, create_engine, metadata
from memfxxk.domain import RequestContext
from memfxxk.ids import new_id
from memfxxk.service import MemoryService

TEST_ORG = "test-org"


def migration_config(connection: Connection) -> Config:
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.attributes["connection"] = connection
    config.attributes["version_table_schema"] = connection.scalar(select(func.current_schema()))
    return config


@pytest.fixture
def make_migration_config() -> Callable[[Connection], Config]:
    return migration_config


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    database_url = Settings().test_database_url
    schema = f"test_{uuid4().hex}"
    database = create_engine(database_url)
    try:
        with database.begin() as connection:
            connection.execute(CreateSchema(schema))
    except OperationalError as exc:
        database.dispose()
        pytest.fail(
            "PostgreSQL integration tests require an accessible test database. "
            "Run `docker compose up -d db`, or set MEMFXXK_TEST_DATABASE_URL. "
            f"Connection error: {exc.orig}",
            pytrace=False,
        )

    url = make_url(database_url)
    existing_options = url.query.get("options", "")
    scoped_url = url.update_query_dict(
        {"options": f"{existing_options} -csearch_path={schema},public".strip()}
    )
    scoped_engine = create_engine(scoped_url.render_as_string(hide_password=False))
    try:
        with scoped_engine.begin() as connection:
            command.upgrade(migration_config(connection), "head")
        yield scoped_engine
    finally:
        scoped_engine.dispose()
        try:
            with database.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True))
        finally:
            database.dispose()


@pytest.fixture(autouse=True)
def clean_integration_tables(request: pytest.FixtureRequest) -> Iterator[None]:
    if request.node.get_closest_marker("integration") is None:
        yield
        return

    engine: Engine = request.getfixturevalue("engine")
    try:
        yield
    finally:
        names = ", ".join(
            engine.dialect.identifier_preparer.format_table(table)
            for table in metadata.sorted_tables
        )
        with engine.begin() as connection:
            connection.exec_driver_sql(f"TRUNCATE TABLE {names}")


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        embedding=EmbeddingSettings(provider="fake"),
        token_counter="simple",
        worker=WorkerSettings(embedded=False),
    )


@pytest.fixture
def service(engine: Engine, test_settings: Settings) -> MemoryService:
    return MemoryService(engine, test_settings)


@pytest.fixture
def make_ctx() -> Callable[..., RequestContext]:
    def build(
        *,
        user: str | None = None,
        agent: str | None = None,
        project: str | None = None,
        org: str = TEST_ORG,
    ) -> RequestContext:
        return RequestContext(org_id=org, user_id=user, agent_id=agent, project_id=project)

    return build


@pytest.fixture
def api_client(engine: Engine, test_settings: Settings) -> Iterator[TestClient]:
    key = token_urlsafe(32)
    with engine.begin() as connection:
        connection.execute(
            api_keys.insert().values(
                id=new_id(), org_id=TEST_ORG, key_hash=hash_key(key), role="host", name="test"
            )
        )
    app = create_app(test_settings, engine=engine)
    with TestClient(
        app, headers={"Authorization": f"Bearer {key}", "X-Memfxxk-Agent": "test-agent"}
    ) as client:
        yield client
