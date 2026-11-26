import asyncio
import json
import secrets
from enum import StrEnum
from typing import Annotated
from uuid import UUID

import typer
import uvicorn
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select

from memfxxk.auth import hash_key
from memfxxk.cli.connection import ClientSettings
from memfxxk.cli.memory import commands as memory_commands
from memfxxk.cli.workspace import commands as workspace_commands
from memfxxk.config import Settings
from memfxxk.db import api_keys, create_engine, project_key_grants, transaction
from memfxxk.domain import ProjectGrant
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
app.add_typer(workspace_commands, name="workspace")


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
    PROJECT = "project"


class ProjectAccess(StrEnum):
    READ = "read"
    WRITE = "write"


@keys_app.command("create")
def create_key(
    org: Annotated[str, typer.Option()],
    role: Annotated[KeyRole, typer.Option()],
    name: Annotated[str, typer.Option()],
    project: Annotated[str | None, typer.Option()] = None,
    user: Annotated[str | None, typer.Option()] = None,
    agent: Annotated[str | None, typer.Option()] = None,
    access: Annotated[ProjectAccess | None, typer.Option()] = None,
) -> None:
    """Create an API key and print its plaintext once."""
    if not org.strip() or not name.strip():
        raise typer.BadParameter("--org and --name must be nonempty")
    grant = None
    if role == KeyRole.PROJECT:
        if not project or not project.strip() or not user or not user.strip():
            raise typer.BadParameter("Project keys require --project and --user")
        if agent is not None and not agent.strip():
            raise typer.BadParameter("--agent must be nonempty when given")
        grant = ProjectGrant(
            project_id=project,
            user_id=user,
            agent_id=agent,
            access=(access or ProjectAccess.READ).value,
        )
    elif any(value is not None for value in (project, user, agent, access)):
        raise typer.BadParameter("Project identity and access options require --role project")

    raw_key = secrets.token_urlsafe(32)
    key_id = new_id()
    engine = create_engine(Settings().database_url)
    try:
        with transaction(engine) as connection:
            connection.execute(
                api_keys.insert().values(
                    id=key_id, org_id=org, key_hash=hash_key(raw_key), role=role.value, name=name
                )
            )
            if grant is not None:
                connection.execute(
                    project_key_grants.insert().values(
                        key_id=key_id,
                        **grant.model_dump(),
                    )
                )
    finally:
        engine.dispose()
    typer.echo(raw_key)


@keys_app.command("list")
def list_keys(org: Annotated[str, typer.Option()]) -> None:
    """List credential metadata for an organization."""
    engine = create_engine(Settings().database_url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                select(
                    api_keys.c.id,
                    api_keys.c.name,
                    api_keys.c.role,
                    api_keys.c.revoked_at,
                    project_key_grants.c.project_id,
                    project_key_grants.c.user_id,
                    project_key_grants.c.agent_id,
                    project_key_grants.c.access,
                )
                .select_from(api_keys.outerjoin(project_key_grants))
                .where(api_keys.c.org_id == org)
                .order_by(api_keys.c.name, api_keys.c.id)
            ).mappings()
            typer.echo(json.dumps([dict(row) for row in rows], default=str, indent=2))
    finally:
        engine.dispose()


@keys_app.command("revoke")
def revoke_key(key_id: UUID, org: Annotated[str, typer.Option()]) -> None:
    """Revoke a credential in the named organization."""
    engine = create_engine(Settings().database_url)
    try:
        with transaction(engine) as conn:
            revoked = conn.scalar(
                api_keys.update()
                .where(api_keys.c.id == key_id, api_keys.c.org_id == org)
                .values(revoked_at=func.coalesce(api_keys.c.revoked_at, func.now()))
                .returning(api_keys.c.id)
            )
        if revoked is None:
            typer.echo("Credential not found.", err=True)
            raise typer.Exit(1)
    finally:
        engine.dispose()
    typer.echo(str(revoked))


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


@index_app.command("retry-qdrant")
def retry_qdrant() -> None:
    """Requeue Qdrant writes and pending removals for the configured endpoint."""
    settings = Settings()
    engine = create_engine(settings.database_url)
    try:
        typer.echo(MemoryService(engine, settings).requeue_qdrant())
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
