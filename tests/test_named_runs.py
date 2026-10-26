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
        self.commands = {"echo": object(), "sleep": object(), "socketio_send": object()}
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
        return self.startScript(name, **kwargs).future

    def startScript(self, name, **kwargs):
        self.calls.append(("script", name, kwargs))
        return SimpleNamespace(run_id=f"run-{len(self.calls)}", future=Future())


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
    assert registry.execute("Abend") == {
        "name": "Abend", "status": "scheduled", "run_id": "run-1",
    }
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


def test_scripts_require_concurrent_sdk_support():
    cmd = FakeCmd()
    cmd.startScript = None
    with pytest.raises(RuntimeError, match="concurrent script runs"):
        RunActions(specs()).bind(cmd)


def test_same_script_can_start_in_parallel_with_distinct_run_ids():
    registry = RunActions(specs())
    cmd = FakeCmd()
    registry.bind(cmd)
    first = registry.execute("Abend")
    second = registry.execute("Abend")
    assert first["run_id"] == "run-1"
    assert second["run_id"] == "run-2"
    assert cmd.calls == [
        ("script", "demo_abend", {}),
        ("script", "demo_abend", {}),
    ]


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


def _send_haus_spec():
    return {
        "name": "send_haus",
        "description": "Send a message to the fixed Socket.IO room haus",
        "command": "socketio_send",
        "args": ["haus", {"param": "msg"}],
        "parameters": {
            "type": "object",
            "properties": {
                "msg": {
                    "type": "string",
                    "description": "Text to be sent to the haus room",
                    "minLength": 1,
                    "maxLength": 500,
                }
            },
            "required": ["msg"],
            "additionalProperties": False,
        },
    }


def test_socketio_message_maps_free_text_to_fixed_room():
    registry = RunActions([_send_haus_spec()])
    cmd = FakeCmd()
    registry.bind(cmd)
    message = "Hallo aus SignalDeck! 😀 Wie läuft's?"
    assert registry.execute("send_haus", {"msg": message}) == {
        "name": "send_haus", "status": "scheduled",
    }
    assert cmd.calls[0][1] == ["socketio_send haus " + message]
    assert cmd.calls[0][2].startswith("mcp-run-")


@pytest.mark.parametrize("message", [
    "", " ", " führendes Leerzeichen", "Leerzeichen am Ende ",
    "erste Zeile\nzweite Zeile", "Zeile\rweiter",
    "Tab\tZeichen", "ESC\x1b[31m", "Null\x00Byte",
    "Bidi\u202eOverride", "Nullbreite\u200bZeichen",
    "x" * 501,
])
def test_free_text_rejects_controls_and_excessive_length(message):
    registry = RunActions([_send_haus_spec()])
    cmd = FakeCmd()
    registry.bind(cmd)
    with pytest.raises((ValueError, ValidationError)):
        registry.execute("send_haus", {"msg": message})
    assert not cmd.calls


def test_free_text_allows_printable_punctuation_without_executing_commands():
    registry = RunActions([_send_haus_spec()])
    cmd = FakeCmd()
    registry.bind(cmd)
    message = "Preis: 12,50 €; Status = 'OK' (alles gut)."
    registry.execute("send_haus", {"msg": message})
    assert cmd.calls[0][1] == ["socketio_send haus " + message]
    assert len(cmd.calls) == 1


def test_free_text_without_bounded_max_length_is_rejected():
    cfg = _send_haus_spec()
    del cfg["parameters"]["properties"]["msg"]["maxLength"]
    with pytest.raises(ValueError, match="maxLength"):
        RunActions([cfg])


@pytest.mark.parametrize("limit", [0, -1, 501, 1.5, True])
def test_free_text_rejects_invalid_max_length(limit):
    cfg = _send_haus_spec()
    cfg["parameters"]["properties"]["msg"]["maxLength"] = limit
    with pytest.raises(ValueError, match="maxLength"):
        RunActions([cfg])


def test_free_text_only_as_last_command_argument():
    cfg = _send_haus_spec()
    cfg["args"] = [{"param": "msg"}, "haus"]
    with pytest.raises(ValueError, match="final Cmd argument"):
        RunActions([cfg])


def test_free_text_not_supported_for_script_macro_expansion():
    cfg = _send_haus_spec()
    cfg.pop("command")
    cfg.pop("args")
    cfg["script"] = "demo_abend"
    cfg["variables"] = {"msg": {"param": "msg"}}
    with pytest.raises(ValueError, match="Cmd commands"):
        RunActions([cfg])


def test_legacy_unbounded_string_schema_stays_invalid():
    cfg = _send_haus_spec()
    cfg["parameters"]["properties"]["msg"] = {"type": "string"}
    with pytest.raises(ValueError):
        RunActions([cfg])
