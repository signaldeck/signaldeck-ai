"""Integration tests: real main-plugin DummyData -> SDK ValueProvider -> MCP."""
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from signaldeck_sdk import ValueProvider
from signaldeck_plugin_main.processors.dummy_data.data import Data
from signaldeck_ai.mcp_processor import MCPServerProcessor
from signaldeck_ai.mcp_server import MCPRuntime


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def demo():
    raw = json.loads((ROOT / "config" / "ai.json").read_text(encoding="utf-8"))
    configs = {p["name"]: p["config"] for p in raw["processors"]}
    vp = ValueProvider()
    dummy = Data("dummy_temperature", configs["dummy_temperature"],
                 SimpleNamespace(), vp, False)
    mcp = MCPServerProcessor("mcp_climate", configs["mcp_climate"],
                             SimpleNamespace(), vp, False)
    return dummy, mcp, vp, raw


def test_real_dummy_data_exports_and_selected_values(demo):
    dummy, mcp, provider, raw = demo
    assert [p["name"] for p in raw["processors"][:2]] == [
        "dummy_temperature", "mcp_climate"
    ]
    assert set(mcp.read_values()) == {"temp_garten", "temp_wohnzimmer", "temp_keller"}
    result = mcp.read_values(fields=["temp_garten"])
    assert result == {"temp_garten": {
        "value": 18.5, "description": "Aktuelle simulierte Temperatur im Garten",
        "unit": "°C",
    }}
    assert set(provider.values) == {
        "temperature_garten", "temperature_wohnzimmer", "temperature_keller"
    }


def test_dummy_historical_temperature_is_synthetic(demo):
    dummy, mcp, provider, _ = demo
    history = mcp.read_history("hist_temp_garten", date=(datetime.now() - timedelta(days=6)).date().isoformat(), days=7, include_points=True)
    assert 6 * 48 <= history["statistics"]["count"] <= 7 * 48
    assert len(history["points"]) == 200  # bounded default sample
    assert history["sampled"] is True
    assert history["total_points"] == history["statistics"]["count"]
    assert history["statistics"]["minimum"] < history["statistics"]["maximum"]
    assert all(datetime.fromisoformat(point["time"]) for point in history["points"])
    assert all("time_inferred" not in point for point in history["points"])
    assert all(not point["time"].startswith("day-") for point in history["points"])
    assert len({point["time"][:10] for point in history["points"]}) >= 6
    assert 48 <= mcp.read_history("hist_temp_wohnzimmer", date=(datetime.now() - timedelta(days=1)).date().isoformat(), days=2)["statistics"]["count"] <= 96
    with pytest.raises(ValueError):
        mcp.read_history("unknown_method", date=datetime.now().date().isoformat(), days=1)


def test_mock_write_changes_only_dummy_temperature(demo):
    dummy, mcp, provider, _ = demo
    before = mcp.read_values(fields=["temp_keller"])["temp_keller"]["value"]
    mcp.execute_action("set_temp_garten", {"temp_garten": 24.75})
    assert mcp.read_values(fields=["temp_garten"])["temp_garten"]["value"] == 24.75
    assert mcp.read_values(fields=["temp_keller"])["temp_keller"]["value"] == before
    with pytest.raises(Exception):
        mcp.execute_action("set_temp_garten", {"temp_garten": 90})
    with pytest.raises(ValueError):
        mcp.execute_action("hist_temp_garten", {"days": 7})


def test_runtime_offers_only_three_stable_tools(demo):
    dummy, mcp, provider, _ = demo
    runtime = MCPRuntime(mcp)
    assert runtime.mcp.instructions == mcp.config["mcp"]["instructions"]
    assert set(runtime.mcp._tool_manager.list_tools()[i].name
               for i in range(len(runtime.mcp._tool_manager.list_tools()))) == {
        "get_values", "get_history", "execute_action", "run"
    }


def test_demo_config_is_not_a_python_package():
    from signaldeck_ai import __file__ as package_init
    package_path = Path(package_init).resolve().parent
    assert not (package_path / "config").exists()


def test_llm_script_view_uses_real_main_plugin_script_processor():
    from signaldeck_sdk import Cmd, ScriptDefinition
    from signaldeck_plugin_main.processors.script.script_processor import ScriptProcessor
    from signaldeck_ai.chat_processor import AGUIChatProcessor
    import asyncio

    config = json.loads((ROOT / "config" / "ai.json").read_text(encoding="utf-8"))
    group = json.loads((ROOT / "config" / "group_ai.json").read_text(encoding="utf-8"))
    processors = {item["name"]: item for item in config["processors"]}
    scripts = {item["name"]: item for item in config["cmd"]["script"]}

    assert processors["script"]["class"] == (
        "signaldeck_plugin_main.processors.script.script_processor.ScriptProcessor"
    )
    assert processors["agui"]["config"]["cmd"]["name"] == "LLM"
    script = ScriptDefinition.from_dict(scripts["demo_llm"])
    assert script.commands == ["LLM $prompt$"]
    assert [(v.name, v.type) for v in script.variables] == [("prompt", "str")]
    assert script.variables[0].default
    assert any(item["type"] == "script" and item["value"] == ["demo_llm"]
               for item in group)

    # Exercise the Main plugin's actual view selection, not a local replica.
    vp = ValueProvider()
    processor = ScriptProcessor(
        "script", processors["script"]["config"], SimpleNamespace(), vp, False
    )
    loop = asyncio.new_event_loop()
    try:
        cmd = Cmd(loop)
        cmd.registerScripts(config["cmd"]["script"])
        processor.registerCommands(cmd)
        assert processor._resolve_selection(["demo_llm"]) == "demo_llm"
        assert cmd.getScript("demo_llm").commands == ["LLM $prompt$"]
    finally:
        loop.close()


def test_socketio_haus_demo_uses_real_merged_incoming_router():
    from signaldeck_plugin_socketio.incoming_routes import IncomingMessageRouter
    from signaldeck_plugin_socketio.service import SocketIOService
    from signaldeck_plugin_socketio.processor import SocketIOProcessor
    from signaldeck_ai.chat_processor import AGUIChatProcessor

    raw = json.loads((ROOT / "config" / "ai.json").read_text(encoding="utf-8"))
    group = json.loads((ROOT / "config" / "group_ai.json").read_text(encoding="utf-8"))
    processors = {item["name"]: item for item in raw["processors"]}
    socketio = processors["socketio"]
    ai = processors["agui"]["config"]
    assert socketio["class"] == (
        "signaldeck_plugin_socketio.processor.SocketIOProcessor"
    )
    assert SocketIOProcessor.__name__ == "SocketIOProcessor"
    cfg = socketio["config"]
    assert any(room["name"] == "haus" and room["persist"] is False
               for room in cfg["rooms"])
    assert cfg["incoming_message_routes"] == [{
        "rooms": ["haus"], "type": "chat",
        "prefix": "@haus", "source": "socketio.ai",
    }]
    assert cfg["message_routes"] == [{
        "source": "ai.socketio.haus", "rooms": ["haus"],
    }]
    assert ai["socketio_bridge"]["routes"] == [{
        "room": "haus",
        "source": "socketio.ai",
        "response_source": "ai.socketio.haus",
    }]
    assert any(item["type"] == "socketio" and item["value"] == ["haus"]
               for item in group)
    assert any(item["type"] == "socketio" and item["value"] == ["overview"]
               for item in group)

    service = SocketIOService()
    service.configure(cfg)
    assert service.incoming_router is not None
    assert service.room_settings("haus").persist is False

    messages = []
    class Context:
        def message(self, source, content, *, metadata):
            messages.append((source, content, metadata))

    event = {
        "type": "chat", "operation": "upsert", "room": "haus",
        "id": "demo-message", "transmitter": "demo-client",
        "transmitterName": "Demo",
        "payload": {
            "text": "@haus Wie warm ist es im Garten?",
            "contentType": "text",
            "system": False,
        },
    }
    assert service.incoming_router.publish(Context(), event) == 1
    assert messages[0][0] == "socketio.ai"
    assert messages[0][1] == "Wie warm ist es im Garten?"
    assert messages[0][2]["room"] == "haus"
    assert service.incoming_router.publish(Context(), {
        **event, "payload": {"text": "Normale Nachricht"}
    }) == 0
    assert len(messages) == 1
