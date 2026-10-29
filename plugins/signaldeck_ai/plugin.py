"""Register SignalDeck AI assets and a read-only AG-UI event stream."""
from __future__ import annotations

import json

from flask import Blueprint, Response, abort, request, stream_with_context

from .chat_processor import AGUIChatProcessor
from .chat_runtime import ChatActionError
from .mcp_processor import MCPServerProcessor
from .mcp_server import MCPRuntime

bp = Blueprint(
    "signaldeck_ai", __name__, template_folder="templates",
    static_folder="static", static_url_path="/static", url_prefix="/plugin/ai",
)


@bp.get("/api/chat/<name>/events/<run_id>")
def stream_events(name: str, run_id: str):
    """SSE only: all client commands arrive via SignalDeck's /run endpoint.

    A connection handle is required in an HTTP header instead of the query
    string so it cannot be recorded in URL access logs.
    """
    from flask import current_app
    processor = current_app.extensions["signaldeck.ai.processors"].get(name)
    if not isinstance(processor, AGUIChatProcessor):
        abort(404)
    identifier = request.headers.get("X-SignalDeck-AI-Connection", "")
    if not identifier:
        abort(401)
    try:
        run = processor.runtime.get_run(identifier, run_id)
    except ChatActionError:
        abort(404)

    try:
        last_id = int(request.headers.get("Last-Event-ID", "0"))
        if last_id < 0:
            raise ValueError
    except ValueError:
        abort(400)

    def event_stream():
        cursor = last_id
        while True:
            # Check connection validity for every subscriber wake-up.
            if not processor.runtime.contains(identifier):
                return
            with run.changed:
                if run.events and cursor < run.events[0][0] - 1:
                    yield 'data: {"type":"RUN_ERROR","message":"Event history expired."}\n\n'
                    return
                events = [(seq, event) for seq, event in run.events if seq > cursor]
                done = run.done
                last_sequence = run.next_sequence - 1
                if not events and not done:
                    run.changed.wait(timeout=3)
                    continue
            for seq, event in events:
                cursor = seq
                yield f"id: {seq}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            if done and cursor >= last_sequence:
                return
            if not events:
                yield ": keep-alive\n\n"

    return Response(
        stream_with_context(event_stream()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@bp.get("/api/chat/<name>/download/<artifact_id>")
def download_artifact(name: str, artifact_id: str):
    """Same-origin, authenticated proxy to cli-agent's localhost file endpoint."""
    from flask import current_app, send_file
    from io import BytesIO
    processor = current_app.extensions["signaldeck.ai.processors"].get(name)
    if not isinstance(processor, AGUIChatProcessor):
        abort(404)
    connection_id = request.headers.get("X-SignalDeck-AI-Connection", "")
    if not connection_id:
        abort(401)
    try:
        payload, filename, mime_type = processor.runtime.download_artifact(
            connection_id, artifact_id,
        )
    except ChatActionError:
        abort(404)
    response = send_file(
        BytesIO(payload), mimetype=mime_type,
        as_attachment=True, download_name=filename,
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def register(app, ctx=None):
    if "signaldeck.ai.processors" in app.extensions:
        return
    if ctx is None:
        raise RuntimeError("signaldeck-plugin-ai requires ApplicationContext")
    processors = dict(ctx.values.processors)
    app.extensions["signaldeck.ai.processors"] = processors
    app.register_blueprint(bp)
    for processor in processors.values():
        if isinstance(processor, MCPServerProcessor):
            processor.runtime = MCPRuntime(processor)
            processor.runtime.start()
