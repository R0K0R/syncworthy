"""Shared test plumbing: a real server on a real port, an HTTP client, a Yjs client."""
import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager

import uvicorn
from pycrdt import Doc, Text, create_sync_message, create_update_message, handle_sync_message
from websockets.sync.client import connect


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def run_app(app):
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "server did not start"
    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=10)


class Headers(dict):
    """Case-insensitive lookups, since servers differ in the case they send."""

    def __init__(self, items=()):
        super().__init__((k.lower(), v) for k, v in dict(items).items())

    def __getitem__(self, key):
        return super().__getitem__(key.lower())


class Resp:
    def __init__(self, status, body, headers=None):
        self.status_code, self._body, self.headers = status, body, Headers(headers or {})

    def json(self):
        return json.loads(self._body)

    @property
    def text(self):
        return self._body.decode("utf-8", "replace")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def http(port, method, path, *, params=None, body=None, headers=None):
    url = f"http://127.0.0.1:{port}{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
    data = json.dumps(body).encode() if body is not None else None
    h = {"content-type": "application/json", **(headers or {})}
    opener = urllib.request.build_opener(NoRedirect)
    try:
        r = opener.open(urllib.request.Request(url, data=data, headers=h, method=method), timeout=10)
        return Resp(r.status, r.read(), dict(r.headers))
    except urllib.error.HTTPError as e:
        return Resp(e.code, e.read(), dict(e.headers))


def ws_url(port, path, query=""):
    return f"ws://127.0.0.1:{port}{path}" + (("?" + query) if query else "")


class YClient:
    """A minimal y-websocket client over a pycrdt Doc (what the browser's provider does)."""

    def __init__(self, port, room, *, query="", headers=None):
        self.doc = Doc()
        self.text = self.doc.get("content", type=Text)
        self.ws = connect(ws_url(port, f"/yjs/{room}", query), additional_headers=headers or {})
        self.ws.send(create_sync_message(self.doc))
        self.pump(1.0)

    def pump(self, seconds=0.5):
        """Process incoming frames for about SECONDS."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                msg = self.ws.recv(timeout=max(0.01, min(0.1, end - time.monotonic())))
            except TimeoutError:
                continue
            if isinstance(msg, bytes) and msg and msg[0] == 0:
                reply = handle_sync_message(msg[1:], self.doc)
                if reply:
                    self.ws.send(reply)

    def wait_for(self, predicate, timeout=6.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return True
            self.pump(0.1)
        return predicate()

    def edit(self, fn):
        updates = []
        sub = self.doc.observe(lambda e: updates.append(e.update))
        with self.doc.transaction():
            fn(self.text)
        for u in updates:
            self.ws.send(create_update_message(u))
        del sub

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


class DocClient:
    """The /ws/doc socket: collects messages."""

    def __init__(self, port, name="W", ident="w", headers=None, query=""):
        q = f"name={name}&id={ident}" + (("&" + query) if query else "")
        self.ws = connect(ws_url(port, "/ws/doc", q), additional_headers=headers or {})
        self.messages = []

    def drain(self, seconds=0.3):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                self.messages.append(json.loads(self.ws.recv(timeout=0.05)))
            except TimeoutError:
                pass

    def wait_for(self, mtype, timeout=6.0, **match):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for m in self.messages:
                if m.get("type") == mtype and all(m.get(k) == v for k, v in match.items()):
                    return m
            self.drain(0.1)
        return None

    def count(self, mtype):
        return sum(1 for m in self.messages if m.get("type") == mtype)

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass
