"""Authentication: project tokens with a role, enforced on every surface.

Two roles: ``edit`` (everything) and ``view`` (read-only: it may read documents,
see presence and chat, but its Yjs updates are dropped and every state-changing
HTTP request is refused).  Tokens come from the project's ``syncworthy.toml``,
never as literals -- the file names where the secret lives::

    [auth]
    tokens = [
        { name = "me",    token_env  = "SYNCWORTHY_TOKEN_ME",  role = "edit" },
        { name = "class", token_file = "/run/secrets/class",   role = "view" },
    ]
    allowed_origins = []        # extra origins allowed to open WebSockets (see below)

or from the environment: ``SYNCWORTHY_EDIT_TOKEN`` / ``SYNCWORTHY_VIEW_TOKEN``
(each also as ``..._FILE``).

A browser presents its token once, as ``?token=...``; the server answers with an
HttpOnly, SameSite=Lax cookie and redirects to the same URL without it.  Other
clients may send ``Authorization: Bearer <token>`` on any request, and
``?access_token=`` on a WebSocket URL (``?token=`` on ``/ws/doc`` is the
client's own identity token, an unrelated thing).  ``/mcp`` keeps its own
bearer check (see ``mcp.py``) and is not touched here.

With no tokens configured the server is open, which is only allowed on a
loopback bind (single user) -- see ``check_bind``.  Every comparison is
constant-time (digests of both sides, ``hmac.compare_digest``, no early exit).

WebSockets also get an Origin check: a page from another origin must not be
able to drive the editor through a visitor's cookie (or, on a loopback bind with
no tokens, through the visitor's own network position).  A request with no
Origin header (every non-browser client) is let through.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import ipaddress
import json
import logging
import os
from dataclasses import dataclass
from http.cookies import SimpleCookie
from urllib.parse import parse_qsl, urlencode, urlsplit

log = logging.getLogger("syncworthy")

EDIT, VIEW = "edit", "view"
ROLES = (EDIT, VIEW)
COOKIE = "sw_auth"
COOKIE_MAX_AGE = 30 * 24 * 3600
EXEMPT_PATHS = ("/mcp",)                 # has its own bearer check
LOGOUT_PATH = "/auth/logout"
# POST endpoints that only read; everything else that is not GET/HEAD/OPTIONS needs `edit`.
VIEW_POSTS = ("/api/check",)
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")

ENV_TOKENS = (("SYNCWORTHY_EDIT_TOKEN", EDIT), ("SYNCWORTHY_VIEW_TOKEN", VIEW))


@dataclass(frozen=True)
class Token:
    name: str
    secret: str
    role: str


def _digest(s: str) -> bytes:
    return hashlib.sha256(s.encode("utf-8")).digest()


def _read_secret(what: str, env: str | None, file: str | None, environ) -> str:
    if env:
        value = environ.get(env, "")
        where = f"${env}"
    else:
        where = str(file)
        try:
            value = open(file, encoding="utf-8").read()
        except OSError as e:
            raise SystemExit(f"syncworthy: auth token {what}: cannot read {where}: {e}")
    value = value.strip()
    if not value:
        # Fail closed: dropping a token silently could leave the server with none, i.e. open.
        raise SystemExit(f"syncworthy: auth token {what}: {where} is empty or unset")
    return value


class Auth:
    """The set of valid tokens."""

    def __init__(self, tokens: list[Token] | None = None, allowed_origins: list[str] | None = None):
        self.tokens = list(tokens or [])
        self.allowed_origins = {o.rstrip("/").lower() for o in (allowed_origins or [])}
        self._digests = [(_digest(t.secret), t) for t in self.tokens]
        for i, (d, t) in enumerate(self._digests):
            for d2, t2 in self._digests[:i]:
                if hmac.compare_digest(d, d2):
                    raise SystemExit(f"syncworthy: auth tokens {t2.name!r} and {t.name!r} are the same secret")
        for t in self.tokens:
            if len(t.secret) < 16:
                log.warning("auth token %r is shorter than 16 characters", t.name)

    @property
    def enabled(self) -> bool:
        return bool(self.tokens)

    def identify(self, given: str | None) -> Token | None:
        """The token GIVEN matches, comparing against every one in constant time."""
        if not given:
            return None
        d = _digest(given)
        found = None
        for digest, tok in self._digests:
            if hmac.compare_digest(d, digest):
                found = tok
        return found

    @classmethod
    def from_config(cls, config, environ=None) -> "Auth":
        environ = os.environ if environ is None else environ
        tokens: list[Token] = []
        for i, entry in enumerate(getattr(config, "auth_tokens", None) or []):
            if not isinstance(entry, dict):
                raise SystemExit(f"syncworthy: [auth] tokens[{i}] must be a table")
            name = str(entry.get("name") or f"token{i}")
            if "token" in entry or "secret" in entry:
                raise SystemExit(f"syncworthy: auth token {name!r}: put the secret in an environment "
                                 "variable or a file (token_env / token_file), not in syncworthy.toml")
            role = entry.get("role", EDIT)
            if role not in ROLES:
                raise SystemExit(f"syncworthy: auth token {name!r}: role must be one of {ROLES}, got {role!r}")
            env, file = entry.get("token_env"), entry.get("token_file")
            if bool(env) == bool(file):
                raise SystemExit(f"syncworthy: auth token {name!r}: give exactly one of token_env / token_file")
            tokens.append(Token(name, _read_secret(name, env, file, environ), role))
        for var, role in ENV_TOKENS:
            if environ.get(var) or environ.get(var + "_FILE"):
                tokens.append(Token(f"env-{role}", _read_secret(var, var if environ.get(var) else None,
                                                                environ.get(var + "_FILE"), environ), role))
        return cls(tokens, getattr(config, "auth_allowed_origins", None))


# ---------------------------------------------------------------------------
# Startup policy
# ---------------------------------------------------------------------------

def is_loopback(host: str) -> bool:
    if host in ("localhost", "ip6-localhost"):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False            # 0.0.0.0, ::, "", a hostname: reachable from elsewhere


def check_bind(auth: Auth, host: str, insecure_open: bool = False) -> None:
    """Refuse to serve an open (token-less) editor on an address others can reach."""
    if auth.enabled or is_loopback(host):
        return
    if insecure_open:
        log.warning("serving on %s with NO authentication (--insecure-open): anyone who can "
                    "reach this port can read and edit the whole project", host)
        return
    raise SystemExit(
        f"syncworthy: refusing to serve on {host!r} with no auth tokens configured: anyone who can "
        "reach this port could read and edit the whole project.\n"
        "  Configure tokens ([auth] in syncworthy.toml, or SYNCWORTHY_EDIT_TOKEN / "
        "SYNCWORTHY_VIEW_TOKEN), bind to 127.0.0.1, or pass --insecure-open to accept the risk.")


# ---------------------------------------------------------------------------
# The ASGI middleware
# ---------------------------------------------------------------------------

LOGIN_PAGE = """<!doctype html><meta charset="utf-8"><title>Sign in</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{{font:16px system-ui,sans-serif;background:#0d0d0f;color:#e8e8f0;display:grid;place-items:center;height:100vh;margin:0}}
form{{display:grid;gap:12px;width:min(22rem,90vw)}}input,button{{font:inherit;padding:.6rem .8rem;border-radius:8px;border:1px solid #333;background:#16161a;color:inherit}}
button{{background:#4ECDC4;color:#0d0d0f;border:0;cursor:pointer}}p{{opacity:.7;margin:0}}</style>
<form method="get" action="{action}"><h2 style="margin:0">{name}</h2>
<p>This project needs an access token.</p>
<input name="token" type="password" placeholder="token" autofocus autocomplete="current-password">
<button>Open</button></form>"""


def _headers(scope) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}


def _cookie_token(headers: dict[str, str]) -> str | None:
    raw = headers.get("cookie")
    if not raw:
        return None
    try:
        jar = SimpleCookie()
        jar.load(raw)
    except Exception:
        return None
    morsel = jar.get(COOKIE)
    return morsel.value if morsel else None


def _bearer(headers: dict[str, str]) -> str | None:
    value = headers.get("authorization", "")
    if value.lower().startswith("bearer "):
        return value.split(" ", 1)[1].strip()
    return None


class AuthMiddleware:
    """Pure ASGI, so one piece of code guards HTTP and WebSocket alike."""

    def __init__(self, app, auth: Auth, app_name: str = "Syncworthy"):
        self.app = app
        self.auth = auth
        self.app_name = app_name

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if kind == "websocket" and not self._origin_ok(scope):
            return await self._reject_ws(receive, send)
        if not self.auth.enabled:
            scope["sw_role"], scope["sw_auth"] = EDIT, False
            return await self.app(scope, receive, send)
        if any(path == p or path.startswith(p + "/") for p in EXEMPT_PATHS):
            return await self.app(scope, receive, send)
        if kind == "http" and path == LOGOUT_PATH:
            return await self._logout(scope, send)
        if kind == "websocket":
            return await self._websocket(scope, receive, send)
        return await self._http(scope, receive, send)

    # ---- websocket ---------------------------------------------------------
    def _origin_ok(self, scope) -> bool:
        headers = _headers(scope)
        origin = headers.get("origin")
        if not origin:
            return True
        host = headers.get("host", "").lower()
        o = urlsplit(origin)
        if o.netloc.lower() == host:
            return True
        return origin.rstrip("/").lower() in self.auth.allowed_origins

    async def _reject_ws(self, receive, send):
        await receive()                                    # websocket.connect
        await send({"type": "websocket.close", "code": 1008})

    async def _websocket(self, scope, receive, send):
        headers = _headers(scope)
        query = dict(parse_qsl(scope.get("query_string", b"").decode("latin-1")))
        tok = None
        for given in (_bearer(headers), _cookie_token(headers), query.get("access_token")):
            tok = self.auth.identify(given)
            if tok:
                break
        if tok is None:
            return await self._reject_ws(receive, send)
        scope["sw_role"], scope["sw_auth"] = tok.role, True
        if tok.role == VIEW and scope.get("path", "").startswith("/yjs"):
            receive = _read_only_yjs(receive)
        return await self.app(scope, receive, send)

    # ---- http ----------------------------------------------------------------
    async def _http(self, scope, receive, send):
        headers = _headers(scope)
        path, method = scope["path"], scope["method"]
        qs = parse_qsl(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True)
        query = dict(qs)
        is_api = path.startswith("/api/")

        from_query = None
        for key in ("token", "access_token"):
            from_query = self.auth.identify(query.get(key)) or from_query
        if from_query is not None and method in ("GET", "HEAD") and not is_api:
            # Present once, keep as a cookie, and leave the URL clean.
            rest = [(k, v) for k, v in qs if k not in ("token", "access_token")]
            location = path + ("?" + urlencode(rest) if rest else "")
            return await _respond(send, 303, b"", [(b"location", location.encode("latin-1")),
                                                    (b"set-cookie", self._cookie(scope, headers, query).encode("latin-1"))])

        tok = (from_query or self.auth.identify(_bearer(headers))
               or self.auth.identify(_cookie_token(headers)))
        if tok is None:
            wants_html = "text/html" in headers.get("accept", "")
            if method in ("GET", "HEAD") and wants_html and not is_api:
                page = LOGIN_PAGE.format(action=html.escape(path, quote=True),
                                         name=html.escape(self.app_name)).encode()
                return await _respond(send, 401, page, [(b"content-type", b"text/html; charset=utf-8")])
            return await _respond(send, 401, json.dumps({"error": "unauthorized"}).encode(),
                                  [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")])
        if tok.role == VIEW and method not in SAFE_METHODS and path not in VIEW_POSTS:
            return await _respond(send, 403, json.dumps({"error": "this token is read-only"}).encode(),
                                  [(b"content-type", b"application/json")])
        scope["sw_role"], scope["sw_auth"] = tok.role, True
        return await self.app(scope, receive, send)

    def _cookie(self, scope, headers, query) -> str:
        secret = query.get("token") if self.auth.identify(query.get("token")) else query.get("access_token")
        secure = scope.get("scheme") == "https" or headers.get("x-forwarded-proto") == "https"
        c = SimpleCookie()
        c[COOKIE] = secret
        m = c[COOKIE]
        m["path"], m["httponly"], m["samesite"], m["max-age"] = "/", True, "Lax", COOKIE_MAX_AGE
        if secure:
            m["secure"] = True
        return m.OutputString()

    async def _logout(self, scope, send):
        await _respond(send, 303, b"", [
            (b"location", b"/"),
            (b"set-cookie", f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax".encode())])


async def _respond(send, status: int, body: bytes, headers: list[tuple[bytes, bytes]]):
    await send({"type": "http.response.start", "status": status,
                "headers": headers + [(b"content-length", str(len(body)).encode()),
                                      (b"cache-control", b"no-store")]})
    await send({"type": "http.response.body", "body": body})


def _read_only_yjs(receive):
    """Wrap a /yjs receive so a view client's document updates never arrive.

    y-websocket frames: ``varuint messageType`` (0 sync, 1 awareness, 2 auth,
    3 query-awareness); a sync message continues with ``varuint syncType`` (0
    step 1, 1 step 2, 2 update).  Step 1 (a request for what the server has) and
    awareness (presence) pass; step 2 and update both carry document changes and
    are dropped.  Anything that does not parse as one of those is dropped too.
    """
    async def filtered():
        while True:
            msg = await receive()
            if msg.get("type") != "websocket.receive":
                return msg
            data = msg.get("bytes")
            if data is None:
                continue                                    # text frames mean nothing on /yjs
            if len(data) >= 1 and data[0] in (1, 3):        # awareness, query-awareness
                return msg
            if len(data) >= 2 and data[0] == 0 and data[1] == 0:   # sync step 1
                return msg
            log.debug("dropped a %d-byte write from a view-only client", len(data))
    return filtered
