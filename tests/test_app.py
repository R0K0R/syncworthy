"""Generic-app tests with a throwaway plugin (nothing Noteworthy in sight)."""
import json
from pathlib import Path

import pytest
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import uvicorn
from websockets.sync.client import connect

from syncworthy import Action, Plugin, SettingsPage, McpTool, create_app
from syncworthy.config import Config


class Diag:
    extensions = (".txt",)

    async def check(self, ctx, path):
        return {"diagnostics": [{"message": "bad", "severity": "error", "line": 1, "col": 1, "file": path}]}


class Demo(Plugin):
    name = "demo"
    ignore = ["secret"]
    static_dir = None
    diagnostics = Diag()

    def actions(self):
        return [Action("hello", "Hello", lambda ctx, p: {"success": True, "output": "hi " + str(p.get("who"))})]

    def settings_pages(self):
        store = {"title": "t"}
        return [SettingsPage("general", "General", fields=[{"key": "title", "type": "text"}],
                             load=lambda ctx: dict(store), save=lambda ctx, d: store.update(d))]

    def mcp_tools(self):
        return [McpTool("ping2", "pong", {"type": "object"}, lambda ctx, a: "pong2")]

    def branding(self):
        return {"name": "Demo", "new_file_name": "x.demo"}


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return json.loads(self._body)


class Client:
    """A tiny HTTP/WebSocket client (no httpx needed): the app runs on a real port."""

    def __init__(self, port):
        self.base = f"http://127.0.0.1:{port}"
        self.port = port

    def _do(self, method, path, params=None, json_body=None, headers=None):
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        data = json.dumps(json_body).encode() if json_body is not None else None
        h = {"content-type": "application/json", **(headers or {})}
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h, method=method), timeout=10)
            return _Resp(r.status, r.read())
        except urllib.error.HTTPError as e:
            return _Resp(e.code, e.read())

    def get(self, path, params=None):
        return self._do("GET", path, params)

    def post(self, path, json=None, headers=None):
        return self._do("POST", path, json_body=json if json is not None else {}, headers=headers)

    def websocket_connect(self, path):
        return connect(f"ws://127.0.0.1:{self.port}{path}")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNCWORTHY_MCP_TOKEN", "tok")
    (tmp_path / "a.txt").write_text("hello\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.md").write_text("# b\n")
    (tmp_path / "secret").mkdir()
    (tmp_path / "secret" / "s.txt").write_text("s")
    (tmp_path / ".git").mkdir()
    app = create_app(tmp_path, Config(), [Demo()])
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    c = Client(port)
    c.root = tmp_path
    yield c
    server.should_exit = True
    thread.join(timeout=5)


def test_app_info(client):
    info = client.get("/api/app").json()
    assert info["name"] == "Demo" and info["branding"]["new_file_name"] == "x.demo"
    assert info["diagnostics"] == {"extensions": [".txt"]} and info["preview"] is None
    assert [a["id"] for a in info["actions"]] == ["hello"]


def test_tree_ignores(client):
    names = {i["name"] for i in client.get("/api/tree").json()["items"]}
    assert names == {"a.txt", "sub"}


def test_file_api(client):
    assert client.get("/api/file", params={"path": "a.txt"}).json() == {"content": "hello\n"}
    assert client.get("/api/file", params={"path": "../x"}).json() == {"error": "Invalid path"}
    assert client.post("/api/rename", json={"path": "a.txt", "newName": "c.txt"}).json()["success"]
    assert (client.root / "c.txt").exists()
    assert client.post("/api/delete", json={"path": "c.txt"}).json()["success"]
    assert not (client.root / "c.txt").exists()


def test_plugin_seams(client):
    assert client.post("/api/check", json={"path": "a.txt"}).json()["diagnostics"][0]["message"] == "bad"
    assert client.post("/api/actions/hello", json={"who": "me"}).json()["output"] == "hi me"
    client.post("/api/settings/general", json={"title": "new"})
    assert client.get("/api/settings/general").json() == {"title": "new"}


def _rpc(client, method, params=None, token="tok"):
    h = {"authorization": f"Bearer {token}"} if token else {}
    return client.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})


def test_mcp(client):
    assert _rpc(client, "tools/list", token=None).status_code == 401
    names = [t["name"] for t in _rpc(client, "tools/list").json()["result"]["tools"]]
    assert names == ["list_documents", "read_document", "edit_document", "append_document", "ping2"]
    r = _rpc(client, "tools/call", {"name": "edit_document", "arguments": {"path": "a.txt", "old_string": "hello", "new_string": "bye"}})
    assert not r.json()["result"]["isError"] and (client.root / "a.txt").read_text() == "bye\n"
    listed = _rpc(client, "tools/call", {"name": "list_documents", "arguments": {}}).json()["result"]["content"][0]["text"]
    assert "secret" not in listed and "a.txt" in listed
    assert _rpc(client, "tools/call", {"name": "ping2", "arguments": {}}).json()["result"]["content"][0]["text"] == "pong2"


def test_doc_socket_chat(client):
    with client.websocket_connect("/ws/doc?name=A&id=a") as a, client.websocket_connect("/ws/doc?name=B&id=b") as b:
        assert json.loads(a.recv())["type"] == "welcome"
        assert json.loads(b.recv())["type"] == "welcome"
        assert json.loads(a.recv())["type"] == "user_joined"
        b.send(json.dumps({"type": "chat", "text": "hi"}))
        assert json.loads(a.recv())["text"] == "hi"


def test_state_dir_keeps_legacy(tmp_path):
    (tmp_path / ".noteworthy-crdt").mkdir()
    assert create_app(tmp_path, Config(), []).state.sync.state_dir.name == ".noteworthy-crdt"
    assert create_app(tmp_path / ".", Config(state_dir=".x"), []).state.sync.state_dir.name == ".x"
    other = tmp_path / "o"; other.mkdir()
    assert create_app(other, Config(), []).state.sync.state_dir.name == ".syncworthy"
