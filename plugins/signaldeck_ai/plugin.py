"""Register MCP backends and AG-UI proxy in the existing SignalDeck Flask app."""
from __future__ import annotations

import json
import os
from urllib.parse import urlsplit, quote

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


def _auth(processor):
    token = os.environ.get(processor.api_token_env, "")
    if not token:
        raise RuntimeError(f"Missing API token environment variable: {processor.api_token_env}")
    return {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}


@bp.get("/api/chat/<name>/capabilities")
def capabilities(name):
    processor = _chat(name)
    try:
        headers = _auth(processor)
        with httpx.Client(timeout=httpx.Timeout(8.0)) as client:
            response = client.get(processor.endpoint.rsplit("/ag-ui", 1)[0] + "/ag-ui/capabilities", headers=headers)
            response.raise_for_status()
            result = response.json()
        return jsonify(result)
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        return jsonify(error=f"Cannot connect to AG-UI agent: {type(exc).__name__}"), 502


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
    try:
        headers = _auth(processor)
    except RuntimeError:
        return jsonify(error="AG-UI backend is not configured"), 503
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
    try:
        headers = _auth(processor)
        base = processor.endpoint.rsplit("/ag-ui", 1)[0]
        with httpx.Client(timeout=8.0) as client:
            response = client.delete(
                base + "/threads/" + quote(thread_id, safe=""), headers=headers
            )
        if response.status_code not in {204, 404}:
            return jsonify(error="Cannot close thread"), 502
        return ("", 204)
    except (httpx.HTTPError, RuntimeError):
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
