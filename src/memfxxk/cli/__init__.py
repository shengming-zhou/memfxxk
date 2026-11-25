import asyncio
import secrets
from enum import StrEnum
from typing import Annotated

import typer
import uvicorn
from alembic import command
from alembic.config import Config

from memfxxk.auth import hash_key
from memfxxk.cli.connection import ClientSettings
from memfxxk.cli.memory import commands as memory_commands
from memfxxk.config import Settings
from memfxxk.db import api_keys, create_engine, transaction
from memfxxk.eval.cli import eval_cli
from memfxxk.ids import new_id
from memfxxk.jobs import Worker
from memfxxk.service import MemoryService

app = typer.Typer(no_args_is_help=True)
keys_app = typer.Typer(no_args_is_help=True)
db_app = typer.Typer(no_args_is_help=True)
index_app = typer.Typer(no_args_is_help=True)
app.add_typer(keys_app, name="keys")
app.add_typer(db_app, name="db")
app.add_typer(index_app, name="index")
app.add_typer(eval_cli, name="eval")
app.add_typer(memory_commands)


@app.callback()
def main(
    ctx: typer.Context,
    user: str | None = None,
    agent: str | None = None,
    project: str | None = None,
) -> None:
    if ctx.obj is None:
        overrides = {"user": user, "agent": agent, "project": project}
        ctx.obj = ClientSettings(
            **{key: value for key, value in overrides.items() if value is not None}
        )


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Run the API server."""
    uvicorn.run("memfxxk.api:create_app", factory=True, host=host, port=port)


@app.command("mcp")
def serve_mcp(ctx: typer.Context) -> None:
    """Expose scoped memory tools over MCP stdio."""
    from memfxxk.mcp_server import serve_stdio

    settings = Settings()
    with ctx.ensure_object(ClientSettings).connect() as client:
        asyncio.run(
            serve_stdio(
                client,
                enable_manage=settings.mcp_enable_manage,
                enable_host=settings.mcp_enable_host,
            )
        )


class KeyRole(StrEnum):
    ADMIN = "admin"
    HOST = "host"


@keys_app.command("create")
def create_key(
    org: Annotated[str, typer.Option()],
    role: Annotated[KeyRole, typer.Option()],
    name: Annotated[str, typer.Option()],
) -> None:
    """Create an API key and print its plaintext once."""
    if not org.strip() or not name.strip():
        raise typer.BadParameter("--org and --name must be nonempty")

    raw_key = secrets.token_urlsafe(32)
    engine = create_engine(Settings().database_url)
    try:
        with transaction(engine) as connection:
            connection.execute(
                api_keys.insert().values(
                    id=new_id(), org_id=org, key_hash=hash_key(raw_key), role=role.value, name=name
                )
            )
    finally:
        engine.dispose()
    typer.echo(raw_key)


@db_app.command("upgrade")
def upgrade_database() -> None:
    """Apply database migrations."""
    command.upgrade(Config("alembic.ini"), "head")


@index_app.command("ensure")
def ensure_index() -> None:
    """Create the HNSW index for the configured embedding model."""
    settings = Settings()
    engine = create_engine(settings.database_url)
    try:
        typer.echo(MemoryService(engine, settings).ensure_index())
    finally:
        engine.dispose()


@index_app.command("reembed")
def reembed(model: Annotated[str | None, typer.Option()] = None) -> None:
    """Queue embeddings for current records."""
    if model is not None and not model.strip():
        raise typer.BadParameter("--model must be nonempty")
    settings = Settings()
    engine = create_engine(settings.database_url)
    try:
        typer.echo(MemoryService(engine, settings).reembed(model))
    finally:
        engine.dispose()


@app.command("worker")
def run_worker() -> None:
    """Process background index jobs."""
    settings = Settings()
    engine = create_engine(settings.database_url)
    try:
        Worker(MemoryService(engine, settings)).run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        engine.dispose()
