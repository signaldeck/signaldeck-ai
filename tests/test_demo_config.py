"""Integration tests: real main-plugin DummyData -> SDK ValueProvider -> MCP."""
import json
from datetime import datetime
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
    history = mcp.read_history("hist_temp_garten", days=7, include_points=True)
    assert history["statistics"]["count"] == 7
    assert len(history["points"]) == 7
    assert history["statistics"]["minimum"] < history["statistics"]["maximum"]
    assert all(datetime.fromisoformat(point["time"]) for point in history["points"])
    assert all(point["time_inferred"] is True for point in history["points"])
    assert all(not point["time"].startswith("day-") for point in history["points"])
    assert mcp.read_history("hist_temp_wohnzimmer", days=2)["statistics"]["count"] == 2
    with pytest.raises(ValueError):
        mcp.read_history("unknown_method", days=1)


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
        "get_values", "get_history", "execute_action"
    }


def test_demo_config_is_not_a_python_package():
    from signaldeck_ai import __file__ as package_init
    package_path = Path(package_init).resolve().parent
    assert not (package_path / "config").exists()
