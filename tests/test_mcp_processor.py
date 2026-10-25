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
            {"type": "field", "name": "keller", "from": "temp_export"},
            {"type": "field", "name": "hidden", "from": "hidden_export"}
        ],
        "methods": [
            {"name": "keller_hist", "from": "history_export"},
            {"name": "setpoint", "from": "action_export"}
        ],
        "mcp_data": {
            "values": [
                {"field": "keller", "description": "Temperature in basement", "unit": "°C"}
            ],
            "data_rows": [
                {"func": "keller_hist", "description": "Basement time series", "max_days": 2}
            ],
            "actions": [
                {"func": "setpoint", "description": "Set a controlled test value",
                 "parameters": {
                     "type": "object", "properties": {
                         "setpoint": {"type": "number", "minimum": 0, "maximum": 30}
                     },
                     "required": ["setpoint"], "additionalProperties": False
                 }}
            ]
        },
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
    assert "hidden" not in response
    assert ("getValue", "hidden_export") not in provider.calls


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


def test_mcp_data_rejects_names_not_bound_by_native_processor_config():
    processor, provider = make_processor()
    config = dict(processor.config)
    config["mcp_data"] = {
        "values": [{"field": "raw_source_name", "description": "Not a local alias"}]
    }
    import logging
    with pytest.raises(ValueError, match="unknown"):
        MCPServerProcessor(
            "mcp_climate", config, SimpleNamespace(logger=logging.getLogger("tests")),
            provider, False,
        )


def test_mcp_value_method_binding_preserves_config_args_and_params():
    processor, provider = make_processor()
    processor.config["values"].append({
        "type": "method", "name": "derived_value", "from": "history_export",
        "args": [1], "params": {"mode": "summary"},
    })
    processor.config["mcp_data"]["values"].append({
        "field": "derived_value", "description": "Derived history value",
    })
    # Construct the processor from the final configuration.
    import logging
    processor = MCPServerProcessor(
        "mcp_climate", processor.config,
        SimpleNamespace(logger=logging.getLogger("tests")), provider, False,
    )
    results = processor.read_values()
    assert "derived_value" in results
    assert any(
        call[0] == "getMethodValue" and call[1] == "history_export"
        and call[2].get("mode") == "summary"
        for call in provider.calls
    )


def test_selected_values_only_read_requested_aliases():
    processor, provider = make_processor()
    processor.config["values"].append({
        "type": "field", "name": "garten", "from": "garden_export"
    })
    processor.config["mcp_data"]["values"].append({
        "field": "garten", "description": "Temperature in garden"
    })
    import logging
    processor = MCPServerProcessor(
        "mcp_climate", processor.config,
        SimpleNamespace(logger=logging.getLogger("tests")), provider, False,
    )
    result = processor.read_values(fields=["garten"])
    assert list(result) == ["garten"]
    assert provider.calls == [("getValue", "garden_export")]
    assert set(processor.read_values().keys()) == {"garten", "keller"}


def test_selected_values_reject_invalid_fields_before_any_read():
    processor, provider = make_processor()
    with pytest.raises(ValueError):
        processor.read_values(fields=["keller", "hidden"])
    with pytest.raises(ValueError):
        processor.read_values(fields="keller")
    assert provider.calls == []


def test_mcp_server_instructions_are_loaded_from_config():
    from signaldeck_ai.mcp_server import MCPRuntime
    processor, _ = make_processor()
    processor.config["mcp"] = {
        "port": 8766,
        "instructions": "Nutze Messwerte zur Beantwortung von Energiefragen."
    }
    runtime = MCPRuntime(processor)
    assert runtime.instructions == "Nutze Messwerte zur Beantwortung von Energiefragen."
    assert runtime.mcp.instructions == runtime.instructions


def test_mcp_server_rejects_non_string_instructions():
    from signaldeck_ai.mcp_server import MCPRuntime
    processor, _ = make_processor()
    processor.config["mcp"] = {"instructions": ["not", "text"]}
    with pytest.raises(ValueError, match="instructions"):
        MCPRuntime(processor)
