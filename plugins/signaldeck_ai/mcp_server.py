"""One explicit, configuration-bounded MCP endpoint per SignalDeck processor."""
from __future__ import annotations

import json
import threading
from typing import Any

import uvicorn
from jsonschema import Draft202012Validator
from mcp.server.fastmcp import FastMCP

from .serialization import bounded_sample, json_safe, series_points, summarize_points


class MCPRuntime:
    def __init__(self, processor: Any):
        self.processor = processor
        cfg = processor.config.get("mcp", {})
        self.host = str(cfg.get("host", "127.0.0.1"))
        self.port = int(cfg.get("port", 8766))
        if self.host not in {"127.0.0.1", "::1"}:
            raise ValueError(
                "MCP binding is restricted to loopback. Use a protected tunnel "
                "for remote access rather than an unauthenticated public MCP."
            )
        if self.port < 1024 or self.port > 65535:
            raise ValueError("MCP port must be between 1024 and 65535")

        self.server = None
        self.thread: threading.Thread | None = None
        self.error: str | None = None
        self.mcp = FastMCP(
            cfg.get("name", processor.name),
            host=self.host,
            port=self.port,
            stateless_http=True,
            json_response=True,
        )
        self._register_tools()

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}/mcp"

    @property
    def status(self) -> str:
        if self.error:
            return "error"
        if self.server is not None and self.server.started:
            return "running"
        if self.thread is not None and not self.thread.is_alive():
            return "stopped"
        return "starting" if self.thread is not None else "pending"

    def _register_tools(self) -> None:
        processor = self.processor
        mcp = self.mcp
        fields = processor.value_specs
        history = processor.history_specs
        actions = processor.action_specs

        descriptions = "\n".join(
            f"- {f['name']}: {f['description']}" for f in fields.values()
        ) or "(no values configured)"

        @mcp.tool(name="get_values", description="Get permitted current SignalDeck values.\n" + descriptions)
        def get_values() -> dict[str, Any]:
            return processor.read_values()

        if history:
            description = "Fetch permitted SignalDeck history and compute exact statistics.\n"
            description += "\n".join(
                f"- {item['name']}: {item['description']} (up to {item.get('max_days', 30)} days)"
                for item in history.values()
            )

            @mcp.tool(name="get_history", description=description)
            def get_history(
                name: str,
                days: int = 1,
                include_points: bool = False,
                max_points: int = 200,
            ) -> dict[str, Any]:
                return processor.read_history(
                    name, days=days, include_points=include_points, max_points=max_points
                )

        if actions:
            description = "Execute an explicitly configured SignalDeck action.\n"
            description += "\n".join(
                f"- {item['name']}: {item['description']}" for item in actions.values()
            )

            @mcp.tool(name="execute_action", description=description)
            def execute_action(name: str, arguments: dict[str, Any]) -> Any:
                return processor.execute_action(name, arguments)

    def start(self) -> None:
        if self.thread is not None:
            return
        application = self.mcp.streamable_http_app()
        config = uvicorn.Config(
            application, host=self.host, port=self.port, log_level="warning",
            access_log=False, lifespan="on",
        )
        self.server = uvicorn.Server(config)

        def serve() -> None:
            try:
                self.server.run()
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
                processor = self.processor
                processor.logger.exception("MCP server %s failed", processor.name)

        self.thread = threading.Thread(
            target=serve, daemon=True, name=f"signaldeck-mcp-{self.processor.name}"
        )
        self.thread.start()

    def stop(self) -> None:
        if self.server is not None:
            self.server.should_exit = True
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=5)
