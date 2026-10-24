"""Bounded, in-memory AG-UI thread and SSE run management.

User commands arrive through SignalDeck's existing /run ActionDispatcher.
Only server-to-browser events use the plugin's read-only SSE route.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx


MAX_MESSAGE_CHARS = 12_000
MAX_HISTORY_MESSAGES = 160
MAX_RUN_EVENTS = 4096
MAX_EVENT_BYTES = 256 * 1024
CONNECTION_IDLE_SECONDS = 30 * 60
MAX_CONNECTIONS = 32


class ChatActionError(ValueError):
    """Expected, safely displayable error in a user action."""


@dataclass
class AgentRun:
    run_id: str
    thread_id: str
    payload: dict[str, Any] = field(repr=False)
    events: deque[tuple[int, dict[str, Any]]] = field(
        default_factory=lambda: deque(maxlen=MAX_RUN_EVENTS), repr=False
    )
    next_sequence: int = 1
    done: bool = False
    interrupted: bool = False
    failed: bool = False
    answer: str = ""
    _message_fragments: dict[str, str] = field(default_factory=dict, repr=False)
    changed: threading.Condition = field(default_factory=threading.Condition, repr=False)

    def emit(self, event: dict[str, Any]) -> None:
        if len(json.dumps(event, ensure_ascii=False).encode("utf-8")) > MAX_EVENT_BYTES:
            raise ValueError("AG-UI event exceeds maximum size")
        with self.changed:
            self.events.append((self.next_sequence, event))
            self.next_sequence += 1
            kind = event.get("type")
            if kind == "TEXT_MESSAGE_START" and event.get("role") == "assistant":
                self._message_fragments[event["messageId"]] = ""
            elif kind == "TEXT_MESSAGE_CONTENT":
                message_id = event.get("messageId")
                if message_id in self._message_fragments:
                    self._message_fragments[message_id] += str(event.get("delta") or "")
            elif kind == "TEXT_MESSAGE_END":
                msg = self._message_fragments.pop(event.get("messageId"), None)
                if msg is not None:
                    self.answer += msg
            elif kind == "RUN_FINISHED":
                self.interrupted = event.get("outcome", {}).get("type") == "interrupt"
                self.done = True
            elif kind == "RUN_ERROR":
                self.failed = True
                self.done = True
            self.changed.notify_all()

    def finish(self) -> None:
        with self.changed:
            if not self.done:
                self.done = True
                self.changed.notify_all()


@dataclass
class AgentConnection:
    token: str = field(repr=False)
    thread_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    messages: list[dict[str, Any]] = field(default_factory=list, repr=False)
    runs: dict[str, AgentRun] = field(default_factory=dict, repr=False)
    current_run: str | None = None
    pending_interrupt: dict[str, Any] | None = field(default=None, repr=False)
    last_used: float = field(default_factory=time.monotonic)
    revoked: bool = False


class ChatRuntime:
    """One runtime per DisplayProcessor instance, not shared between users."""

    def __init__(
        self, endpoint: str, *,
        http_client_factory: Callable[..., httpx.Client] = httpx.Client,
    ):
        self.endpoint = endpoint
        self._client_factory = http_client_factory
        self._lock = threading.RLock()
        self._connections: dict[str, AgentConnection] = {}

    def _expire_locked(self) -> None:
        now = time.monotonic()
        for ident, connection in tuple(self._connections.items()):
            if now - connection.last_used > CONNECTION_IDLE_SECONDS:
                self._connections.pop(ident, None)
                connection.revoked = True
                # Expired credentials are discarded. Backend thread cleanup is
                # best effort and must not block the request holding this lock.
                self._spawn_cleanup(connection)

    def _connection_locked(self, identifier: str) -> AgentConnection:
        self._expire_locked()
        connection = self._connections.get(identifier)
        if connection is None or connection.revoked:
            raise ChatActionError("Chat-Verbindung nicht mehr aktiv. Bitte neu verbinden.")
        connection.last_used = time.monotonic()
        return connection

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "text/event-stream",
        }

    def verify_and_connect(self, token: str) -> str:
        if not isinstance(token, str) or not 1 <= len(token) <= 4096:
            raise ChatActionError("API-Token fehlt oder ist ungültig.")
        # cli-agent supports the optional capabilities discovery endpoint.
        # No connection state is created until the supplied credential works.
        try:
            with self._client_factory(timeout=8.0) as client:
                response = client.get(
                    self.endpoint.removesuffix("/ag-ui") + "/ag-ui/capabilities",
                    headers=self._headers(token),
                )
                if response.status_code in (401, 403):
                    raise ChatActionError("API-Token wurde vom Agenten abgelehnt.")
                response.raise_for_status()
                if not isinstance(response.json(), dict):
                    raise ChatActionError("Ungültige Agenten-Antwort.")
        except httpx.HTTPError as exc:
            raise ChatActionError("AG-UI-Agent nicht erreichbar.") from exc

        with self._lock:
            self._expire_locked()
            if len(self._connections) >= MAX_CONNECTIONS:
                raise ChatActionError("Zu viele aktive Chat-Verbindungen.")
            identifier = secrets.token_urlsafe(32)
            self._connections[identifier] = AgentConnection(token=token)
            return identifier

    def contains(self, identifier: str) -> bool:
        with self._lock:
            try:
                self._connection_locked(identifier)
            except ChatActionError:
                return False
            return True

    def start_message(self, identifier: str, message: str) -> str:
        if not isinstance(message, str) or not message.strip() or len(message) > MAX_MESSAGE_CHARS:
            raise ChatActionError("Nachricht fehlt oder ist zu lang.")
        with self._lock:
            connection = self._connection_locked(identifier)
            self._assert_idle(connection)
            if connection.pending_interrupt:
                raise ChatActionError("Zunächst die ausstehende Tool-Freigabe beantworten.")
            if len(connection.messages) >= MAX_HISTORY_MESSAGES:
                raise ChatActionError("Chat-History-Limit erreicht. Bitte neuen Chat starten.")
            connection.messages.append({
                "id": uuid.uuid4().hex,
                "role": "user",
                "content": message.strip(),
            })
            return self._submit_locked(connection)

    def approve(self, identifier: str, interrupt_id: str, decision: str) -> str:
        if decision not in {"yes", "no", "session"}:
            raise ChatActionError("Ungültige Freigabeentscheidung.")
        with self._lock:
            connection = self._connection_locked(identifier)
            self._assert_idle(connection)
            pending = connection.pending_interrupt
            if pending is None or pending.get("id") != interrupt_id:
                raise ChatActionError("Keine passende ausstehende Tool-Freigabe.")
            resume = [{
                "interruptId": interrupt_id,
                "status": "resolved",
                "payload": {
                    "approved": decision != "no",
                    "rememberForSession": decision == "session",
                },
            }]
            connection.pending_interrupt = None
            return self._submit_locked(connection, resume=resume)

    def _assert_idle(self, connection: AgentConnection) -> None:
        if connection.current_run is not None:
            existing = connection.runs.get(connection.current_run)
            if existing is not None and not existing.done:
                raise ChatActionError("Agent bearbeitet bereits eine Anfrage.")

    def _submit_locked(
        self, connection: AgentConnection, *, resume: list[dict[str, Any]] | None = None,
    ) -> str:
        run_id = uuid.uuid4().hex
        payload = {
            "protocolVersion": "1.0",
            "threadId": connection.thread_id,
            "runId": run_id,
            "messages": [dict(msg) for msg in connection.messages],
        }
        if resume is not None:
            payload["resume"] = resume
        run = AgentRun(run_id=run_id, thread_id=connection.thread_id, payload=payload)
        connection.current_run = run_id
        connection.runs = {run_id: run}  # keep one bounded run replay per chat
        worker = threading.Thread(
            target=self._worker, args=(connection, run), daemon=True,
            name="signaldeck-agui-" + run_id[:8],
        )
        worker.start()
        return run_id

    def _worker(self, connection: AgentConnection, run: AgentRun) -> None:
        try:
            timeout = httpx.Timeout(connect=8.0, read=None, write=20.0, pool=8.0)
            with self._client_factory(timeout=timeout) as client:
                with client.stream(
                    "POST", self.endpoint,
                    headers={**self._headers(connection.token), "Content-Type": "application/json"},
                    json=run.payload,
                ) as response:
                    if response.status_code != 200:
                        raise ChatActionError(
                            f"Agent hat die Anfrage abgelehnt (HTTP {response.status_code})."
                        )
                    lines: list[str] = []
                    for line in response.iter_lines():
                        if connection.revoked:
                            return
                        if line == "":
                            if lines:
                                self._receive_frame(connection, run, lines)
                                lines = []
                        elif line.startswith("data:"):
                            lines.append(line[5:].lstrip(" "))
                    if lines:
                        self._receive_frame(connection, run, lines)
            if not run.done:
                run.emit({"type": "RUN_ERROR", "message": "AG-UI-Stream unerwartet beendet."})
        except Exception:
            if not run.done and not connection.revoked:
                run.emit({"type": "RUN_ERROR", "message": "AG-UI-Agentenlauf fehlgeschlagen."})
        finally:
            with self._lock:
                if not connection.revoked and connection.current_run == run.run_id:
                    if run.done and not run.failed:
                        if run.answer and not run.interrupted:
                            connection.messages.append({
                                "id": uuid.uuid4().hex,
                                "role": "assistant",
                                "content": run.answer[:100_000],
                            })
                    else:
                        # Reset ambiguous history rather than retrying an
                        # action whose previous execution status is unknown.
                        connection.pending_interrupt = None
                run.finish()

    def _receive_frame(self, connection: AgentConnection, run: AgentRun, lines: list[str]):
        try:
            event = json.loads("\n".join(lines))
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise ValueError("Malformed AG-UI event")
            if event.get("type") == "RUN_FINISHED":
                outcome = event.get("outcome") or {}
                if outcome.get("type") == "interrupt":
                    interrupts = outcome.get("interrupts") or []
                    with self._lock:
                        if not connection.revoked and connection.current_run == run.run_id:
                            connection.pending_interrupt = interrupts[0] if interrupts else None
            run.emit(event)
        except (json.JSONDecodeError, ValueError, TypeError, KeyError) as exc:
            raise ChatActionError("Ungültiges AG-UI-Event.") from exc

    def get_run(self, identifier: str, run_id: str) -> AgentRun:
        with self._lock:
            connection = self._connection_locked(identifier)
            run = connection.runs.get(run_id)
            if run is None:
                raise ChatActionError("AG-UI-Run nicht gefunden.")
            return run

    def new_chat(self, identifier: str) -> None:
        with self._lock:
            connection = self._connection_locked(identifier)
            self._assert_idle(connection)
            previous_thread = connection.thread_id
            connection.thread_id = uuid.uuid4().hex
            connection.messages = []
            connection.pending_interrupt = None
            connection.current_run = None
            connection.runs.clear()
            self._spawn_cleanup(connection, previous_thread)

    def disconnect(self, identifier: str) -> None:
        with self._lock:
            connection = self._connections.pop(identifier, None)
            if connection is None:
                raise ChatActionError("Chat-Verbindung nicht gefunden.")
            connection.revoked = True
            self._spawn_cleanup(connection)

    def _spawn_cleanup(self, connection: AgentConnection, thread_id: str | None = None):
        token = connection.token
        thread = thread_id or connection.thread_id

        def cleanup():
            try:
                with self._client_factory(timeout=8.0) as client:
                    client.delete(
                        self.endpoint.removesuffix("/ag-ui") + "/threads/" + thread,
                        headers=self._headers(token),
                    )
            except httpx.HTTPError:
                pass

        threading.Thread(target=cleanup, daemon=True, name="signaldeck-agui-cleanup").start()

    def shutdown(self) -> None:
        with self._lock:
            connections = tuple(self._connections.values())
            self._connections.clear()
            for connection in connections:
                connection.revoked = True
                self._spawn_cleanup(connection)
