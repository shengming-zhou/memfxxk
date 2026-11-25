from alembic import context
from sqlalchemy.engine import Connection

from memfxxk.config import Settings
from memfxxk.db import create_engine, metadata

config = context.config


def run_migrations_online(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=metadata,
        version_table_schema=config.attributes.get("version_table_schema"),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url") or Settings().database_url,
        target_metadata=metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
elif (connection := config.attributes.get("connection")) is not None:
    run_migrations_online(connection)
else:
    engine = create_engine(config.get_main_option("sqlalchemy.url") or Settings().database_url)
    try:
        with engine.connect() as connection:
            run_migrations_online(connection)
    finally:
        engine.dispose()
