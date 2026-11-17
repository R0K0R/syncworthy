"""MCP over Streamable HTTP: an agent edits the document, not the file.

The file is the document's export.  Writing it behind a live room's back gets
the old text put straight back over the new one at the room's next save, and
nobody connected ever sees the edit.  Every tool here goes through the room
instead, so an edit reaches each open editor the way a keystroke does and the
file follows on the room's own save.

Spoken over Streamable HTTP (one POST, one JSON-RPC answer) rather than stdio:
the rooms live in this process, and an agent elsewhere can reach them over the
same port the editor already uses::

    claude mcp add --transport http <name> http://<host>:<port>/mcp

Generic tools: list_documents, read_document, edit_document, append_document.
Plugins add more (``Plugin.mcp_tools``).
"""
from __future__ import annotations

import hmac
import inspect
import logging
import os
from pathlib import Path

from pycrdt import Text

from .files import list_documents, resolve_in_project
from .rooms import find_edit_hits

log = logging.getLogger("syncworthy")

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

TOOLS = [
    {
        "name": "list_documents",
        "description": (
            "List the project's editable documents, saying which are open in a "
            "live room right now (someone has the file open) and how long each is."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "under": {"type": "string",
                          "description": "Only paths under this directory, e.g. 'content/8'"},
            },
        },
    },
    {
        "name": "read_document",
        "description": (
            "Read a document. Returns the live room's text when the file is open "
            "in an editor -- which can be ahead of what is on disk -- and the file "
            "otherwise."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string",
                                    "description": "Project-relative, e.g. 'content/8/2.typ'"}},
            "required": ["path"],
        },
    },
    {
        "name": "edit_document",
        "description": (
            "Replace old_string with new_string in a document, through its room, so "
            "every open editor sees the edit and nothing of theirs is overwritten. "
            "old_string must appear exactly once unless replace_all is set."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
                "replace_all": {"type": "boolean", "default": False},
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
    {
        "name": "append_document",
        "description": "Add text to the end of a document, through its room.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "text": {"type": "string"}},
            "required": ["path", "text"],
        },
    },
]


def expected_token(env_name: str) -> str | None:
    """The token every request must carry, from the environment or a file.

    ``<ENV>_FILE`` names a file holding the secret, so it can live somewhere
    only the process can read (/run/agenix/..., mode 400) rather than in a unit
    file or a store path that is world-readable by construction.
    """
    path = os.environ.get(env_name + "_FILE")
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip() or None
        except OSError as e:
            log.error("[MCP] token file %s unreadable: %s", path, e)
            return None
    return (os.environ.get(env_name) or "").strip() or None


def authorize(header: str | None, query_token: str | None, env_name: str) -> tuple[bool, str]:
    """Whether a request may proceed, and why not when it may not.

    Fails closed.  With no token configured the endpoint refuses everyone,
    rather than serving whoever can reach it: the tools here read and rewrite
    the project, and `tailscale funnel' arrives over loopback like anything
    else, so there is no address this could trust its way out of.
    """
    expected = expected_token(env_name)
    if not expected:
        return False, f"this server has no {env_name} set, so it serves nobody"
    # A query token as well as a header, for clients that cannot send one
    # (Claude's mobile and web connectors authenticate by OAuth or not at all).
    # A token in the URL is weaker -- it is kept in the connector's config and
    # shows up in any logging that records URLs -- so it is the fallback.
    if query_token:
        given = query_token.strip()
    elif header and header.lower().startswith("bearer "):
        given = header.split(" ", 1)[1].strip()
    else:
        return False, "missing bearer token"
    if not hmac.compare_digest(given, expected):
        return False, "bad token"
    return True, ""


def _edit_on_disk(path: Path, old: str, new: str, replace_all: bool = False) -> int:
    """The same edit, for a file nobody has open.

    Same rules as the room path -- one match unless told otherwise -- so
    whether an edit is refused does not depend on who happens to be editing.
    """
    content = path.read_text(encoding="utf-8")
    hits = find_edit_hits(content, old, replace_all=replace_all)
    for i in reversed(hits):
        content = content[:i] + new + content[i + len(old):]
    tmp = path.with_name(f".{path.name}.sw-tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
    return len(hits)


class McpServer:
    def __init__(self, state, plugin_tools):
        """STATE is the AppState; PLUGIN_TOOLS a list of (PluginContext, McpTool)."""
        self.state = state
        self.plugin_tools = {t.name: (ctx, t) for ctx, t in plugin_tools}
        self.server_info = {"name": state.app_name.lower().replace(" ", "-"), "version": "1.0.0"}

    def tools(self) -> list[dict]:
        extra = [{"name": t.name, "description": t.description, "inputSchema": t.input_schema}
                 for _, t in self.plugin_tools.values()]
        return TOOLS + extra

    def _safe_path(self, rel: str) -> Path:
        target = resolve_in_project(self.state.root, rel)
        if target is None:
            raise ValueError(f"path outside the project: {rel}")
        return target.resolve()

    async def call_tool(self, name: str, args: dict):
        st = self.state
        rooms = st.rooms.rooms

        if name in self.plugin_tools:
            ctx, tool = self.plugin_tools[name]
            result = tool.handler(ctx, args)
            if inspect.isawaitable(result):
                result = await result
            return result

        if name == "list_documents":
            paths = list_documents(st.root, st.ignore, st.text_suffixes, args.get("under"))
            lines = []
            for rel in paths:
                room = rooms.get(rel)
                if room is not None:
                    try:
                        n = len(str(room.ydoc.get("content", type=Text)))
                        lines.append(f"{rel}  [open now, {n} chars]")
                        continue
                    except Exception:
                        pass
                try:
                    n = len(self._safe_path(rel).read_text(encoding="utf-8"))
                except Exception:
                    n = 0
                lines.append(f"{rel}  [{n} chars]")
            return "\n".join(lines) if lines else "(no documents)"

        if name == "read_document":
            rel = args["path"]
            room = rooms.get(rel)
            if room is not None:
                return str(room.ydoc.get("content", type=Text))
            return self._safe_path(rel).read_text(encoding="utf-8")

        if name == "edit_document":
            rel = args["path"]
            path = self._safe_path(rel)
            old, new = args["old_string"], args["new_string"]
            all_of_them = bool(args.get("replace_all"))
            if rel in rooms:
                # Somebody has it open.  The edit goes into the document as the
                # one deletion and insertion it is, so what reaches them is that
                # much of a change and no more -- their cursor, their scroll and
                # their undo survive it.  Replacing the text wholesale is what
                # `rooms/reload' does, and it takes the editor out from under them.
                n = await st.rooms.edit_room(rel, old, new, replace_all=all_of_them)
                return (f"replaced {n} occurrence(s) in {rel}, as a delta through the "
                        "live room: open editors have it, and the room saves the file")
            n = _edit_on_disk(path, old, new, replace_all=all_of_them)
            return f"replaced {n} occurrence(s) in {rel} on disk; no editor has it open"

        if name == "append_document":
            rel = args["path"]
            path = self._safe_path(rel)
            addition = args["text"]
            if rel in rooms:
                text = rooms[rel].ydoc.get("content", type=Text)
                text += addition          # an insert at the end: again, a delta
                return f"appended {len(addition)} chars to {rel} through the live room"
            with path.open("a", encoding="utf-8") as fh:
                fh.write(addition)
            return f"appended {len(addition)} chars to {rel} on disk"

        raise ValueError(f"unknown tool: {name}")

    async def handle_rpc(self, msg: dict) -> dict | None:
        """One JSON-RPC message in, one answer out (None for a notification)."""
        method = msg.get("method")
        mid = msg.get("id")

        if method is not None and mid is None:
            # A notification: nothing to answer, including `initialized'.
            return None

        def ok(result):
            return {"jsonrpc": "2.0", "id": mid, "result": result}

        def err(code, message):
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}

        if method == "initialize":
            asked = (msg.get("params") or {}).get("protocolVersion")
            version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return ok({
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": self.server_info,
            })

        if method == "ping":
            return ok({})

        if method == "tools/list":
            return ok({"tools": self.tools()})

        if method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name", "")
            args = params.get("arguments") or {}
            try:
                result = await self.call_tool(name, args)
                # A tool may answer in blocks -- a render hands back pictures, and a
                # picture is the whole point of asking it.
                content = (result if isinstance(result, list)
                           else [{"type": "text", "text": result}])
                return ok({"content": content, "isError": False})
            except Exception as e:
                log.warning("[MCP] %s failed: %s: %s", name, type(e).__name__, e)
                # A failed tool is a result, not a protocol error: the agent is
                # meant to read what went wrong and try something else.
                return ok({"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}],
                           "isError": True})

        return err(-32601, f"method not found: {method}")
