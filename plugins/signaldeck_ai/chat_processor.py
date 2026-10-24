"""DisplayData-backed generic AG-UI chat with runtime, per-tab credentials."""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from threading import RLock
from urllib.parse import urlsplit

from signaldeck_sdk import DisplayProcessor, DisplayData


@dataclass
class AgentConnection:
    """A temporary, private server-side credential and its AG-UI threads."""

    token: str = field(repr=False)
    threads: set[str] = field(default_factory=set)
    last_used: float = field(default_factory=time.monotonic)


class ChatDisplayData(DisplayData):
    def __init__(self, ctx, action_hash, processor):
        super().__init__(ctx, action_hash)
        self.name = processor.name
        self.api_base = f"/plugin/ai/api/chat/{processor.name}"


class AGUIChatProcessor(DisplayProcessor):
    """An AG-UI endpoint stays offline until a user supplies a token in the UI.

    Credentials exist only in the backend process, are scoped to a random
    browser-held connection identifier, and are never persisted or exported as
    DisplayData. A connection expires after inactivity and on shutdown.
    """

    CONNECTION_IDLE_SECONDS = 30 * 60
    MAX_CONNECTIONS = 32

    def __init__(self, name, config, ctx, valueProvider, collect_data):
        super().__init__(name, config, ctx, valueProvider, collect_data)
        endpoint = str(config.get("endpoint", "")).rstrip("/")
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/ag-ui"}
        ):
            raise ValueError("AG-UI endpoint must be http://localhost:<port>/ag-ui")
        if parsed.port is None or parsed.port < 1 or parsed.port > 65535:
            raise ValueError("AG-UI endpoint requires a TCP port")
        self.endpoint = endpoint if parsed.path == "/ag-ui" else endpoint + "/ag-ui"
        self._connections: dict[str, AgentConnection] = {}
        self._connections_lock = RLock()

    def _expire_locked(self) -> None:
        now = time.monotonic()
        for connection_id, connection in list(self._connections.items()):
            if now - connection.last_used >= self.CONNECTION_IDLE_SECONDS:
                del self._connections[connection_id]

    def connect(self, token: str) -> str:
        """Store a verified token and return an opaque, memory-only handle."""
        if not isinstance(token, str) or not token or len(token) > 4096:
            raise ValueError("Invalid AG-UI token")
        with self._connections_lock:
            self._expire_locked()
            if len(self._connections) >= self.MAX_CONNECTIONS:
                raise RuntimeError("Too many active chat connections")
            connection_id = secrets.token_urlsafe(32)
            self._connections[connection_id] = AgentConnection(token=token)
            return connection_id

    def connection(self, connection_id: str) -> AgentConnection | None:
        with self._connections_lock:
            self._expire_locked()
            connection = self._connections.get(connection_id)
            if connection is not None:
                connection.last_used = time.monotonic()
            return connection

    def claim_thread(self, connection_id: str, thread_id: str) -> AgentConnection | None:
        if not thread_id or len(thread_id) > 128:
            return None
        with self._connections_lock:
            self._expire_locked()
            connection = self._connections.get(connection_id)
            if connection is None:
                return None
            if any(
                thread_id in other.threads
                for ident, other in self._connections.items()
                if ident != connection_id
            ):
                return None
            connection.threads.add(thread_id)
            connection.last_used = time.monotonic()
            return connection

    def owns_thread(self, connection_id: str, thread_id: str) -> bool:
        with self._connections_lock:
            self._expire_locked()
            connection = self._connections.get(connection_id)
            return connection is not None and thread_id in connection.threads

    def release_thread(self, connection_id: str, thread_id: str) -> None:
        with self._connections_lock:
            connection = self._connections.get(connection_id)
            if connection is not None:
                connection.threads.discard(thread_id)

    def disconnect(self, connection_id: str) -> AgentConnection | None:
        with self._connections_lock:
            return self._connections.pop(connection_id, None)

    def shutdown(self):
        with self._connections_lock:
            self._connections.clear()

    def getTemplate(self, value):
        return "ai/chat.html"

    def getDisplayData(self, value, actionHash, **kwargs):
        return ChatDisplayData(self.ctx, actionHash, self)

    def getAdditionalJsFiles(self, value):
        return [("signaldeck_ai", "js/chat.js")]

    def getAdditionalCssFiles(self, value):
        return [("signaldeck_ai", "css/chat.css")]

    def getJS_functions_to_call_on_client(self, data):
        return {f"signaldeck-ai-chat-{data.hash}": "initSignalDeckAIChat"}
