from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated
from uuid import UUID

import typer

from memfxxk.cli.connection import open_client
from memfxxk.domain.workspace import (
    ImportWorkspaceRequest,
    WorkspaceDocument,
    WorkspaceLesson,
    WorkspaceManifest,
    WorkspaceSnapshot,
    portable_path,
)

commands = typer.Typer(no_args_is_help=True)
_MANIFEST = "workspace.json"


def _linked(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _load_text(root: Path, relative: Path) -> str:
    path = root
    for part in relative.parts:
        path /= part
        if _linked(path):
            raise ValueError(f"Linked workspace entry: {path.relative_to(root)}")
    if not path.resolve().is_relative_to(root) or not path.is_file():
        raise ValueError(f"Expected a workspace file: {relative}")
    return path.read_bytes().decode("utf-8")


def _raise_walk_error(error: OSError) -> None:
    raise error


def read_workspace(directory: Path) -> WorkspaceSnapshot:
    if _linked(directory) or not directory.is_dir():
        raise ValueError("Select a workspace directory")
    root = directory.resolve(strict=True)
    manifest_path = root / _MANIFEST
    if manifest_path.exists() or _linked(manifest_path):
        manifest = WorkspaceManifest.model_validate_json(_load_text(root, Path(_MANIFEST)))
        return WorkspaceSnapshot(
            documents=[
                WorkspaceDocument(
                    **entry.model_dump(),
                    content=_load_text(root, Path("documents") / entry.path),
                )
                for entry in manifest.documents
            ],
            lessons=[
                WorkspaceLesson(
                    **entry.model_dump(),
                    content=_load_text(root, Path("lessons") / f"{entry.memory_id}.md"),
                )
                for entry in manifest.lessons
            ],
        )
    docs = []
    for folder, dirs, files in root.walk(on_error=_raise_walk_error):
        if any(_linked(folder / name) for name in dirs):
            raise ValueError(f"Linked workspace directory under {folder.relative_to(root)}")
        for name in files:
            file = folder / name
            if file.suffix == ".md":
                relative = file.relative_to(root)
                docs.append(
                    WorkspaceDocument(
                        path=portable_path(relative.as_posix()),
                        content=_load_text(root, relative),
                    )
                )
    return WorkspaceSnapshot(documents=sorted(docs, key=lambda doc: doc.path))


def write_workspace(directory: Path, snapshot: WorkspaceSnapshot) -> None:
    parent = directory.parent.resolve(strict=True)
    destination = parent / directory.name
    if not directory.name or destination.exists() or _linked(destination):
        raise FileExistsError("Choose a new directory for workspace export")
    data = snapshot.model_dump(
        exclude={
            "documents": {"__all__": {"content"}},
            "lessons": {"__all__": {"content"}},
        }
    )
    manifest = WorkspaceManifest.model_validate(data)
    with TemporaryDirectory(dir=parent) as temporary:
        staging = Path(temporary) / "workspace"
        staging.mkdir()
        for entry in snapshot.documents:
            path = staging / "documents" / entry.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(entry.content.encode("utf-8"))
        for lesson in snapshot.lessons:
            path = staging / "lessons" / f"{lesson.memory_id}.md"
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(lesson.content.encode("utf-8"))
        (staging / _MANIFEST).write_bytes(
            manifest.model_dump_json(indent=2).encode("utf-8") + b"\n"
        )
        staging.rename(destination)


@commands.command("export")
def export_workspace(
    ctx: typer.Context,
    directory: Path,
    lesson: Annotated[list[UUID] | None, typer.Option("--lesson")] = None,
) -> None:
    """Export Markdown and selected lesson evidence to a new directory."""
    try:
        with open_client(ctx) as client:
            snapshot = client.export_workspace(lesson or [])
        write_workspace(directory, snapshot)
    except (OSError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Exported {len(snapshot.documents)} documents and {len(snapshot.lessons)} lessons.")


@commands.command("import")
def import_workspace(ctx: typer.Context, directory: Path, update: bool = False) -> None:
    """Import a workspace snapshot or a directory of Markdown files."""
    try:
        snapshot = read_workspace(directory)
        request = ImportWorkspaceRequest(snapshot=snapshot, update_existing=update)
        with open_client(ctx) as client:
            result = client.import_workspace(request)
    except (OSError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(result.model_dump_json(indent=2))
