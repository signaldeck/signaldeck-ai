import json
from types import SimpleNamespace

import httpx
import pytest
from flask import Flask

from signaldeck_ai.chat_processor import AGUIChatProcessor
from signaldeck_ai.chat_runtime import ChatActionError, ChatRuntime


class Provider:
    def registerProcessor(self, processor, export):
        pass


def make_processor():
    return AGUIChatProcessor(
        "chat", {"endpoint": "http://127.0.0.1:8765/ag-ui"},
        SimpleNamespace(), Provider(), False,
    )


def test_chat_is_offline_at_startup():
    processor = make_processor()
    assert processor.endpoint == "http://127.0.0.1:8765/ag-ui"
    assert not processor.runtime.contains("missing")
    assert not hasattr(processor, "api_token_env")


@pytest.mark.parametrize("url", [
    "https://evil.example/ag-ui", "http://evil.example:8765/ag-ui",
    "http://127.0.0.1:8765/path", "http://127.0.0.1:8765/ag-ui?token=x",
])
def test_chat_config_rejects_untrusted_endpoints(url):
    with pytest.raises(ValueError):
        AGUIChatProcessor("chat", {"endpoint": url}, SimpleNamespace(), Provider(), False)


def test_native_action_processor_returns_ack_without_rerender(monkeypatch):
    processor = make_processor()
    app = Flask(__name__)
    monkeypatch.setattr(processor.runtime, "verify_and_connect", lambda token, view_key=None: "opaque-connection")
    with app.test_request_context(
        "/run", method="POST",
        headers={"Origin": "http://localhost"},
    ):
        reply = processor.process(["chat"], "action-hash", ai_action="connect", token="secret")
    assert reply == {"ok": True, "connection_id": "opaque-connection"}
    assert "secret" not in json.dumps(reply)
    assert "html" not in reply  # must not replace the live DOM/SSE client


def test_action_processor_forbids_get_and_cross_origin():
    processor = make_processor()
    app = Flask(__name__)
    with app.test_request_context("/run", method="GET"):
        response = processor.process(["chat"], "action-hash", ai_action="connect", token="x")
    assert response["ok"] is False
    with app.test_request_context(
        "/run", method="POST", headers={"Origin": "https://evil.example"},
    ):
        response = processor.process(["chat"], "action-hash", ai_action="connect", token="x")
    assert response["ok"] is False


def make_client(events_for_run):
    requests = []

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/ag-ui/capabilities":
            if request.headers.get("Authorization") != "Bearer test-secret":
                return httpx.Response(401)
            return httpx.Response(200, json={"identity": {"name": "agent"}})
        if request.url.path == "/ag-ui" and request.method == "POST":
            body = json.loads(request.content)
            events = events_for_run(body)
            data = "".join("data: " + json.dumps(item) + "\n\n" for item in events)
            return httpx.Response(200, text=data, headers={"content-type": "text/event-stream"})
        if request.method == "DELETE" and request.url.path.startswith("/threads/"):
            return httpx.Response(204)
        return httpx.Response(404)

    mock_transport = httpx.MockTransport(transport)
    client_factory = lambda **kwargs: httpx.Client(transport=mock_transport, **kwargs)
    return client_factory, requests


def await_run(run):
    with run.changed:
        assert run.changed.wait_for(lambda: run.done, timeout=5), "run did not finish"


def test_agui_async_run_stream_and_thread_lifecycle():
    def events(body):
        assert body["protocolVersion"] == "1.0"
        assert body["messages"][-1]["content"] == "Hallo"
        return [
            {"type": "RUN_STARTED", "runId": body["runId"], "threadId": body["threadId"]},
            {"type": "TEXT_MESSAGE_START", "messageId": "m1", "role": "assistant"},
            {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "Guten"},
            {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": " Tag"},
            {"type": "TEXT_MESSAGE_END", "messageId": "m1"},
            {"type": "RUN_FINISHED", "runId": body["runId"], "threadId": body["threadId"],
             "outcome": {"type": "success"}},
        ]

    factory, requests = make_client(events)
    runtime = ChatRuntime("http://127.0.0.1:8765/ag-ui", http_client_factory=factory)
    identifier = runtime.verify_and_connect("test-secret")
    run_id = runtime.start_message(identifier, "Hallo")
    run = runtime.get_run(identifier, run_id)
    await_run(run)
    assert run.answer == "Guten Tag"
    assert run.events[-1][1]["type"] == "RUN_FINISHED"
    assert any(r.url.path == "/ag-ui" for r in requests)
    assert runtime.contains(identifier)
    runtime.disconnect(identifier)
    assert not runtime.contains(identifier)


def test_agui_interrupt_requires_correct_resume_and_keeps_thread():
    captures = []

    def events(body):
        captures.append(body)
        if "resume" not in body:
            return [
                {"type": "RUN_STARTED"},
                {"type": "RUN_FINISHED", "outcome": {
                    "type": "interrupt",
                    "interrupts": [{"id": "approval-1", "reason": "tool_call"}],
                }},
            ]
        return [
            {"type": "RUN_STARTED"},
            {"type": "RUN_FINISHED", "outcome": {"type": "success"}},
        ]

    factory, _ = make_client(events)
    runtime = ChatRuntime("http://127.0.0.1:8765/ag-ui", http_client_factory=factory)
    identifier = runtime.verify_and_connect("test-secret")
    first = runtime.start_message(identifier, "Steuere das Gerät")
    await_run(runtime.get_run(identifier, first))
    with pytest.raises(ChatActionError):
        runtime.start_message(identifier, "Andere Frage")
    with pytest.raises(ChatActionError):
        runtime.approve(identifier, "wrong-id", "yes")
    second = runtime.approve(identifier, "approval-1", "yes")
    await_run(runtime.get_run(identifier, second))
    assert captures[0]["threadId"] == captures[1]["threadId"]
    assert captures[1]["resume"][0]["interruptId"] == "approval-1"
    assert captures[1]["resume"][0]["payload"]["approved"] is True
    runtime.shutdown()


def test_token_rejected_without_storing_session():
    factory, _ = make_client(lambda _: [])
    runtime = ChatRuntime("http://127.0.0.1:8765/ag-ui", http_client_factory=factory)
    with pytest.raises(ChatActionError):
        runtime.verify_and_connect("wrong-secret")
    assert runtime._connections == {}


def test_reset_history_starts_fresh_thread_and_clears_agent_context():
    captured = []
    factory, requests = make_client(lambda body: (
        captured.append(body) or [
            {"type": "RUN_STARTED", "runId": body["runId"]},
            {"type": "RUN_FINISHED", "outcome": {"type": "success"}},
        ]
    ))
    runtime = ChatRuntime("http://127.0.0.1:8765/ag-ui", http_client_factory=factory)
    identifier = runtime.verify_and_connect("test-secret")
    first_run_id = runtime.start_message(identifier, "Alte Nachricht")
    await_run(runtime.get_run(identifier, first_run_id))
    first_thread = captured[0]["threadId"]
    assert captured[0]["messages"][0]["content"] == "Alte Nachricht"

    runtime.new_chat(identifier)
    with pytest.raises(ChatActionError):
        runtime.get_run(identifier, first_run_id)
    assert runtime.contains(identifier)  # Token and connection survive reset
    assert runtime._connections[identifier].messages == []
    assert runtime._connections[identifier].pending_interrupt is None

    next_run_id = runtime.start_message(identifier, "Neues Thema")
    await_run(runtime.get_run(identifier, next_run_id))
    assert captured[1]["threadId"] != first_thread
    assert [m["content"] for m in captured[1]["messages"]] == ["Neues Thema"]
    runtime.shutdown()


def test_reset_history_allowed_after_interrupt_and_discards_pending_approval():
    factory, _ = make_client(lambda body: [
        {"type": "RUN_STARTED"},
        {"type": "RUN_FINISHED", "outcome": {
            "type": "interrupt", "interrupts": [{"id": "obsolete-approval"}]
        }},
    ])
    runtime = ChatRuntime("http://127.0.0.1:8765/ag-ui", http_client_factory=factory)
    identifier = runtime.verify_and_connect("test-secret")
    run_id = runtime.start_message(identifier, "Trigger approval")
    await_run(runtime.get_run(identifier, run_id))
    assert runtime._connections[identifier].pending_interrupt
    runtime.new_chat(identifier)
    assert runtime._connections[identifier].pending_interrupt is None
    with pytest.raises(ChatActionError, match="Keine passende"):
        runtime.approve(identifier, "obsolete-approval", "yes")
    runtime.shutdown()


def test_native_reset_history_action_returns_ack_without_rerender(monkeypatch):
    processor = make_processor()
    calls = []
    monkeypatch.setattr(processor.runtime, "new_chat", lambda identifier: calls.append(identifier))
    monkeypatch.setattr(processor.runtime, "assert_view", lambda value, identifier: None)
    app = Flask(__name__)
    with app.test_request_context(
        "/run", method="POST", headers={"Origin": "http://localhost"},
    ):
        reply = processor.process(
            ["chat"], "action-hash", ai_action="reset_history", connection_id="opaque"
        )
    assert reply == {"ok": True, "history_reset": True}
    assert calls == ["opaque"]
    assert "html" not in reply
