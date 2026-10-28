"""Diagnostic MCP console uses the exact FastMCP registry and SignalDeck actions."""
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask
from signaldeck_sdk import ValueProvider
from signaldeck_plugin_main.processors.dummy_data.data import Data

from signaldeck_ai.mcp_processor import MCPServerProcessor
from signaldeck_ai.mcp_server import MCPRuntime


@pytest.fixture()
def console_processor():
    raw = json.loads(
        (Path(__file__).resolve().parents[1] / "config" / "ai.json").read_text()
    )
    configs = {p["name"]: p["config"] for p in raw["processors"]}
    provider = ValueProvider()
    dummy = Data("dummy_temperature", configs["dummy_temperature"],
                 SimpleNamespace(), provider, False)
    # DummyData deliberately emits timezone-naive synthetic wall times. For
    # MCP protocol tests, adapt only this fixture to a timezone-aware UTC
    # source, as real persisted SignalDeck measurements already are.
    original_get_method = provider.getMethodValue

    def aware_demo_method(name, *args, **kwargs):
        if name.startswith("hist_temperature_"):
            date = kwargs.get("date")
            if date is not None:
                kwargs = {**kwargs, "date": date.replace(tzinfo=None)}
            result = original_get_method(name, *args, **kwargs)
            if hasattr(result, "index") and hasattr(result.index, "tz_localize"):
                result.index = result.index.tz_localize("UTC")
            return result
        return original_get_method(name, *args, **kwargs)

    provider.getMethodValue = aware_demo_method
    processor = MCPServerProcessor("mcp_climate", configs["mcp_climate"],
                                    SimpleNamespace(), provider, False)
    processor.runtime = MCPRuntime(processor)
    return processor, dummy


def invoke(processor, **params):
    app = Flask(__name__)
    with app.test_request_context(
        "/run", method="POST", headers={"Origin": "http://localhost"}
    ):
        return processor.process(["overview"], "example-hash", mcp_action="execute_tool", **params)


def test_catalog_is_actual_registered_mcp_tool_schema(console_processor):
    processor, _ = console_processor
    tools = {t["name"]: t for t in processor.registered_tool_catalog()}
    assert set(tools) == {"get_values", "get_history", "get_historical_value", "execute_action", "run"}
    assert "fields" in tools["get_values"]["input_schema"]["properties"]
    assert "name" in tools["get_history"]["input_schema"]["properties"]
    history_description = tools["get_history"]["description"]
    assert "AGGREGATE STATISTICS" in history_description
    assert "ALL measurements" in history_description
    assert "absolute ISO 8601" in history_description
    assert set(tools["get_history"]["input_schema"]["required"]) == {"name", "start", "end"}
    assert set(tools["get_historical_value"]["input_schema"]["required"]) == {"name", "date"}
    props = tools["get_history"]["input_schema"]["properties"]
    assert "aggregate statistics" in props["include_points"]["description"]
    assert "No individual points" in props["include_points"]["description"]
    assert "max_points" not in props
    assert "every timestamp/value measurement" in props["include_points"]["description"]
    assert "start" in props and "end" in props
    assert not {"date", "days", "start_time", "end_time"} & props.keys()
    assert "UTC offset" in props["start"]["description"] or "offset" in props["start"]["description"]

    assert tools["execute_action"]["is_action"] is True
    assert "Aktuelle" in tools["get_values"]["description"] or "simulierte" in tools["get_values"]["description"]


def test_read_tool_executes_with_fastmcp_and_shows_result(console_processor):
    processor, _ = console_processor
    result = invoke(
        processor, tool_name="get_values",
        arguments=json.dumps({"fields": ["temp_garten"]})
    )
    assert result["ok"], result
    assert result["tool"] == "get_values"
    rendered = json.dumps(result["result"], ensure_ascii=False)
    assert "temp_garten" in rendered
    assert "18.5" in rendered


def test_history_tool_executes_with_fastmcp(console_processor):
    processor, _ = console_processor
    result = invoke(
        processor, tool_name="get_history",
        arguments=json.dumps({
            "name": "hist_temp_garten", "start": "2026-10-24T00:00:00Z",
            "end": "2026-10-26T00:00:00Z", "include_points": True
        }),
    )
    assert result["ok"], result
    assert "hist_temp_garten" in json.dumps(result["result"])


def test_history_tool_accepts_absolute_window_through_fastmcp(console_processor):
    processor, _ = console_processor
    result = invoke(
        processor, tool_name="get_history",
        arguments=json.dumps({
            "name": "hist_temp_garten", "start": "2026-10-24T07:00:00Z",
            "end": "2026-10-25T08:00:00Z", "include_points": True,
        }),
    )
    assert result["ok"], result
    payload = result["result"]
    assert payload["start"] == "2026-10-24T07:00:00Z"
    assert payload["end"] == "2026-10-25T08:00:00Z"
    start = datetime.fromisoformat(payload["start"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(payload["end"].replace("Z", "+00:00"))
    assert all(start <= datetime.fromisoformat(point["time"].replace("Z", "+00:00")) < end
               for point in payload["points"])


def test_history_mcp_metadata_handoff_and_statistics_only(console_processor):
    import asyncio

    processor, _ = console_processor
    args = {"name": "hist_temp_garten", "start": "2026-10-24T00:00:00Z",
            "end": "2026-10-25T00:00:00Z", "include_points": True}
    result = asyncio.run(processor.runtime.mcp.call_tool("get_history", args))
    assert result.structuredContent["name"] == args["name"]
    assert result.structuredContent["sampled"] is False
    assert result.meta["io.github.shuewe.cli-agent/only_metadata_to_llm"] is True
    metadata = result.meta["io.github.shuewe.cli-agent/meta_data_for_llm"]
    assert metadata["records_path"] == "/points"
    assert metadata["columns"][:2] == ["time", "value"]
    assert metadata["time_column"] == "time"
    assert metadata["value_column"] == "value"
    assert metadata["row_count"] == len(result.structuredContent["points"])
    assert metadata["points"] == result.structuredContent["points"]
    assert "workspace_path" not in metadata  # cli-agent adds the session path

    only_stats = asyncio.run(processor.runtime.mcp.call_tool(
        "get_history", {**args, "include_points": False},
    ))
    assert "points" not in only_stats.structuredContent
    assert not only_stats.meta or not only_stats.meta.get(
        "io.github.shuewe.cli-agent/only_metadata_to_llm",
    )


def test_history_mcp_returns_all_records_with_compact_metadata(monkeypatch, console_processor):
    import asyncio
    from datetime import datetime, timedelta, timezone

    processor, _ = console_processor
    start = datetime(2026, 10, 24, 0, 0, tzinfo=timezone.utc)
    measurements = {
        (start + timedelta(minutes=index)).isoformat(): float(index)
        for index in range(1205)
    }

    def history(name, *args, **kwargs):
        if kwargs.get("currentValues"):
            return None
        return measurements

    monkeypatch.setattr(processor.valueProvider, "getMethodValue", history)
    result = asyncio.run(processor.runtime.mcp.call_tool("get_history", {
        "name": "hist_temp_garten", "start": "2026-10-24T00:00:00Z",
        "end": "2026-10-25T00:00:00Z", "include_points": True,
    }))
    assert result.structuredContent["total_points"] == 1205
    assert result.structuredContent["sampled"] is False
    assert len(result.structuredContent["points"]) == 1205
    meta = result.meta["io.github.shuewe.cli-agent/meta_data_for_llm"]
    assert meta["row_count"] == 1205
    assert "points" not in meta
    assert meta["statistics"]["mean"] == 602.0


def test_history_diagnostic_accepts_more_than_10000_complete_points(monkeypatch, console_processor):
    from datetime import datetime, timedelta, timezone

    processor, _ = console_processor
    start = datetime(2026, 10, 24, tzinfo=timezone.utc)
    measurements = {
        (start + timedelta(seconds=index)).isoformat(): float(index)
        for index in range(10001)
    }

    def history(name, *args, **kwargs):
        return measurements

    monkeypatch.setattr(processor.valueProvider, "getMethodValue", history)
    result = invoke(
        processor, tool_name="get_history",
        arguments=json.dumps({
            "name": "hist_temp_garten", "start": "2026-10-24T00:00:00Z",
            "end": "2026-10-25T00:00:00Z", "include_points": True,
        }),
    )
    assert result["ok"], result.get("error")
    payload = result["result"]
    assert payload["statistics"]["count"] == 10001
    assert payload["total_points"] == 10001
    assert payload["sampled"] is False
    assert len(payload["points"]) == 10001
    assert payload["points"][-1]["value"] == 10000.0


def test_mutating_tool_requires_explicit_confirmation(console_processor):
    processor, dummy = console_processor
    params = {
        "tool_name": "execute_action",
        "arguments": json.dumps({
            "name": "set_temp_garten", "arguments": {"temp_garten": 24.5}
        })
    }
    declined = invoke(processor, **params)
    assert declined["ok"] is False
    assert dummy.getValue("temp_garten") == 18.5

    accepted = invoke(processor, **params, confirm_write="true")
    assert accepted["ok"], accepted
    assert dummy.getValue("temp_garten") == 24.5


def test_unknown_tools_and_invalid_arguments_are_rejected(console_processor):
    processor, _ = console_processor
    for name, raw in [
        ("not_a_tool", "{}"),
        ("get_values", "[]"),
        ("get_values", "not json"),
        ("get_values", '{"fields":["unpublished"]}'),
        ("get_history", '{"days":7}'),
    ]:
        result = invoke(processor, tool_name=name, arguments=raw)
        assert result["ok"] is False, (name, raw, result)


def test_get_forbidden_even_for_read_only_tools(console_processor):
    processor, _ = console_processor
    app = Flask(__name__)
    with app.test_request_context("/run", method="GET"):
        result = processor.process(
            ["overview"], "example-hash", mcp_action="execute_tool",
            tool_name="get_values", arguments="{}",
        )
    assert result["ok"] is False


def test_view_renders_registered_tool_details(console_processor):
    processor, _ = console_processor
    app = Flask(__name__)
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    env = Environment(
        loader=FileSystemLoader(str(Path(__file__).resolve().parents[1] / "plugins"
                                    / "signaldeck_ai" / "templates")),
        autoescape=select_autoescape(["html"]),
    )
    env.filters["tojson"] = lambda data, indent=2: json.dumps(data, indent=indent)
    data = SimpleNamespace(
        hash="example-hash", name="mcp_climate", status="running",
        url="http://127.0.0.1:8766/mcp",
        instructions=processor.runtime.instructions,
        tools=processor.registered_tool_catalog(),
    )
    html = env.get_template("ai/mcp.html").render(displayData=data)
    assert 'data-actionhash="example-hash"' in html
    assert "get_values" in html
    assert "get_history" in html
    assert "execute_action" in html
    assert "Schreibaktion" in html


def test_point_lookup_tool_requires_full_timestamp_and_returns_one_value(console_processor):
    processor, _ = console_processor
    result = invoke(
        processor, tool_name="get_historical_value",
        arguments=json.dumps({
            "name": "hist_temp_garten",
            "date": "2026-10-20T14:30:00Z",
        }),
    )
    assert result["ok"], result
    structured = result["result"]
    assert isinstance(structured, dict), structured
    assert "2026-10-20T14:30:00Z" in json.dumps(structured)

    for invalid in ["2026-10-20", "2026-10-20T14:30", "2026-10-20T99:30:00Z",
                    "2026-10-20T14:30:00"]:
        rejected = invoke(
            processor, tool_name="get_historical_value",
            arguments=json.dumps({"name": "hist_temp_garten", "date": invalid}),
        )
        assert rejected["ok"] is False, (invalid, rejected)


def test_diagnostic_tools_use_structured_response_not_duplicate_text(console_processor):
    processor, _ = console_processor
    for tool_name, arguments in [
        ("get_values", {"fields": ["temp_garten"]}),
        ("get_history", {"name": "hist_temp_garten", "start": "2026-10-20T00:00:00Z",
                         "end": "2026-10-21T00:00:00Z", "include_points": True}),
        ("get_historical_value", {"name": "hist_temp_garten",
                                  "date": "2026-10-20T14:30:00Z"}),
    ]:
        result = invoke(
            processor, tool_name=tool_name,
            arguments=json.dumps(arguments),
        )
        assert result["ok"], result
        # Previously this was a two-item list: [text content blocks, dict].
        assert isinstance(result["result"], dict), result
