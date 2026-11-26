import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from memfxxk.cli.workspace import read_workspace, write_workspace
from memfxxk.domain.workspace import WorkspaceDocument, WorkspaceSnapshot


def test_workspace_directory_preserves_markdown_bytes_and_rejects_overwrite(tmp_path: Path) -> None:
    folder = tmp_path / "plain"
    (folder / "nested").mkdir(parents=True)
    original = "# 项目\r\n\r\nKeep whitespace.  \r\n".encode()
    (folder / "nested" / "a.md").write_bytes(original)
    snapshot = read_workspace(folder)
    assert len(snapshot.documents) == 1
    destination = tmp_path / "exported"
    write_workspace(destination, snapshot)
    assert (destination / "documents" / "nested" / "a.md").read_bytes() == original
    assert read_workspace(destination) == snapshot
    with pytest.raises(FileExistsError):
        write_workspace(destination, snapshot)
    assert (destination / "documents" / "nested" / "a.md").read_bytes() == original


@pytest.mark.parametrize("path", ["../secret.md", "NUL.md", "notes./a.md", "/abs.md", "./a.md"])
def test_workspace_rejects_unsafe_paths(path: str) -> None:
    with pytest.raises(ValidationError):
        WorkspaceDocument(path=path, content="anything")


@pytest.mark.parametrize("other", ["A.md", "a.md/test.md"])
def test_workspace_rejects_portable_path_collisions(other: str) -> None:
    with pytest.raises(ValidationError):
        WorkspaceSnapshot(
            documents=[
                WorkspaceDocument(path="a.md", content="one"),
                WorkspaceDocument(path=other, content="two"),
            ]
        )


def test_workspace_manifest_cannot_follow_a_file_link(tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("Outside content", encoding="utf-8")
    root = tmp_path / "workspace"
    (root / "documents").mkdir(parents=True)
    (root / "workspace.json").write_text(
        json.dumps({"documents": [{"path": "linked.md"}]}), encoding="utf-8"
    )
    try:
        (root / "documents" / "linked.md").symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"Symbolic link creation unavailable: {exc}")
    with pytest.raises(ValueError, match="Linked workspace entry"):
        read_workspace(root)
