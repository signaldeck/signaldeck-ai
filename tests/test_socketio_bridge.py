"""Socket.IO bridge: only routed prompts, no persistent cli-agent sessions."""
import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from signaldeck_ai.chat_runtime import AgentConnection, ChatRuntime
from signaldeck_ai.socketio_bridge import SocketIOChatBridge


class Bus:
    def __init__(self):
        self.listeners = []

    def subscribe(self, callback):
        self.listeners.append(callback)
        return lambda: self.listeners.remove(callback)

    def publish(self, msg):
        for cb in tuple(self.listeners):
            cb(msg)


class Context:
    def __init__(self):
        self.message_bus = Bus()
        self.sent = []

    def message(self, source, content, *, metadata=None):
        self.sent.append((source, content, metadata))
        self.message_bus.publish(SimpleNamespace(
            source=source, content=content, metadata=metadata or {},
        ))


def runtime(with_login=True):
    result = ChatRuntime("http://127.0.0.1:8765/ag-ui")
    if with_login:
        result._connections["connection"] = AgentConnection(
            token="secret", view_key="energie",
        )
        result._views["energie"] = "connection"
    return result


def config():
    return {
        "routes": [{
            "room": "haus", "source": "socketio.ai",
            "response_source": "ai.socketio.haus",
        }],
        "max_queue": 5,
        "timeout_seconds": 30,
    }


def send(ctx, room="haus", event_id="e1", prompt="Wie warm?", source="socketio.ai"):
    ctx.message_bus.publish(SimpleNamespace(
        source=source, content=prompt,
        metadata={"room": room, "event_id": event_id,
                  "transmitter": "family-user"},
    ))


def wait_for(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Timed out waiting for bridge response")


def test_bridge_processes_routed_message_once_and_ignores_other_sources():
    ctx = Context()
    rt = runtime()
    captured = []

    async def ask(prompt, token):
        captured.append((prompt, token))
        return "21 °C"

    bridge = SocketIOChatBridge(config(), ctx, rt, ask=ask)
    try:
        send(ctx)
        send(ctx)  # event replay must not trigger a second run
        send(ctx, source="ai.socketio.haus", event_id="e2")
        send(ctx, room="privat", event_id="e3")
        wait_for(lambda: len(ctx.sent) >= 1)
        assert captured == [("Wie warm?", "secret")]
        assert ctx.sent == [(
            "ai.socketio.haus", "21 °C",
            {"type": "chat", "content_type": "text", "system": False}
        )]
        assert rt._connections["connection"].messages == []
    finally:
        bridge.shutdown()
        assert not ctx.message_bus.listeners


def test_no_login_generates_explanatory_response_and_no_agent_call():
    ctx = Context()
    rt = runtime(False)
    calls = []

    async def ask(prompt, token):
        calls.append((prompt, token))
        return "never"

    bridge = SocketIOChatBridge(config(), ctx, rt, ask=ask)
    try:
        send(ctx)
        wait_for(lambda: len(ctx.sent) == 1)
        assert "nicht verbunden" in ctx.sent[0][1]
        assert calls == []
    finally:
        bridge.shutdown()


def test_queued_requests_are_serialized():
    ctx, rt = Context(), runtime()
    active = 0
    peak = 0
    completed = []

    async def ask(prompt, token):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.025)
        completed.append(prompt)
        active -= 1
        return "Antwort: " + prompt

    bridge = SocketIOChatBridge(config(), ctx, rt, ask=ask)
    try:
        for index in range(3):
            send(ctx, event_id=f"id-{index}", prompt=f"Frage {index}")
        wait_for(lambda: len(ctx.sent) == 3)
        assert peak == 1
        assert completed == ["Frage 0", "Frage 1", "Frage 2"]
    finally:
        bridge.shutdown()


def test_agent_error_is_answered_and_worker_continues():
    ctx, rt = Context(), runtime()
    calls = []

    async def ask(prompt, token):
        calls.append(prompt)
        if prompt == "fail":
            raise RuntimeError("private agent error")
        return "OK"

    bridge = SocketIOChatBridge(config(), ctx, rt, ask=ask)
    try:
        send(ctx, prompt="fail", event_id="1")
        send(ctx, prompt="success", event_id="2")
        wait_for(lambda: len(ctx.sent) == 2)
        assert "nicht bearbeiten" in ctx.sent[0][1]
        assert ctx.sent[1][1] == "OK"
        assert calls == ["fail", "success"]
    finally:
        bridge.shutdown()


def test_bad_config_fails_closed_before_subscribing():
    ctx, rt = Context(), runtime()
    for bad in [
        {}, {"routes": []}, {"routes": [{"room": "haus"}]},
        {"routes": config()["routes"], "max_queue": -1},
        {"routes": config()["routes"], "timeout_seconds": 0},
    ]:
        with pytest.raises(ValueError):
            SocketIOChatBridge(bad, ctx, rt)
    assert ctx.message_bus.listeners == []
