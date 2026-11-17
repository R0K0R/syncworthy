"""
Hub - Real-time hub for NON-YJS functionality.

Strict packet separation:
  /yjs    -> Document content sync   (handled entirely by rooms.py + y-monaco)
  /ws/doc -> Chat, presence, cursors, plugin messages (handled here)

This hub does NOT touch document content or Yjs.  A plugin's previewer is
told when someone opens or leaves a file, and pushes its own messages through
``broadcast`` / ``broadcast_to_file``.
"""
import asyncio
import logging
import json
import uuid
from typing import Dict, Optional, List
from dataclasses import dataclass, field
from fastapi import WebSocket
from pathlib import Path

log = logging.getLogger("syncworthy")


# User colors for avatars
USER_COLORS = [
    "#FF6B6B", "#4ECDC4", "#FFE66D", "#95E1D3",
    "#F38181", "#AA96DA", "#FCBAD3", "#A8D8EA"
]

@dataclass
class User:
    """Connected user (doc-socket only)."""
    id: str
    name: str = "Anonymous"
    color: str = "#FF6B6B"
    websocket: WebSocket = None
    current_file: Optional[str] = None
    token: Optional[str] = None   # stable client token (from localStorage)


class Hub:
    """
    Hub for Chat / File Presence, and the relay for cursor packets.
    Does NOT manage document content (that is the Yjs layer's concern).
    """

    def __init__(self):
        self.users: Dict[str, User] = {}
        self.color_index = 0
        self._lock = asyncio.Lock()
        # Plugin previewers (see plugins.Previewer); the first that handles a
        # path is the one told about it.
        self.previewers: list = []

    def _previewer_for(self, path: str | None):
        if not path:
            return None
        for p in self.previewers:
            try:
                if p.handles(path):
                    return p
            except Exception as e:
                log.error(f"[Hub] previewer.handles failed: {e}")
        return None

    def _get_color(self) -> str:
        color = USER_COLORS[self.color_index % len(USER_COLORS)]
        self.color_index += 1
        return color

    async def connect(self, websocket: WebSocket, name: str = "Anonymous", user_id: str = None, token: str = None) -> User:
        """Register a new user connection."""
        if not user_id:
            user_id = str(uuid.uuid4())[:8]
        elif user_id in self.users and self.users[user_id].websocket != websocket:
            # Guard against duplicated-tab/session ID collisions:
            # never let a new socket overwrite an existing user's connection.
            while user_id in self.users:
                user_id = str(uuid.uuid4())[:8]

        if user_id in self.users:
            user = self.users[user_id]
            user.websocket = websocket
            user.name = name
            if token:
                user.token = token
            await self.broadcast({
                "type": "user_updated",
                "user": {
                    "id": user_id,
                    "name": name,
                    "color": user.color,
                    "file": user.current_file,
                    "token": user.token
                }
            })
        else:
            user = User(
                id=user_id,
                name=name,
                color=self._get_color(),
                websocket=websocket,
                token=token,
            )
            self.users[user_id] = user
            await self.broadcast({
                "type": "user_joined",
                "user": {
                    "id": user_id,
                    "name": name,
                    "color": user.color,
                    "file": user.current_file,
                    "token": user.token
                }
            }, exclude=user_id)

        return user

    async def disconnect(self, user_id: str, websocket: WebSocket = None):
        """Remove a user."""
        if user_id not in self.users:
            return
        user = self.users[user_id]
        if websocket and user.websocket != websocket:
            return
        try:
            prev = self._previewer_for(user.current_file)
            if prev:
                prev.stop(user.current_file)
        except Exception as e:
            log.error(f"[Hub] Error stopping watch during disconnect: {e}")

        del self.users[user_id]
        await self.broadcast({"type": "user_left", "userId": user_id})

    async def join_file(self, user_id: str, path: str):
        """User joins a file (for preview triggering)."""
        if user_id not in self.users:
            return

        path = str(Path(path)) if path else ""
        user = self.users[user_id]

        try:
            prev = self._previewer_for(user.current_file)
            if prev:
                prev.stop(user.current_file)
        except Exception as e:
            log.error(f"[Hub] Error stopping watch: {e}")

        user.current_file = path

        prev = self._previewer_for(path)
        if prev:
            try:
                prev.start(path)
                # Send the previewer's cached state to the joining user.
                for message in await prev.initial_messages(path):
                    await user.websocket.send_text(json.dumps(message))
            except Exception as e:
                log.error(f"[Hub] Error starting watch: {e}")

        # Broadcast the file change to ALL connected clients so their avatar
        # lists update immediately (not just users already on this file).
        await self.broadcast({
            "type": "user_updated",
            "user": {"id": user.id, "name": user.name, "color": user.color,
                     "file": path, "token": user.token},
        }, exclude=user_id)

    async def send_chat(self, user_id: str, text: str, timestamp: int):
        """Broadcast chat message."""
        if user_id not in self.users:
            return
        user = self.users[user_id]
        await self.broadcast({
            "type": "chat",
            "userId": user_id,
            "name": user.name,
            "color": user.color,
            "text": text,
            "timestamp": timestamp,
        })

    async def update_identity(self, user_id: str, name: str):
        """Update user's display name."""
        if user_id not in self.users:
            return
        self.users[user_id].name = name
        u = self.users[user_id]
        await self.broadcast({
            "type": "user_updated",
            "user": {
                "id": user_id,
                "name": name,
                "color": u.color,
                "file": u.current_file,
                "token": u.token
            }
        })

    def get_users(self) -> List[dict]:
        return [
            {"id": u.id, "name": u.name, "color": u.color, "file": u.current_file, "token": u.token}
            for u in self.users.values()
        ]

    def get_users_on_file(self, path: str) -> List[dict]:
        return [
            {"id": u.id, "name": u.name, "color": u.color}
            for u in self.users.values()
            if u.current_file == path
        ]

    async def broadcast(self, message: dict, exclude: str = None):
        msg_json = json.dumps(message)
        for user_id, user in list(self.users.items()):
            if user_id == exclude:
                continue
            try:
                await user.websocket.send_text(msg_json)
            except Exception:
                pass

    async def broadcast_to_file(self, path: str, message: dict, exclude: str = None):
        msg_json = json.dumps(message)
        for user_id, user in list(self.users.items()):
            if user_id == exclude or user.current_file != path:
                continue
            try:
                await user.websocket.send_text(msg_json)
            except Exception:
                pass
