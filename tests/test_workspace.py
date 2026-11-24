from __future__ import annotations

import pytest

from nexis.workspace import Workspace, WorkspaceViolation


@pytest.fixture()
def workspace(tmp_path):
    return Workspace(tmp_path / "NexisWorkspace")


def test_root_is_created(workspace):
    assert workspace.root.is_dir()


def test_write_and_read_roundtrip(workspace):
    workspace.write_text("notes/today.md", "hello nexis")
    assert workspace.read_text("notes/today.md") == "hello nexis"


def test_relative_paths_resolve_inside(workspace):
    target = workspace.resolve("a/b.txt")
    assert target == workspace.root / "a" / "b.txt"


@pytest.mark.parametrize("path", ["../escape.txt", "..", "a/../../escape.txt"])
def test_parent_traversal_is_blocked(workspace, path):
    with pytest.raises(WorkspaceViolation):
        workspace.resolve(path)


def test_absolute_outside_path_is_blocked(workspace, tmp_path):
    with pytest.raises(WorkspaceViolation):
        workspace.resolve(tmp_path / "elsewhere.txt")
    with pytest.raises(WorkspaceViolation):
        workspace.resolve("C:/Windows/win.ini")


def test_delete_requires_recursive_for_directories(workspace):
    workspace.write_text("folder/file.txt", "x")
    with pytest.raises(IsADirectoryError):
        workspace.delete("folder")
    workspace.delete("folder", recursive=True)
    assert not (workspace.root / "folder").exists()


def test_delete_root_is_refused(workspace):
    with pytest.raises(ValueError):
        workspace.delete(".")


def test_list_dir_reports_files_and_folders(workspace):
    workspace.write_text("one.txt", "1")
    workspace.write_text("sub/two.txt", "2")
    entries = {item.path: item.kind for item in workspace.list_dir(".")}
    assert entries["one.txt"] == "file"
    assert entries["sub"] == "directory"


def test_search_finds_lines(workspace):
    workspace.write_text("a.txt", "alpha\nbeta gamma\n")
    workspace.write_text("b.bin", "gamma\x00binary")
    hits = workspace.search("gamma")
    assert len(hits) == 1
    assert hits[0].path == "a.txt"
    assert hits[0].line == 2


def test_binary_file_is_not_read_as_text(workspace):
    (workspace.root / "blob.bin").write_bytes(b"\x00\x01\x02")
    with pytest.raises(ValueError):
        workspace.read_text("blob.bin")
