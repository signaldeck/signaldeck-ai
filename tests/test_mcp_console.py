"""Diagnostic MCP console uses the exact FastMCP registry and SignalDeck actions."""
import json
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
    assert set(tools) == {"get_values", "get_history", "execute_action", "run"}
    assert "fields" in tools["get_values"]["input_schema"]["properties"]
    assert "name" in tools["get_history"]["input_schema"]["properties"]
    history_description = tools["get_history"]["description"]
    assert "AGGREGATE STATISTICS" in history_description
    assert "ALL measurements" in history_description
    assert "inclusive END date" in history_description
    assert "date" in tools["get_history"]["input_schema"]["required"]
    props = tools["get_history"]["input_schema"]["properties"]
    assert "aggregate statistics" in props["include_points"]["description"]
    assert "No individual points" in props["include_points"]["description"]
    assert "evenly sampled" in props["max_points"]["description"]

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
            "name": "hist_temp_garten", "date": "2026-10-24", "days": 2, "include_points": True
        }),
    )
    assert result["ok"], result
    assert "hist_temp_garten" in json.dumps(result["result"])


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
