#!/usr/bin/env python3
"""Send a genuine Socket.IO client chat event to the local @haus AI demo.

Prerequisites:
  pip install "python-socketio[client]>=5.12,<6"
  python scripts/test_socketio_ai.py "@haus Wie warm ist es im Garten?"
The SignalDeck AI Chat view must first be connected to cli-agent-api.
"""
from __future__ import annotations

import argparse
import sys
import threading
import uuid

import socketio


def main() -> int:
    parser = argparse.ArgumentParser(description="Exercise the Socket.IO → AI → Socket.IO demo")
    parser.add_argument("message", nargs="?", default="@haus Wie warm ist es im Garten?")
    parser.add_argument("--url", default="http://127.0.0.1:5000")
    parser.add_argument("--room", default="haus")
    parser.add_argument("--timeout", type=float, default=130.0)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be > 0")
    client = socketio.Client(reconnection=False, logger=False)
    done = threading.Event()

    @client.on("event")
    def received(event):
        if not isinstance(event, dict) or event.get("room") != args.room:
            return
        if event.get("type") != "chat":
            return
        payload = event.get("payload")
        if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
            return
        # Incoming events from other clients may arrive while we are waiting.
        # Only a server-originated event constitutes the AI bridge's reply.
        if event.get("transmitter") != "signaldeck":
            return
        print("Haus-Assistent:", payload["text"], flush=True)
        done.set()

    try:
        client.connect(
            args.url,
            auth={
                "id": "ai-demo-" + uuid.uuid4().hex,
                "username": "AI Demo Testclient",
            },
            transports=["polling"],
            wait_timeout=10,
        )
        joined = client.call("join_room", {"room": args.room}, timeout=10)
        if not isinstance(joined, dict) or joined.get("ok") is not True:
            raise RuntimeError(f"Could not join room {args.room!r}: {joined!r}")
        event_id = uuid.uuid4().hex
        event = {
            "schemaVersion": 1,
            "id": event_id,
            "room": args.room,
            "type": "chat",
            "operation": "upsert",
            "payload": {
                "text": args.message,
                "contentType": "text",
                "system": False,
            },
        }
        response = client.call("send_event", event, timeout=10)
        if not isinstance(response, dict) or response.get("ok") is not True:
            raise RuntimeError(f"Socket.IO server rejected the test event: {response!r}")
        print(f"Gesendet an {args.room}: {args.message}", flush=True)
        if not done.wait(args.timeout):
            print("Keine Antwort empfangen (Timeout).", file=sys.stderr)
            return 1
        return 0
    except (OSError, RuntimeError, socketio.exceptions.SocketIOError) as exc:
        print(f"Socket.IO-Test fehlgeschlagen: {exc}", file=sys.stderr)
        return 1
    finally:
        if client.connected:
            client.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())
