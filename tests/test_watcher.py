"""The file watcher: external edits, own writes, create/delete/rename."""
import time

import pytest

from helpers import DocClient, YClient, run_app
from syncworthy import Plugin, create_app
from syncworthy import watcher as watcher_mod
from syncworthy.config import Config


class Demo(Plugin):
    name = "demo"
    ignore = ["secret"]


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(watcher_mod, "GRACE_SECONDS", 0.8)
    monkeypatch.setattr(watcher_mod, "DEBOUNCE_MS", 150)
    (tmp_path / "a.txt").write_text("hello\nworld\n")
    (tmp_path / "other.txt").write_text("other\n")
    (tmp_path / "secret").mkdir()
    app = create_app(tmp_path, Config(), [Demo()])
    with run_app(app) as port:
        time.sleep(0.6)                      # let the watcher install
        yield type("P", (), {"root": tmp_path, "port": port, "app": app, "rooms": app.state.sync.rooms})


def test_external_edit_merges_into_live_room(project):
    c = YClient(project.port, "a.txt")
    try:
        assert str(c.text) == "hello\nworld\n"
        ident = c.doc.client_id
        (project.root / "a.txt").write_text("hello\nbrave new world\n")
        assert c.wait_for(lambda: str(c.text) == "hello\nbrave new world\n"), str(c.text)
        # the room kept its identity: the client keeps editing the same document
        c.edit(lambda t: t.insert(0, ">> "))
        assert c.wait_for(lambda: (project.root / "a.txt").read_text() == ">> hello\nbrave new world\n")
        assert c.doc.client_id == ident
    finally:
        c.close()


def test_own_writes_are_not_echoed(project, monkeypatch):
    reloads = []
    real = project.rooms.reload_room

    async def counting(path):
        reloads.append(path)
        return await real(path)

    monkeypatch.setattr(project.rooms, "reload_room", counting)
    d = DocClient(project.port)
    c = YClient(project.port, "a.txt")
    try:
        for ch in "abc":
            c.edit(lambda t, ch=ch: t.insert(len(str(t)), ch))
            time.sleep(0.4)                   # each is saved on its own
        assert c.wait_for(lambda: (project.root / "a.txt").read_text().endswith("abc"))
        time.sleep(1.5)                       # well past the watcher's debounce
        d.drain(0.3)
        assert reloads == []
        assert d.count("tree_changed") == 0
        # and the ledger does not shadow a later external edit
        (project.root / "a.txt").write_text("external\n")
        assert c.wait_for(lambda: str(c.text) == "external\n")
        assert reloads == ["a.txt"]
    finally:
        c.close(); d.close()


def test_create_and_delete_without_room_refresh_the_tree(project):
    d = DocClient(project.port)
    try:
        d.drain(0.3)
        (project.root / "new.txt").write_text("x")
        assert d.wait_for("tree_changed")
        n = d.count("tree_changed")
        (project.root / "new.txt").unlink()
        end = time.monotonic() + 5
        while d.count("tree_changed") <= n and time.monotonic() < end:
            d.drain(0.1)
        assert d.count("tree_changed") > n
    finally:
        d.close()


def test_ignored_paths_are_silent(project):
    d = DocClient(project.port)
    try:
        d.drain(0.3)
        (project.root / "secret" / "s.txt").write_text("s")        # an ignore glob
        (project.root / ".syncworthy").mkdir(exist_ok=True)         # the state dir
        (project.root / ".syncworthy" / "x.bin").write_bytes(b"x")
        (project.root / "build").mkdir()                            # a default ignore
        (project.root / "build" / "o").write_text("o")
        d.drain(1.5)
        assert d.count("tree_changed") == 0
    finally:
        d.close()


def test_delete_closes_the_room_and_does_not_resurrect_the_file(project):
    d = DocClient(project.port)
    c = YClient(project.port, "a.txt")
    try:
        assert "a.txt" in project.rooms.rooms
        (project.root / "a.txt").unlink()
        assert d.wait_for("file_removed", path="a.txt")
        assert "a.txt" not in project.rooms.rooms
        # a client still typing must not bring the file back
        try:
            c.edit(lambda t: t.insert(0, "zombie"))
        except Exception:
            pass
        time.sleep(1.0)
        assert not (project.root / "a.txt").exists()
    finally:
        c.close(); d.close()


def test_rename_rebinds_the_room(project):
    d = DocClient(project.port)
    c = YClient(project.port, "a.txt")
    try:
        (project.root / "a.txt").rename(project.root / "renamed.txt")
        msg = d.wait_for("file_renamed")
        assert msg and msg["old"] == "a.txt" and msg["new"] == "renamed.txt"
        assert "renamed.txt" in project.rooms.rooms and "a.txt" not in project.rooms.rooms
        c.edit(lambda t: t.insert(0, "moved "))
        assert c.wait_for(lambda: (project.root / "renamed.txt").exists()
                          and (project.root / "renamed.txt").read_text().startswith("moved hello"))
        assert not (project.root / "a.txt").exists()
    finally:
        c.close(); d.close()


def test_directory_rename_rebinds_rooms_under_it(project):
    (project.root / "dir").mkdir()
    (project.root / "dir" / "n.txt").write_text("nested\n")
    time.sleep(0.6)
    d = DocClient(project.port)
    c = YClient(project.port, "dir/n.txt")
    try:
        (project.root / "dir").rename(project.root / "dir2")
        assert d.wait_for("file_renamed", old="dir/n.txt", new="dir2/n.txt")
        assert "dir2/n.txt" in project.rooms.rooms
    finally:
        c.close(); d.close()


def test_delete_and_recreate_in_place_just_reloads(project):
    """An editor that saves by deleting and recreating the file."""
    c = YClient(project.port, "a.txt")
    d = DocClient(project.port)
    try:
        p = project.root / "a.txt"
        p.unlink()
        p.write_text("rewritten\n")
        assert c.wait_for(lambda: str(c.text) == "rewritten\n")
        d.drain(1.5)
        assert "a.txt" in project.rooms.rooms and d.count("file_removed") == 0
    finally:
        c.close(); d.close()
