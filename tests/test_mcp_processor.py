from datetime import datetime, timedelta
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
            if kwargs.get("currentValues") is True:
                return None
            if "date" not in kwargs:
                # The same exported method can also be mapped as a read-only
                # value with unrelated, explicit args/params.
                return {"2026-10-24": 20.0}
            assert kwargs.get("days") == 0
            assert kwargs.get("fullDay") is True
            requested = kwargs["date"].date()
            return {requested.isoformat(): float(requested.day)}
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
    result = processor.read_history("keller_hist", date="2026-10-24", days=2, include_points=True)
    assert result["statistics"]["count"] == 2
    assert result["statistics"]["maximum"] == 25
    assert result["date"] == "2026-10-24"
    assert result["start_date"] == "2026-10-24"
    assert result["end_date"] == "2026-10-25"
    assert all(item[2].get("days") == 0 and item[2].get("fullDay") is True for item in provider.calls if item[0] == "getMethodValue" and not item[2].get("currentValues"))
    assert {item[2]["date"].date().isoformat() for item in provider.calls if item[0] == "getMethodValue" and not item[2].get("currentValues")} == {"2026-10-24", "2026-10-25"}
    with pytest.raises(ValueError):
        processor.read_history("keller_hist", date="2026-10-24", days=3)
    with pytest.raises(ValueError):
        processor.read_history("unlisted", date="2026-10-24", days=1)


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


@pytest.mark.parametrize("date,days", [
    ("2026-02-30", 1), ("2026-10-24T12:00:00", 1),
    ("2026-10-24", 0), ("2026-10-24", True), ("2026-10-24", 3),
    ("2999-01-01", 1),
])
def test_get_history_rejects_invalid_or_unbounded_dates(date, days):
    processor, provider = make_processor()
    with pytest.raises(ValueError):
        processor.read_history("keller_hist", date=date, days=days)
    assert provider.calls == []


def test_get_history_queries_forward_across_month_boundary_without_offset():
    processor, provider = make_processor()
    history = processor.read_history("keller_hist", date="2024-03-01", days=2,
                                     include_points=True)
    assert history["start_date"] == "2024-03-01"
    assert history["end_date"] == "2024-03-02"
    assert [row["time"][:10] for row in history["points"]] == ["2024-03-01", "2024-03-02"]
    assert all(call[2]["days"] == 0 for call in provider.calls)


def test_get_history_queries_leap_day_as_second_day():
    processor, provider = make_processor()
    history = processor.read_history(
        "keller_hist", date="2024-02-28", days=2, include_points=True
    )
    assert history["start_date"] == "2024-02-28"
    assert history["end_date"] == "2024-02-29"
    assert [row["time"][:10] for row in history["points"]] == [
        "2024-02-28", "2024-02-29",
    ]
    assert all(call[2]["days"] == 0 for call in provider.calls)


def test_get_history_rejects_future_end_of_window_without_provider_reads():
    processor, provider = make_processor()
    from datetime import datetime
    today = datetime.now().date().isoformat()
    with pytest.raises(ValueError, match="Future"):
        processor.read_history("keller_hist", date=today, days=2)
    assert provider.calls == []


def test_today_merges_cached_samples_and_prefers_cache_on_exact_timestamp():
    processor, provider = make_processor()
    today = datetime.now().date()
    yesterday = today - timedelta(days=1)
    persisted_time = today.isoformat() + "T11:00:00"
    new_time = today.isoformat() + "T11:01:00"
    old_cache_time = yesterday.isoformat() + "T23:59:00"
    original = provider.getMethodValue

    def history(name, *args, **kwargs):
        if kwargs.get("currentValues"):
            return {
                persisted_time: 22.0,
                new_time: 23.0,
                old_cache_time: 900.0,
            }
        if name == "history_export" and kwargs.get("date") is not None:
            # Keep persisted reading and RAM cache overlapping at 11:00.
            if kwargs["date"].date() == today:
                return {
                    today.isoformat() + "T10:55:00": 19.0,
                    persisted_time: 20.0,
                }
            return {
                yesterday.isoformat() + "T12:00:00": 18.0,
            }
        return original(name, *args, **kwargs)

    provider.getMethodValue = history
    result = processor.read_history(
        "keller_hist", date=yesterday.isoformat(),
        days=2, include_points=True,
    )
    assert [p["value"] for p in result["points"]] == [18.0, 19.0, 22.0, 23.0]
    assert result["statistics"]["count"] == 4
    assert result["statistics"]["maximum"] == 23.0
    assert result["total_points"] == 4
    assert len([p for p in result["points"] if p["time"] == persisted_time]) == 1
    assert all(p["value"] != 900.0 for p in result["points"])


def test_cache_is_not_queried_for_historical_day_or_range_provider():
    processor, provider = make_processor()
    yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
    processor.read_history("keller_hist", date=yesterday, days=1)
    assert not any(c[2].get("currentValues") for c in provider.calls)

    processor.history_specs["keller_hist"]["mode"] = "range"
    provider.calls.clear()
    processor.read_history("keller_hist", date=yesterday, days=1)
    assert not any(c[2].get("currentValues") for c in provider.calls)


def test_unavailable_or_scalar_cache_does_not_change_persisted_history():
    processor, provider = make_processor()
    today = datetime.now().date().isoformat()
    original = provider.getMethodValue

    def bad_cache(name, *args, **kwargs):
        if kwargs.get("currentValues"):
            return 99.0  # DummyData-compatible scalar, not a cache Series.
        return original(name, *args, **kwargs)

    provider.getMethodValue = bad_cache
    result = processor.read_history(
        "keller_hist", date=today, days=1, include_points=True
    )
    assert result["statistics"]["count"] == 1
    assert result["points"][0]["value"] == float(datetime.now().day)
