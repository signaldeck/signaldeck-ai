"""Opt-in asynchronous one-shot AG-UI bridge for routed Socket.IO messages.

No socketio dependency: inbound/outbound traffic uses SignalDeck's MessageBus.
Requests are serialized to avoid consuming cli-agent's eight session slots.
"""
from __future__ import annotations

import asyncio
import logging
import queue
import threading
from collections import deque
from collections.abc import Mapping
from typing import Any, Callable

from .chat_runtime import ChatActionError, ChatRuntime, MAX_MESSAGE_CHARS
from .llm_command import LLMCommand

_LOG = logging.getLogger(__name__)


class SocketIOChatBridge:
    def __init__(
        self, config: dict[str, Any], ctx: Any, runtime: ChatRuntime, *,
        ask: Callable[[str, str], Any] | None = None,
    ):
        if not isinstance(config, dict) or set(config) - {
            "routes", "max_queue", "timeout_seconds"
        }:
            raise ValueError("Invalid socketio_bridge configuration")
        routes = config.get("routes")
        if not isinstance(routes, list) or not routes:
            raise ValueError("socketio_bridge.routes requires at least one route")
        self.routes: dict[tuple[str, str], str] = {}
        for route in routes:
            if not isinstance(route, dict) or set(route) != {
                "room", "source", "response_source"
            }:
                raise ValueError(
                    "Each socketio_bridge route requires room, source, response_source"
                )
            if any(not isinstance(v, str) or not v.strip() or v != v.strip()
                   for v in route.values()):
                raise ValueError("Invalid socketio_bridge route")
            key = (route["room"], route["source"])
            if key in self.routes:
                raise ValueError("Duplicate socketio_bridge room/source route")
            self.routes[key] = route["response_source"]

        max_queue = config.get("max_queue", 5)
        if type(max_queue) is not int or not 0 <= max_queue <= 100:
            raise ValueError("socketio_bridge.max_queue must be 0..100")
        timeout = config.get("timeout_seconds", 120)
        if type(timeout) is not int or not 1 <= timeout <= 600:
            raise ValueError("socketio_bridge.timeout_seconds must be 1..600")

        self.ctx = ctx
        self.runtime = runtime
        # One active run plus exactly max_queue waiting requests.
        self._slots = threading.BoundedSemaphore(max_queue + 1)
        self._queue: queue.Queue[tuple[str, str, str]] = queue.Queue(maxsize=max_queue + 1)
        self._stopped = threading.Event()
        self._guard = threading.RLock()
        self._seen: set[tuple[str, str]] = set()
        self._seen_order: deque[tuple[str, str]] = deque()
        self._ask = ask
        self._command = LLMCommand("socketio_bridge_oneshot", runtime, timeout_seconds=timeout)
        self._unsubscribe = ctx.message_bus.subscribe(self.on_message)
        self._worker = threading.Thread(
            target=self._run, name="signaldeck-socketio-ai", daemon=True
        )
        self._worker.start()

    def _publish(self, source: str, content: str, room: str) -> None:
        if not self._stopped.is_set():
            self.ctx.message(
                source=source, content=content,
                metadata={"type": "chat", "content_type": "text", "system": False},
            )

    def on_message(self, message: Any) -> None:
        meta = message.metadata or {}
        if not isinstance(meta, Mapping):
            return
        room = meta.get("room")
        response_source = self.routes.get((room, message.source))
        if response_source is None or not isinstance(message.content, str):
            return
        prompt = message.content.strip()
        if not prompt or len(prompt) > MAX_MESSAGE_CHARS:
            return

        event_id = meta.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            return
        key = (room, event_id)
        with self._guard:
            if self._stopped.is_set() or key in self._seen:
                return
            # Keep only bounded de-duplication memory.
            self._seen.add(key)
            self._seen_order.append(key)
            while len(self._seen_order) > 1024:
                self._seen.discard(self._seen_order.popleft())
            if not self._slots.acquire(blocking=False):
                self._publish(
                    response_source,
                    "Der Haus-Assistent ist ausgelastet. Bitte später erneut versuchen.",
                    room,
                )
                return
            try:
                self._queue.put_nowait((response_source, room, prompt))
            except queue.Full:
                self._slots.release()
                self._publish(
                    response_source,
                    "Der Haus-Assistent ist ausgelastet. Bitte später erneut versuchen.",
                    room,
                )

    def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                response_source, room, prompt = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if self._stopped.is_set():
                    break
                try:
                    token = self.runtime.command_token()
                    if self._ask is None:
                        answer = asyncio.run(
                            self._command._ask_once(
                                prompt, token, stop_event=self._stopped
                            )
                        )
                    else:
                        answer = asyncio.run(self._ask(prompt, token))
                except ChatActionError:
                    answer = "Haus-Assistent nicht verbunden. Bitte zuerst in einer AI-Chat-View anmelden."
                except Exception:
                    _LOG.exception("Socket.IO AI one-shot failed for room=%s", room)
                    answer = "Der Haus-Assistent konnte die Anfrage nicht bearbeiten."
                if answer:
                    self._publish(response_source, answer, room)
            finally:
                self._queue.task_done()
                self._slots.release()

    def shutdown(self) -> None:
        with self._guard:
            self._stopped.set()
            self._unsubscribe()
        self._worker.join(timeout=2.0)
