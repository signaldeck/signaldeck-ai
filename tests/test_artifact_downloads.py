"""AG-UI document downloads stay on the authenticated SignalDeck connection."""
import json
from types import SimpleNamespace

from flask import Flask

from signaldeck_ai.chat_runtime import AgentConnection, AgentRun, ChatRuntime, ChatActionError
from signaldeck_ai.plugin import bp


def test_document_result_is_persisted_only_for_current_thread():
    runtime = ChatRuntime("http://127.0.0.1:8765/ag-ui")
    connection = AgentConnection(token="secret", thread_id="thread-123")
    runtime._connections["connection"] = connection
    run = AgentRun(run_id="run-123", thread_id=connection.thread_id, payload={})
    connection.current_run = run.run_id
    connection.runs[run.run_id] = run

    artifact_id = "a" * 32
    event = {
        "type": "TOOL_CALL_RESULT",
        "toolCallId": "call-1",
        "messageId": "msg-1",
        "content": [
            {"type": "text", "text": "Export"},
            {"type": "document",
             "source": {"type": "url",
                        "value": "/threads/thread-123/artifacts/" + artifact_id,
                        "mimeType": "text/csv"},
             "metadata": {"filename": "export.csv", "artifactId": artifact_id}},
        ],
    }
    runtime._receive_frame(connection, run, [json.dumps(event)])
    assert connection.display_messages == [
        {"role": "download", "id": artifact_id,
         "filename": "export.csv", "mimeType": "text/csv"}
    ]

    event["content"][1]["source"]["value"] = "/threads/other/artifacts/" + artifact_id
    runtime._receive_frame(connection, run, [json.dumps(event)])
    assert len(connection.display_messages) == 1


def test_flask_proxy_requires_connection_and_sends_attachment():
    from signaldeck_ai.chat_processor import AGUIChatProcessor
    from signaldeck_ai.plugin import bp

    class Provider:
        def registerProcessor(self, processor, export):
            pass

    processor = AGUIChatProcessor(
        "chat", {"endpoint": "http://127.0.0.1:8765/ag-ui"},
        SimpleNamespace(), Provider(), False,
    )
    processor.runtime.download_artifact = lambda connection, artifact: (
        b"a,b\n1,2\n", "export.csv", "text/csv"
    )

    app = Flask(__name__)
    app.extensions["signaldeck.ai.processors"] = {"chat": processor}
    app.register_blueprint(bp)
    client = app.test_client()
    assert client.get("/plugin/ai/api/chat/chat/download/" + "a" * 32).status_code == 401
    response = client.get(
        "/plugin/ai/api/chat/chat/download/" + "a" * 32,
        headers={"X-SignalDeck-AI-Connection": "connection"},
    )
    assert response.status_code == 200
    assert response.data == b"a,b\n1,2\n"
    assert "attachment" in response.headers["Content-Disposition"]
    assert response.headers["Cache-Control"] == "no-store"
