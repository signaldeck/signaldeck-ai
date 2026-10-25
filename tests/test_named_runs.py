"""Named MCP runs must never expose unrestricted Cmd/script commands."""
from concurrent.futures import Future
from types import SimpleNamespace

import pytest
from jsonschema import ValidationError

from signaldeck_ai.run_actions import RunActions
from signaldeck_ai.mcp_processor import MCPServerProcessor
from signaldeck_ai.mcp_server import MCPRuntime


class FakeCmd:
    def __init__(self):
        self.commands = {"echo": object(), "sleep": object()}
        self.alias = {}
        self.script = {"demo_abend": SimpleNamespace(variables=[])}
        self.tasks = {}
        self.calls = []

    def getScript(self, name):
        return self.script.get(name)

    def run(self, commands, name=None, **kwargs):
        self.calls.append(("cmd", list(commands), name, kwargs))
        return Future()

    def runScript(self, name, **kwargs):
        self.calls.append(("script", name, kwargs))
        return Future()


def specs():
    return [
        {
            "name": "Abend", "description": "Start a fixed demo script",
            "script": "demo_abend",
        },
        {
            "name": "Pruefen", "description": "Run bounded echo command",
            "command": "echo",
            "args": ["demo", {"param": "count"}],
            "parameters": {
                "type": "object",
                "properties": {"count": {
                    "type": "integer", "minimum": 1, "maximum": 5,
                }},
                "required": ["count"],
                "additionalProperties": False,
            },
        },
    ]


def test_named_script_start_is_only_scheduled_and_no_dynamic_source():
    registry = RunActions(specs())
    cmd = FakeCmd()
    registry.bind(cmd)
    assert registry.execute("Abend") == {"name": "Abend", "status": "scheduled"}
    assert cmd.calls == [("script", "demo_abend", {})]
    exposed = registry.descriptions()
    assert {item["name"] for item in exposed} == {"Abend", "Pruefen"}
    assert all("script" not in item and "command" not in item for item in exposed)


def test_bounded_command_start_maps_parameters_to_fixed_tokens():
    registry = RunActions(specs())
    cmd = FakeCmd()
    registry.bind(cmd)
    assert registry.execute("Pruefen", {"count": 2})["status"] == "scheduled"
    assert cmd.calls[0][1] == ["echo demo 2"]
    assert cmd.calls[0][2].startswith("mcp-run-")


@pytest.mark.parametrize("name,args", [
    ("unknown", {}),
    ("echo", {}),
    ("Pruefen", {"count": 100}),
    ("Pruefen", {"count": "2"}),
    ("Pruefen", {"count": 1, "command": "sleep"}),
    ("Pruefen", {"count": 2, "extra": True}),
    ("Pruefen", {"count": "2;shutdown"}),
])
def test_unpublished_commands_or_invalid_parameters_never_execute(name, args):
    registry = RunActions(specs())
    cmd = FakeCmd()
    registry.bind(cmd)
    with pytest.raises((ValueError, ValidationError)):
        registry.execute(name, args)
    assert cmd.calls == []


@pytest.mark.parametrize("bad", [
    {"name": "bad", "description": "cmd line", "command": "echo",
     "args": ["x;rm"]},
    {"name": "bad", "description": "cmd line", "command": "echo",
     "args": [{"param": "str"}],
     "parameters": {"type": "object", "properties": {
         "str": {"type": "string"}
     }, "required": ["str"], "additionalProperties": False}},
    {"name": "bad", "description": "cmd line", "command": "echo",
     "args": [{"param": "x"}],
     "parameters": {"type": "object", "properties": {
         "x": {"type": "string", "enum": ["abc;rm"]}
     }, "required": ["x"], "additionalProperties": False}},
    {"name": "bad", "description": "both",
     "command": "echo", "script": "demo_abend"},
    {"name": "bad", "description": "cmd line", "command": "echo",
     "args": [{"param": "x"}], "parameters": {"type": "object",
      "properties": {"x": {"type": "string", "enum": ["safe"]}},
      "additionalProperties": True, "required": ["x"]}},
])
def test_unsafe_configuration_fails_closed(bad):
    with pytest.raises(ValueError):
        RunActions([bad])


def test_script_alias_must_resolve_and_command_must_not_be_shadowed():
    cmd = FakeCmd()
    cmd.alias["echo"] = "sleep 10"
    with pytest.raises(ValueError, match="shadowed"):
        RunActions(specs()).bind(cmd)
    cmd.alias.clear()
    del cmd.script["demo_abend"]
    with pytest.raises(ValueError, match="unknown Cmd script"):
        RunActions(specs()).bind(cmd)


def test_same_script_cannot_start_in_parallel():
    registry = RunActions(specs())
    cmd = FakeCmd()
    registry.bind(cmd)
    running = Future()
    cmd.tasks["demo_abend"] = running
    with pytest.raises(ValueError, match="already running"):
        registry.execute("Abend")
    assert cmd.calls == []


def test_immediately_failed_future_is_reported():
    registry = RunActions(specs())
    cmd = FakeCmd()

    def failed_run(*args, **kwargs):
        failure = Future()
        failure.set_exception(ValueError("invalid command"))
        return failure

    cmd.run = failed_run
    registry.bind(cmd)
    with pytest.raises(ValueError, match="invalid command"):
        registry.execute("Pruefen", {"count": 2})


def test_registration_is_conditional_and_tool_name_stable():
    class VP:
        def registerProcessor(self, processor, export):
            pass

    config = {"mcp_data": {"runs": specs()}}
    processor = MCPServerProcessor(
        "run_test", config, SimpleNamespace(), VP(), False
    )
    cmd = FakeCmd()
    processor.registerCommands(cmd)
    runtime = MCPRuntime(processor)
    tools = {tool["name"]: tool for tool in runtime.tool_catalog()}
    assert set(tools) == {"run"}
    assert tools["run"]["is_action"]
    assert "Abend" in tools["run"]["description"]
    assert "Pruefen" in tools["run"]["description"]
    assert "demo_abend" not in tools["run"]["description"]
    assert processor.run_named_action("Abend")["status"] == "scheduled"


def test_script_args_require_defined_script_variables():
    registry = RunActions([{
        "name": "Hint", "description": "Strict script variable",
        "script": "demo_abend",
        "variables": {"unknown": {"param": "value"}},
        "parameters": {"type": "object", "properties": {
            "value": {"type": "string", "enum": ["safe"]},
        }, "required": ["value"], "additionalProperties": False},
    }])
    cmd = FakeCmd()
    with pytest.raises(ValueError, match="unknown script variable"):
        registry.bind(cmd)
