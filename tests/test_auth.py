"""Auth: tokens with roles on every surface, and the bind policy."""
import json
import subprocess
import sys
import time

import pytest
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect

from helpers import DocClient, YClient, free_port, http, run_app, ws_url
from syncworthy import create_app
from syncworthy.auth import Auth, Token, check_bind, is_loopback
from syncworthy.config import Config

EDIT = "E" * 24
VIEW = "V" * 24
MCP = "mcp-secret-token"


@pytest.fixture
def secured(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNCWORTHY_MCP_TOKEN", MCP)
    monkeypatch.setenv("SW_T_EDIT", EDIT)
    monkeypatch.setenv("SW_T_VIEW", VIEW)
    (tmp_path / "a.txt").write_text("hello\n")
    config = Config.from_dict({"auth": {"tokens": [
        {"name": "me", "token_env": "SW_T_EDIT", "role": "edit"},
        {"name": "guest", "token_env": "SW_T_VIEW", "role": "view"}]}})
    app = create_app(tmp_path, config, [])
    with run_app(app) as port:
        yield type("S", (), {"root": tmp_path, "port": port, "app": app})


def bearer(t):
    return {"authorization": f"Bearer {t}"}


def test_http_api_needs_a_token(secured):
    p = secured.port
    assert http(p, "GET", "/api/tree").status_code == 401
    assert http(p, "GET", "/api/tree", headers=bearer("nope")).status_code == 401
    assert http(p, "GET", "/api/tree", headers=bearer(EDIT[:-1])).status_code == 401
    assert http(p, "GET", "/api/tree", headers=bearer(VIEW)).status_code == 200
    assert http(p, "GET", "/api/tree", headers=bearer(EDIT)).status_code == 200
    assert http(p, "GET", "/api/file", params={"path": "a.txt"}, headers=bearer(VIEW)).json() == {"content": "hello\n"}


def test_view_cannot_write_edit_can(secured):
    p = secured.port
    body = {"path": "n.txt", "content": "x"}
    r = http(p, "POST", "/api/file", body=body, headers=bearer(VIEW))
    assert r.status_code == 403 and not (secured.root / "n.txt").exists()
    assert http(p, "POST", "/api/delete", body={"path": "a.txt"}, headers=bearer(VIEW)).status_code == 403
    assert http(p, "POST", "/api/rename", body={"path": "a.txt", "newName": "b.txt"},
                headers=bearer(VIEW)).status_code == 403
    assert http(p, "POST", "/api/rooms/reload", headers=bearer(VIEW)).status_code == 403
    assert (secured.root / "a.txt").exists()
    assert http(p, "POST", "/api/check", body={"path": "a.txt"}, headers=bearer(VIEW)).status_code == 200  # read-only
    assert http(p, "POST", "/api/file", body=body, headers=bearer(EDIT)).json()["success"]
    assert (secured.root / "n.txt").read_text() == "x"
    assert http(p, "POST", "/api/file", body=body).status_code == 401


def test_static_assets_and_the_cookie_handshake(secured):
    p = secured.port
    page = {"accept": "text/html"}
    r = http(p, "GET", "/", headers=page)
    assert r.status_code == 401 and "access token" in r.text          # the sign-in form, not the app
    assert http(p, "GET", "/js/app.js").status_code == 401
    assert http(p, "GET", "/js/app.js", params={"token": "bad"}).status_code == 401

    r = http(p, "GET", "/", params={"token": EDIT, "x": "1"}, headers=page)
    assert r.status_code == 303 and r.headers["Location"] == "/?x=1"        # the token leaves the URL
    cookie = r.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie and EDIT in cookie
    jar = {"cookie": cookie.split(";")[0]}
    assert http(p, "GET", "/", headers={**page, **jar}).status_code == 200
    assert http(p, "GET", "/js/app.js", headers=jar).status_code == 200
    info = http(p, "GET", "/api/app", headers=jar).json()
    assert info["auth"] == {"enabled": True, "role": "edit"}
    v = http(p, "GET", "/", params={"token": VIEW}).headers["Set-Cookie"].split(";")[0]
    assert http(p, "GET", "/api/app", headers={"cookie": v}).json()["auth"]["role"] == "view"
    out = http(p, "GET", "/auth/logout", headers=jar)
    assert out.status_code == 303 and "Max-Age=0" in out.headers["Set-Cookie"]


def test_mcp_keeps_its_own_bearer(secured):
    p = secured.port
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    assert http(p, "POST", "/mcp", body=rpc).status_code == 401
    assert http(p, "POST", "/mcp", body=rpc, headers=bearer(EDIT)).status_code == 401   # not an MCP token
    assert http(p, "POST", "/mcp", body=rpc, headers=bearer(MCP)).status_code == 200


def rejected(url, **kw):
    with pytest.raises(InvalidStatus) as e:
        with connect(url, **kw):
            pass
    return e.value.response.status_code


def test_doc_socket(secured):
    p = secured.port
    url = ws_url(p, "/ws/doc", "name=A&id=a")
    assert rejected(url) == 403
    assert rejected(ws_url(p, "/ws/doc", "name=A&id=a&token=client-id&access_token=wrong")) == 403
    assert rejected(url, additional_headers=bearer("wrong")) == 403
    # a view token may chat (it cannot edit anything through this socket)
    a = DocClient(p, "A", "a", headers=bearer(VIEW))
    b = DocClient(p, "B", "b", query=f"access_token={EDIT}")
    try:
        a.drain(0.5)
        a.ws.send(json.dumps({"type": "chat", "text": "hi from view"}))
        assert b.wait_for("chat", text="hi from view")
    finally:
        a.close(); b.close()


def test_yjs_requires_a_token(secured):
    p = secured.port
    assert rejected(ws_url(p, "/yjs/a.txt")) == 403
    assert rejected(ws_url(p, "/yjs/a.txt"), additional_headers=bearer("wrong")) == 403


def test_yjs_view_reads_but_cannot_write(secured):
    p = secured.port
    ed = YClient(p, "a.txt", headers=bearer(EDIT))
    vw = YClient(p, "a.txt", query=f"access_token={VIEW}")
    try:
        assert str(vw.text) == "hello\n"                            # reads work
        ed.edit(lambda t: t.insert(0, "from-edit "))
        assert vw.wait_for(lambda: str(vw.text).startswith("from-edit"))   # and follow live
        vw.edit(lambda t: t.insert(0, "FROM-VIEW "))                 # dropped
        vw.pump(1.0)
        ed.pump(1.0)
        time.sleep(0.6)
        assert "FROM-VIEW" not in str(ed.text)
        assert "FROM-VIEW" not in (secured.root / "a.txt").read_text()
        assert (secured.root / "a.txt").read_text().startswith("from-edit ")
        # a fresh editor sees the room without it too
        ed2 = YClient(p, "a.txt", headers=bearer(EDIT))
        assert "FROM-VIEW" not in str(ed2.text)
        ed2.close()
    finally:
        ed.close(); vw.close()


def test_yjs_edit_cookie_works(secured):
    c = http(secured.port, "GET", "/", params={"token": EDIT}).headers["Set-Cookie"].split(";")[0]
    ed = YClient(secured.port, "a.txt", headers={"cookie": c})
    try:
        ed.edit(lambda t: t.insert(0, "x"))
        assert ed.wait_for(lambda: (secured.root / "a.txt").read_text().startswith("xhello"))
    finally:
        ed.close()


def test_websocket_origin_check(secured):
    p = secured.port
    cookie = http(p, "GET", "/", params={"token": EDIT}).headers["Set-Cookie"].split(";")[0]
    base = {"cookie": cookie}
    url = ws_url(p, "/ws/doc", "name=A&id=a")
    assert rejected(url, additional_headers={**base, "origin": "http://evil.example"}) == 403
    with connect(url, additional_headers={**base, "origin": f"http://127.0.0.1:{p}"}):
        pass


def test_open_mode_on_loopback_and_origin_check(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    app = create_app(tmp_path, Config(), [])
    assert not app.state.sync.auth.enabled
    with run_app(app) as p:
        assert http(p, "GET", "/api/tree").status_code == 200
        assert http(p, "POST", "/api/file", body={"path": "n", "content": "y"}).json()["success"]
        assert http(p, "GET", "/api/app").json()["auth"] == {"enabled": False, "role": "edit"}
        with connect(ws_url(p, "/ws/doc", "name=A&id=a")):
            pass
        # even open, a web page elsewhere must not be able to drive the editor
        assert rejected(ws_url(p, "/ws/doc"), additional_headers={"origin": "http://evil.example"}) == 403


# ---- policy and configuration ----------------------------------------------

def test_loopback_detection():
    assert all(is_loopback(h) for h in ("127.0.0.1", "localhost", "::1", "[::1]", "127.1.2.3"))
    assert not any(is_loopback(h) for h in ("0.0.0.0", "::", "", "192.168.1.5", "100.64.0.1", "example.org"))


def test_check_bind_policy():
    open_, secured_ = Auth(), Auth([Token("t", "x" * 20, "edit")])
    check_bind(open_, "127.0.0.1")
    check_bind(secured_, "0.0.0.0")
    with pytest.raises(SystemExit) as e:
        check_bind(open_, "0.0.0.0")
    assert "refusing" in str(e.value) and "--insecure-open" in str(e.value)
    check_bind(open_, "0.0.0.0", insecure_open=True)


def test_cli_refuses_non_loopback_without_tokens(tmp_path):
    cmd = [sys.executable, "-m", "syncworthy", "serve", str(tmp_path), "--headless", "-p", str(free_port())]
    r = subprocess.run(cmd + ["--bind", "0.0.0.0"], capture_output=True, text=True, timeout=30,
                       env={"PATH": "", "HOME": str(tmp_path)})
    assert r.returncode != 0 and "refusing to serve" in r.stderr


def test_cli_serves_with_tokens_or_insecure_open(tmp_path):
    import os
    for extra, env in ((["--insecure-open"], {}), ([], {"SYNCWORTHY_EDIT_TOKEN": "t" * 20})):
        port = free_port()
        proc = subprocess.Popen([sys.executable, "-m", "syncworthy", "serve", str(tmp_path), "--headless",
                                 "--bind", "0.0.0.0", "-p", str(port), *extra],
                                env={**os.environ, **env}, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            status = None
            for _ in range(150):
                if proc.poll() is not None:
                    break
                try:
                    status = http(port, "GET", "/api/tree", headers=bearer("t" * 20)).status_code
                    break
                except OSError:
                    time.sleep(0.1)
            assert proc.poll() is None, proc.stderr.read()
            assert status == 200
        finally:
            proc.terminate()
            proc.wait(timeout=10)


def test_tokens_never_literal_and_never_silently_dropped(monkeypatch, tmp_path):
    def load(entry, **env):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        return Auth.from_config(Config.from_dict({"auth": {"tokens": [entry]}}))

    with pytest.raises(SystemExit, match="not in syncworthy.toml"):
        load({"name": "x", "token": "literal", "role": "edit"})
    with pytest.raises(SystemExit, match="empty or unset"):
        load({"name": "x", "token_env": "SW_DOES_NOT_EXIST", "role": "edit"})
    with pytest.raises(SystemExit, match="role"):
        load({"name": "x", "token_env": "SW_T", "role": "root"}, SW_T="s" * 20)
    with pytest.raises(SystemExit, match="exactly one"):
        load({"name": "x", "role": "edit"})
    f = tmp_path / "tok"
    f.write_text("file-secret-0123456789\n")
    a = load({"name": "f", "token_file": str(f), "role": "view"})
    assert a.identify("file-secret-0123456789").role == "view"
    assert a.identify("file-secret-012345678") is None and a.identify("") is None and a.identify(None) is None


def test_env_tokens(monkeypatch):
    monkeypatch.setenv("SYNCWORTHY_EDIT_TOKEN", "e" * 20)
    monkeypatch.setenv("SYNCWORTHY_VIEW_TOKEN", "v" * 20)
    a = Auth.from_config(Config())
    assert a.enabled and a.identify("e" * 20).role == "edit" and a.identify("v" * 20).role == "view"


def test_duplicate_secrets_are_refused():
    with pytest.raises(SystemExit, match="same secret"):
        Auth([Token("a", "s" * 20, "edit"), Token("b", "s" * 20, "view")])
