from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from signaldeck_ai.mcp_processor import MCPServerProcessor


class Provider:
    def __init__(self):
        self.calls = []
        self.methods = {
            "history_export": (None, lambda *args, **kwargs: None),
            "action_export": (None, lambda *args, **kwargs: None),
        }
    def registerProcessor(self, processor, export):
        pass
    def getValue(self, name, refresh=True):
        self.calls.append(("getValue", name))
        return 23.5
    def getMethodValue(self, name, *args, **kwargs):
        self.calls.append(("getMethodValue", name, kwargs))
        if name == "history_export":
            days = kwargs.get("days", 0)
            return {f"2026-10-{24-days:02d}": 20 + days}
        if name == "action_export":
            return {"ok": True, "setpoint": kwargs["setpoint"]}
        raise ValueError(name)


def make_processor():
    p = Provider()
    config = {
        "values": [
            {
                "type": "field", "name": "keller", "from": "temp_export",
                "description": "Temperature in basement", "unit": "°C"
            }
        ],
        "methods": [
            {
                "name": "keller_hist", "from": "history_export", "kind": "history",
                "description": "Basement time series", "max_days": 2
            },
            {
                "name": "setpoint", "from": "action_export", "kind": "action",
                "description": "Set a controlled test value",
                "parameters": {
                    "type": "object", "properties": {
                        "setpoint": {"type": "number", "minimum": 0, "maximum": 30}
                    },
                    "required": ["setpoint"], "additionalProperties": False
                }
            }
        ],
    }
    import logging
    ctx = SimpleNamespace(logger=logging.getLogger("tests"))
    return MCPServerProcessor("mcp_climate", config, ctx, p, False), p


def test_mcp_reuses_value_provider_and_descriptions():
    processor, provider = make_processor()
    response = processor.read_values()
    assert response["keller"]["value"] == 23.5
    assert response["keller"]["description"] == "Temperature in basement"
    assert ("getValue", "temp_export") in provider.calls


def test_histories_iterate_days_and_reject_excessive_range():
    processor, provider = make_processor()
    result = processor.read_history("keller_hist", days=2, include_points=True)
    assert result["statistics"]["count"] == 2
    assert result["statistics"]["maximum"] == 21
    assert any(item[2].get("days") == 0 and item[2].get("fullDay") is True for item in provider.calls if item[0] == "getMethodValue")
    assert any(item[2].get("days") == 1 and item[2].get("fullDay") is True for item in provider.calls if item[0] == "getMethodValue")
    with pytest.raises(ValueError):
        processor.read_history("keller_hist", days=3)
    with pytest.raises(ValueError):
        processor.read_history("unlisted", days=1)


def test_actions_validate_parameters_before_provider_invocation():
    processor, provider = make_processor()
    assert processor.execute_action("setpoint", {"setpoint": 20})["ok"]
    count = len(provider.calls)
    with pytest.raises(Exception):
        processor.execute_action("setpoint", {"setpoint": 60})
    with pytest.raises(Exception):
        processor.execute_action("setpoint", {"setpoint": 20, "extra": True})
    assert len(provider.calls) == count


def test_mcp_runtime_builds_streamable_http_asgi_app():
    from signaldeck_ai.mcp_server import MCPRuntime
    processor, _ = make_processor()
    runtime = MCPRuntime(processor)
    assert runtime.url == "http://127.0.0.1:8766/mcp"
    assert runtime.mcp.streamable_http_app() is not None
    assert runtime.status == "pending"
