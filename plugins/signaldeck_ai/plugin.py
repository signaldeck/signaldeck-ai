"""Register MCP backends and AG-UI proxy in the existing SignalDeck Flask app."""
from __future__ import annotations

import json
from urllib.parse import quote

import httpx
from flask import Blueprint, Response, abort, jsonify, request, stream_with_context

from .chat_processor import AGUIChatProcessor
from .mcp_processor import MCPServerProcessor
from .mcp_server import MCPRuntime


bp = Blueprint(
    "signaldeck_ai", __name__, template_folder="templates",
    static_folder="static", static_url_path="/static", url_prefix="/plugin/ai",
)

MAX_BODY = 1_048_576
RUN_TIMEOUT = httpx.Timeout(connect=8.0, read=None, write=20.0, pool=8.0)


def _processors(app):
    return app.extensions["signaldeck.ai.processors"]


def _chat(name):
    # Active Flask app lookup, separate from the importing module's lifetime.
    from flask import current_app
    processor = _processors(current_app).get(name)
    if not isinstance(processor, AGUIChatProcessor):
        abort(404)
    return processor


def _same_origin():
    """CSRF boundary for the browser-to-SignalDeck request (not user auth)."""
    origin = request.headers.get("Origin")
    if origin is None:
        return False
    expected = f"{request.scheme}://{request.host}"
    return origin == expected


def _auth(connection):
    return {
        "Authorization": f"Bearer {connection.token}",
        "Accept": "text/event-stream",
    }


def _connection(processor):
    connection_id = request.headers.get("X-SignalDeck-AI-Connection", "")
    if not connection_id:
        abort(401)
    connection = processor.connection(connection_id)
    if connection is None:
        abort(401)
    return connection


@bp.post("/api/chat/<name>/connect")
def connect(name):
    if not _same_origin():
        return jsonify(error="Same-origin request required"), 403
    processor = _chat(name)
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or set(data) != {"token"}:
        return jsonify(error="Token required"), 400
    token = data["token"]
    if not isinstance(token, str) or not 1 <= len(token) <= 4096:
        return jsonify(error="Invalid token"), 400
    headers = {"Authorization": f"Bearer {token}"}
    try:
        with httpx.Client(timeout=8.0) as client:
            response = client.get(
                processor.endpoint.removesuffix("/ag-ui") + "/ag-ui/capabilities",
                headers=headers,
            )
        if response.status_code in {401, 403}:
            return jsonify(error="Token rejected by agent"), 401
        response.raise_for_status()
        response.json()
        connection_id = processor.connect(token)
        return jsonify(connection_id=connection_id), 200
    except (httpx.HTTPError, ValueError, RuntimeError):
        return jsonify(error="Agent unavailable or connection limit reached"), 502


@bp.post("/api/chat/<name>/disconnect")
def disconnect(name):
    if not _same_origin():
        return jsonify(error="Same-origin request required"), 403
    processor = _chat(name)
    connection_id = request.headers.get("X-SignalDeck-AI-Connection", "")
    connection = processor.disconnect(connection_id)
    if connection is None:
        return jsonify(error="Connection not found"), 401
    # Do not allow a stale session to keep its backend threads indefinitely.
    for thread_id in tuple(connection.threads):
        try:
            with httpx.Client(timeout=5.0) as client:
                client.delete(
                    processor.endpoint.removesuffix("/ag-ui") + "/threads/" +
                    quote(thread_id, safe=""),
                    headers=_auth(connection),
                )
        except httpx.HTTPError:
            pass
    return ("", 204)


@bp.get("/api/chat/<name>/capabilities")
def capabilities(name):
    processor = _chat(name)
    connection = _connection(processor)
    try:
        with httpx.Client(timeout=8.0) as client:
            response = client.get(
                processor.endpoint.removesuffix("/ag-ui") + "/ag-ui/capabilities",
                headers=_auth(connection),
            )
            response.raise_for_status()
            return jsonify(response.json())
    except (httpx.HTTPError, ValueError):
        return jsonify(error="Agent unavailable"), 502


@bp.post("/api/chat/<name>/run")
def run(name):
    if not _same_origin():
        return jsonify(error="Same-origin request required"), 403
    processor = _chat(name)
    if request.content_length is None or request.content_length > MAX_BODY:
        return jsonify(error="Content-Length required and must be <=1 MiB"), 413
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="JSON object required"), 400
    if not isinstance(data.get("threadId"), str) or not isinstance(data.get("runId"), str):
        return jsonify(error="AG-UI threadId/runId required"), 400
    if not isinstance(data.get("messages"), list):
        return jsonify(error="AG-UI messages required"), 400
    connection_id = request.headers.get("X-SignalDeck-AI-Connection", "")
    connection = processor.claim_thread(connection_id, data["threadId"])
    if connection is None:
        return jsonify(error="Not connected or thread belongs to another connection"), 403
    headers = _auth(connection)
    headers["Content-Type"] = "application/json"

    def events():
        try:
            with httpx.Client(timeout=RUN_TIMEOUT) as client:
                with client.stream("POST", processor.endpoint, headers=headers, json=data) as response:
                    if response.status_code != 200:
                        yield "data: " + json.dumps({
                            "type": "RUN_ERROR",
                            "message": f"Agent returned HTTP {response.status_code}",
                        }) + "\n\n"
                        return
                    for chunk in response.iter_bytes():
                        if chunk:
                            yield chunk
        except httpx.HTTPError as exc:
            yield "data: " + json.dumps({
                "type": "RUN_ERROR", "message": f"Agent transport error: {type(exc).__name__}"
            }) + "\n\n"

    return Response(
        stream_with_context(events()),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@bp.delete("/api/chat/<name>/thread/<thread_id>")
def delete_thread(name, thread_id):
    if not _same_origin():
        return jsonify(error="Same-origin request required"), 403
    processor = _chat(name)
    if not thread_id or len(thread_id) > 128:
        return jsonify(error="Invalid thread ID"), 400
    connection_id = request.headers.get("X-SignalDeck-AI-Connection", "")
    if not processor.owns_thread(connection_id, thread_id):
        return jsonify(error="Thread not owned by connection"), 403
    connection = processor.connection(connection_id)
    headers = _auth(connection)
    try:
        base = processor.endpoint.rsplit("/ag-ui", 1)[0]
        with httpx.Client(timeout=8.0) as client:
            response = client.delete(
                base + "/threads/" + quote(thread_id, safe=""), headers=headers
            )
        if response.status_code not in {204, 404}:
            return jsonify(error="Cannot close thread"), 502
        processor.release_thread(connection_id, thread_id)
        return ("", 204)
    except httpx.HTTPError:
        return jsonify(error="Cannot close thread"), 502


def register(app, ctx=None):
    if "signaldeck.ai.processors" in app.extensions:
        return
    if ctx is None:
        raise RuntimeError("signaldeck-ai requires ApplicationContext")
    processors = dict(ctx.values.processors)
    app.extensions["signaldeck.ai.processors"] = processors
    app.register_blueprint(bp)
    # SignalDeck constructs every processor before registering plugin hooks.
    # At this point references from later processors are available.
    for processor in processors.values():
        if isinstance(processor, MCPServerProcessor):
            processor.runtime = MCPRuntime(processor)
            processor.runtime.start()
