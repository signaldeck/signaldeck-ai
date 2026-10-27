"""One explicit, configuration-bounded MCP endpoint per SignalDeck processor."""
from __future__ import annotations

import asyncio
import json
import threading
from typing import Annotated, Any

import uvicorn
from jsonschema import Draft202012Validator
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent
from pydantic import Field

from .serialization import bounded_sample, json_safe, series_points, summarize_points


_ONLY_METADATA_TO_LLM = "io.github.shuewe.cli-agent/only_metadata_to_llm"
_METADATA_FOR_LLM = "io.github.shuewe.cli-agent/meta_data_for_llm"
_MAX_HISTORY_RESULT_BYTES = 8_000_000
_INLINE_HISTORY_BYTES = 8_000


def _history_tool_result(payload: dict[str, Any]) -> CallToolResult:
    """Let cli-agent persist complete history while exposing only useful metadata.

    No path is chosen here. The agent owns artifact creation and cleanup when
    OS read access is available; ordinary MCP clients receive the full data.
    """
    if "points" not in payload:
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
            structuredContent=payload,
        )

    serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    size = len(serialized.encode("utf-8"))
    if size > _MAX_HISTORY_RESULT_BYTES:
        raise ValueError(
            "Complete history exceeds the MCP result size budget; "
            "narrow the dates or the daily clock-time window."
        )

    points = payload["points"]
    columns = ["time", "value"]
    if any("time_inferred" in point for point in points):
        columns.append("time_inferred")
    metadata = {key: value for key, value in payload.items() if key != "points"}
    metadata.update({
        "row_count": len(points),
        "columns": columns,
        "records_path": "/points",
        "time_column": "time",
        "value_column": "value",
        "format": "json",
    })
    if size <= _INLINE_HISTORY_BYTES:
        metadata["points"] = points

    return CallToolResult(
        content=[TextContent(
            type="text",
            text="Complete history in structuredContent; use its /points array for analysis.",
        )],
        structuredContent=payload,
        **{"_meta": {
            _ONLY_METADATA_TO_LLM: True,
            _METADATA_FOR_LLM: metadata,
        }},
    )


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

        self.instructions = cfg.get("instructions", "")
        if not isinstance(self.instructions, str):
            raise ValueError("mcp.instructions must be a string")

        self.server = None
        self.thread: threading.Thread | None = None
        self.error: str | None = None
        self.mcp = FastMCP(
            cfg.get("name", processor.name),
            host=self.host,
            port=self.port,
            stateless_http=True,
            json_response=True,
            instructions=self.instructions,
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
        runs = processor.run_actions.descriptions()

        descriptions = "\n".join(
            f"- {f['name']}: {f['description']}" for f in fields.values()
        ) or "(no values configured)"

        if fields:
            @mcp.tool(
                name="get_values",
                description=(
                    "Read selected SignalDeck values by their configured names. "
                    "Omit fields to read all published values. "
                    "Never use source ValueProvider names.\n" + descriptions
                ),
            )
            def get_values(fields: list[str] | None = None) -> dict[str, Any]:
                return processor.read_values(fields)

        if history:
            description = (
                "Read historical measurements for an explicit calendar date "
                "(YYYY-MM-DD). date is REQUIRED and is the inclusive START date. "
                "days=1 reads that calendar day; days=N reads that day and the "
                "following N-1 days, subject to the configured max_days. "
                "Optional start_time and end_time (HH:MM or HH:MM:SS) select "
                "the SAME daily clock-time window on EACH selected day. "
                "The start is inclusive and end exclusive; omit either bound "
                "to use the corresponding day boundary. Overnight windows "
                "(start_time >= end_time) are not supported. "
                "For focused investigations, narrow the daily time window "
                "to reduce the amount of data to process. "
                "This is NOT the offset argument of SignalDeck PersistData.hist: "
                "the adapter selects each day with hist(date=..., days=0, "
                "fullDay=True). If the requested window includes today, "
                "timestamped samples from hist(currentValues=True) are "
                "merged with persisted measurements (cache wins on duplicate "
                "timestamps); earlier days use persisted data only. "
                "By DEFAULT (include_points=false), the tool "
                "returns AGGREGATE STATISTICS over ALL measurements in the "
                "selected period: count, numeric_count, minimum, maximum, "
                "mean, first_time and last_time. With include_points=true "
                "it ADDITIONALLY returns ALL timestamp/value measurements "
                "without sampling or truncation. When cli-agent supports "
                "session files and OS read, the complete result is kept in "
                "a workspace file; metadata includes its workspace_path "
                "and records_path=/points. Use Data MCP on that file "
                "for detailed comparisons or calculations.\\n"
            )
            description += "\\n".join(
                f"- {item['name']}: {item['description']} (up to {item.get('max_days', 30)} days)"
                for item in history.values()
            )

            @mcp.tool(name="get_history", description=description, structured_output=False)
            def get_history(
                name: Annotated[str, Field(
                    description="Local name of an allowed data_rows history method."
                )],
                date: Annotated[str, Field(
                    description=(
                        "Required calendar date YYYY-MM-DD, inclusive START "
                        "date of the selected history window. For example "
                        "2026-10-20 with days=2 selects October 20 and 21."
                    ),
                    pattern=r"^\d{4}-\d{2}-\d{2}$",
                )],
                days: Annotated[int, Field(
                    description=(
                        "Number of calendar days starting on the selected "
                        "date, including the date itself (default 1). "
                        "NOT a relative offset from today."
                    ),
                    ge=1,
                )] = 1,
                start_time: Annotated[str | None, Field(
                    description=(
                        "Optional inclusive local clock-time lower bound, "
                        "HH:MM or HH:MM:SS. Applied separately on EACH "
                        "selected calendar day, not just the first. "
                        "Omit to start at midnight. Must be earlier than end_time."
                    ),
                    pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?$",
                )] = None,
                end_time: Annotated[str | None, Field(
                    description=(
                        "Optional exclusive local clock-time upper bound, "
                        "HH:MM or HH:MM:SS. Applied separately on EACH "
                        "selected calendar day. Omit to include the full "
                        "remainder of the day. Overnight windows are invalid."
                    ),
                    pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?$",
                )] = None,
                include_points: Annotated[bool, Field(
                    description=(
                        "Default false: return only aggregate statistics "
                        "(count, numeric_count, min/max/mean and first/last "
                        "timestamps), calculated over ALL measurements in "
                        "the selected days. No individual points are returned. "
                        "True: include every timestamp/value measurement, "
                        "with no sampling. cli-agent can store the full "
                        "result as a file for Data MCP operations."
                    ),
                )] = False,
            ) -> CallToolResult:
                history_result = processor.read_history(
                    name, date=date, days=days,
                    start_time=start_time, end_time=end_time,
                    include_points=include_points, max_points=None,
                )
                return _history_tool_result(history_result)


            @mcp.tool(
                name="get_historical_value",
                description=(
                    "Retrieve ONE historical value for a precise, REQUIRED local "
                    "date and time (YYYY-MM-DDTHH:MM:SS). An ISO date without "
                    "a time is invalid. Calls SignalDeck hist(date=..., "
                    "days=0, fullDay=False), selecting the datastore's best "
                    "matching value; this is not necessarily a measurement "
                    "recorded at exactly the requested second. For whole-day "
                    "measurements and statistics, use get_history instead. "
                    "Only explicitly configured per-day history names are "
                    "permitted. Available history names:\n"
                ) + "\n".join(
                    f"- {item['name']}: {item['description']}"
                    for item in history.values()
                    if item.get("mode", "per_day") == "per_day"
                ),
            )
            def get_historical_value(
                name: Annotated[str, Field(
                    description="Local name of an explicitly allowed per-day history method."
                )],
                date: Annotated[str, Field(
                    description=(
                        "REQUIRED exact local date AND time formatted "
                        "YYYY-MM-DDTHH:MM:SS; date-only values are rejected. "
                        "Example: 2026-10-20T14:30:00."
                    ),
                    pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$",
                )],
            ) -> dict[str, Any]:
                return processor.read_historical_value(name, date=date)

        if actions:
            description = "Execute an explicitly configured SignalDeck action.\n"
            description += "\n".join(
                f"- {item['name']}: {item['description']}" for item in actions.values()
            )

            @mcp.tool(name="execute_action", description=description)
            def execute_action(name: str, arguments: dict[str, Any]) -> Any:
                return processor.execute_action(name, arguments)

        if runs:
            description = (
                "Start one explicitly permitted, named SignalDeck action. "
                "The action may execute a configured Cmd command or start a "
                "SignalDeck script. Only the public action name and tightly "
                "restricted arguments are accepted; raw Cmd commands and "
                "script source are NEVER accepted. The response confirms "
                "scheduling, not successful completion of an asynchronous "
                "command or script. Actions available:\\n"
            )
            description += "\\n".join(
                "- " + item["name"] + ": " + item["description"]
                + " (arguments schema: " + json.dumps(item["parameters"], ensure_ascii=False) + ")"
                for item in runs
            )
            @mcp.tool(name="run", description=description)
            def run(
                name: str,
                arguments: dict[str, Any] | None = None,
            ) -> dict[str, str]:
                """Start a permitted named action; returns status=scheduled or raises.

                name is the configured public alias, NOT a Cmd command or script.
                arguments must match the alias-specific JSON schema, if any.
                """
                return processor.run_named_action(name, arguments)

    def tool_catalog(self) -> list[dict[str, Any]]:
        """Expose the actual FastMCP registry, including its generated schemas."""
        return [
            {
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": tool.parameters,
                "output_schema": tool.output_schema,
                "is_action": tool.name in {"execute_action", "run"},
            }
            for tool in self.mcp._tool_manager.list_tools()
        ]

    def call_registered_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Use the very same FastMCP handlers as an external MCP client.

        Called from the synchronous Flask /run action handler. A separate event
        loop is safe here; no MCP HTTP listener or cli-agent is required.
        """
        if not isinstance(name, str) or not isinstance(arguments, dict):
            raise ValueError("Tool name and JSON object arguments are required")
        catalog = {item["name"]: item for item in self.tool_catalog()}
        if name not in catalog:
            raise ValueError("Unknown or unavailable MCP tool")
        Draft202012Validator(catalog[name]["input_schema"]).validate(arguments)
        result = asyncio.run(self.mcp.call_tool(name, arguments))
        # FastMCP returns either a dict, content blocks, or (blocks, structured
        # output). Nested MCP content blocks are Pydantic objects, so convert
        # the complete result recursively, not just top-level items.
        def convert(value):
            if hasattr(value, "model_dump"):
                return value.model_dump(mode="json", exclude_none=True)
            if isinstance(value, (tuple, list)):
                return [convert(item) for item in value]
            if isinstance(value, dict):
                return {key: convert(item) for key, item in value.items()}
            return value

        converted = convert(result)
        if isinstance(converted, dict) and isinstance(converted.get("structuredContent"), dict):
            # Opt-in tools return a CallToolResult directly, which also
            # contains _meta and a short descriptive text content block.
            return converted["structuredContent"]
        # FastMCP's in-process call returns (text content blocks, structured
        # output). The diagnostic UI should show the structured output once,
        # not both serialized and structured copies of the same measurements.
        if (isinstance(converted, list) and len(converted) == 2
                and isinstance(converted[0], list)
                and isinstance(converted[1], dict)):
            return converted[1]
        return converted

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
