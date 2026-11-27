import os
import shlex
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated
from uuid import UUID

import typer
from pydantic import BaseModel

from memfxxk.cli.connection import ClientSettings, open_client
from memfxxk.domain.context import BuildContextRequest
from memfxxk.domain.documents import PutDocumentRequest
from memfxxk.domain.manage import (
    LinkRequest,
    ListRequest,
    Mark,
    PatchRequest,
    RememberRequest,
)
from memfxxk.domain.retrieval import SearchRequest

commands = typer.Typer()
docs = typer.Typer(no_args_is_help=True)
session = typer.Typer(no_args_is_help=True)
allocation = typer.Typer(no_args_is_help=True)
commands.add_typer(docs, name="doc")
commands.add_typer(session, name="session")
commands.add_typer(allocation, name="allocation")


def _show(model: BaseModel, pretty: bool = True) -> None:
    typer.echo(model.model_dump_json(indent=2 if pretty else None))


@allocation.command("show")
def allocation_show(ctx: typer.Context) -> None:
    with open_client(ctx) as client:
        _show(client.get_allocation_state())


@allocation.command("history")
def allocation_history(
    ctx: typer.Context,
    after: Annotated[int, typer.Option(min=0)] = 0,
    limit: Annotated[int, typer.Option(min=1, max=100)] = 50,
) -> None:
    with open_client(ctx) as client:
        for entry in client.get_allocation_history(after, limit):
            _show(entry, pretty=False)


@allocation.command("reset")
def allocation_reset(ctx: typer.Context) -> None:
    with open_client(ctx) as client:
        _show(client.reset_allocation_state())


def _scope_drop(ctx: typer.Context, user_level: bool) -> set[str]:
    if not user_level:
        return set()
    if not ctx.ensure_object(ClientSettings).user:
        raise typer.BadParameter("Set MEMFXXK_USER to use --user-level")
    return {"agent_id", "project_id"}


def _edited(text: str, suffix: str) -> str:
    command = os.environ.get("EDITOR") or ("notepad" if os.name == "nt" else "vi")
    arguments = shlex.split(command, posix=os.name != "nt")
    if os.name == "nt":
        arguments = [part.strip('"') for part in arguments]
    with TemporaryDirectory(prefix="memfxxk-edit-") as directory:
        draft = Path(directory) / f"draft{suffix}"
        draft.write_text(text, encoding="utf-8")
        subprocess.run([*arguments, str(draft)], check=True)
        return draft.read_text(encoding="utf-8")


@commands.command()
def search(
    ctx: typer.Context,
    query: str,
    k: Annotated[int, typer.Option("-k", min=1)] = 10,
    types: Annotated[list[str] | None, typer.Option("--type")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    with open_client(ctx) as client:
        result = client.search(SearchRequest(query=query, k=k, types=types))
    if json_output:
        _show(result, pretty=False)
        return
    for hit in result.hits:
        typer.echo(f"{hit.memory_id}  [{hit.type}]  {hit.title or hit.snippet}")


@commands.command("list")
def list_command(
    ctx: typer.Context,
    important: bool = False,
    pinned: bool = False,
    inactive: bool = False,
    types: Annotated[list[str] | None, typer.Option("--type")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    marks: list[Mark] = []
    if important:
        marks.append("important")
    if pinned:
        marks.append("pinned")
    with open_client(ctx) as client:
        result = client.list_memories(
            ListRequest(
                types=types,
                marks=marks or None,
                status="inactive" if inactive else "active",
            )
        )
    if json_output:
        _show(result, pretty=False)
        return
    for item in result.items:
        typer.echo(f"{item.memory_id}  [{item.type} v{item.version}]  {item.title or item.snippet}")


@commands.command()
def show(ctx: typer.Context, memory_id: UUID, version: int | None = None, window: int = 0) -> None:
    with open_client(ctx) as client:
        memory = client.get_memory(memory_id, version=version, window=window)
    for item in [*memory.neighbors.before, memory, *memory.neighbors.after]:
        typer.echo(item.content)


@commands.command()
def remember(
    ctx: typer.Context,
    text: str,
    title: str | None = None,
    user_level: bool = False,
    important: bool = False,
    pin: bool = False,
) -> None:
    scope_drop = _scope_drop(ctx, user_level)
    with open_client(ctx) as client:
        result = client.remember(
            RememberRequest(
                content=text,
                title=title,
                important=important,
                pinned=pin,
                scope_drop=scope_drop,
            )
        )
    typer.echo(result.memory_id)


@commands.command()
def edit(ctx: typer.Context, memory_id: UUID) -> None:
    with open_client(ctx) as client:
        memory = client.get_memory(memory_id)
        content = _edited(memory.content, ".md")
        if content != memory.content:
            _show(
                client.patch_memory(
                    memory_id,
                    PatchRequest(
                        content=content,
                        base_version=memory.version,
                    ),
                )
            )


@commands.command()
def mark(
    ctx: typer.Context, memory_id: UUID, kind: Mark, off: bool = False, user_level: bool = False
) -> None:
    request = PatchRequest(
        important=not off if kind == "important" else None,
        pinned=not off if kind == "pinned" else None,
        mark_scope_drop=_scope_drop(ctx, user_level),
    )
    with open_client(ctx) as client:
        _show(client.patch_memory(memory_id, request))


@commands.command()
def link(
    ctx: typer.Context,
    source: UUID,
    target: UUID,
    purpose: str = "fixes",
    note: str | None = None,
) -> None:
    with open_client(ctx) as client:
        result = client.link(
            LinkRequest(
                from_memory_id=source,
                to_memory_id=target,
                purpose=purpose,
                note=note,
            )
        )
    typer.echo(result.relation_id)


@commands.command()
def unlink(ctx: typer.Context, relation_id: UUID) -> None:
    with open_client(ctx) as client:
        client.unlink(relation_id)


@commands.command()
def forget(ctx: typer.Context, memory_id: UUID, delete: bool = False, yes: bool = False) -> None:
    if delete and not yes:
        typer.confirm("Permanently delete this memory and all versions?", abort=True)
    with open_client(ctx) as client:
        _show(client.forget(memory_id, "delete" if delete else "deactivate"))


@docs.command("show")
def show_document(ctx: typer.Context, path: str, revision: int | None = None) -> None:
    with open_client(ctx) as client:
        typer.echo(client.get_document(path, revision=revision).content)


@docs.command("put")
def put_document(
    ctx: typer.Context,
    path: str,
    content: str | None = None,
    file: Annotated[Path | None, typer.Option(exists=True, dir_okay=False)] = None,
    base_revision: int | None = None,
) -> None:
    if file is not None and content is not None:
        raise typer.BadParameter("Provide either content or --file")
    text = file.read_text(encoding="utf-8") if file is not None else content
    if text is None:
        text = typer.get_text_stream("stdin").read()
    with open_client(ctx) as client:
        result = client.put_document(
            path, PutDocumentRequest(content=text, base_revision=base_revision)
        )
    typer.echo(result.revision)


@docs.command("edit")
def edit_document(ctx: typer.Context, path: str) -> None:
    with open_client(ctx) as client:
        document = client.get_document(path)
        content = _edited(document.content, ".md")
        if content != document.content:
            scope_drop = {
                key
                for key in {"user_id", "agent_id", "project_id"}
                if getattr(document.scope, key) is None
            }
            result = client.put_document(
                path,
                PutDocumentRequest(
                    content=content,
                    base_revision=document.revision,
                    scope_drop=scope_drop,
                ),
            )
            typer.echo(result.revision)


@docs.command("history")
def document_history(ctx: typer.Context, path: str) -> None:
    with open_client(ctx) as client:
        for revision in client.list_revisions(path):
            typer.echo(
                f"{revision.revision}  {revision.created_at.isoformat()}  {revision.message or ''}"
            )


@session.command("show")
def show_session(ctx: typer.Context, session_id: str) -> None:
    with open_client(ctx) as client:
        _show(client.get_session(session_id))


@commands.command()
def context(ctx: typer.Context, session_id: str, query: str, explain: bool = False) -> None:
    with open_client(ctx) as client:
        result = client.build_context(BuildContextRequest(session_id=session_id, query=query))
    typer.echo(result.rendered)
    if explain:
        for section in result.sections:
            typer.echo(f"{section.name} {section.used_tokens}/{section.budget}")
            for item in section.items:
                typer.echo(f"  {item.ref_id}  {item.reason}  {item.tokens} tokens")
