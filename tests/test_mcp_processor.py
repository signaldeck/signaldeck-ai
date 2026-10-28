from datetime import datetime, timedelta, timezone
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
            assert kwargs["date"].tzinfo == timezone.utc
            return {requested.isoformat() + "T12:00:00+00:00": float(requested.day)}
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


def test_histories_iterate_utc_days_and_reject_excessive_range():
    processor, provider = make_processor()
    result = processor.read_history("keller_hist", start="2026-10-24T00:00:00Z",
                                    end="2026-10-26T00:00:00Z", include_points=True)
    assert result["statistics"]["count"] == 2
    assert result["statistics"]["maximum"] == 25
    assert result["start"] == "2026-10-24T00:00:00Z"
    assert result["end"] == "2026-10-26T00:00:00Z"
    assert all(item[2].get("days") == 0 and item[2].get("fullDay") is True for item in provider.calls if item[0] == "getMethodValue" and not item[2].get("currentValues"))
    assert {item[2]["date"].date().isoformat() for item in provider.calls if item[0] == "getMethodValue" and not item[2].get("currentValues")} == {"2026-10-24", "2026-10-25"}
    with pytest.raises(ValueError):
        processor.read_history("keller_hist", start="2026-10-24T00:00:00Z", end="2026-10-27T00:00:00Z")
    with pytest.raises(ValueError):
        processor.read_history("unlisted", start="2026-10-24T00:00:00Z", end="2026-10-25T00:00:00Z")


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


@pytest.mark.parametrize("start,end", [
    ("2026-10-24T12:00:00", "2026-10-24T13:00:00Z"),
    ("2026-10-24", "2026-10-24T13:00:00Z"),
    ("2026-02-30T12:00:00Z", "2026-10-24T13:00:00Z"),
    ("2026-10-24T12:00:00+24:00", "2026-10-24T13:00:00Z"),
    ("2026-10-24T12:00:00Z", "2026-10-24T13:00:00"),
    ("2026-10-24T12:00:00Z", "2026-10-24T12:00:00Z"),
    ("2026-10-24T13:00:00Z", "2026-10-24T12:00:00Z"),
    ("2026-10-24T00:00:00Z", "2026-10-27T00:00:01Z"),
    ("2999-01-01T00:00:00Z", "2999-01-01T01:00:00Z"),
    (None, "2026-10-24T13:00:00Z"),
])
def test_history_rejects_invalid_naive_unbounded_or_future_instants(start, end):
    processor, provider = make_processor()
    with pytest.raises(ValueError):
        processor.read_history("keller_hist", start=start, end=end)
    assert provider.calls == []


def test_equivalent_utc_and_offset_windows_read_same_instants():
    processor, provider = make_processor()
    utc = processor.read_history(
        "keller_hist", start="2026-10-24T10:00:00Z",
        end="2026-10-24T14:00:00Z", include_points=True,
    )
    provider.calls.clear()
    offset = processor.read_history(
        "keller_hist", start="2026-10-24T11:00:00+01:00",
        end="2026-10-24T15:00:00+01:00", include_points=True,
    )
    assert utc["points"] == offset["points"]
    assert utc["statistics"] == offset["statistics"]
    assert len(provider.calls) == 1


def test_history_across_utc_midnight_filters_exact_boundaries_and_no_duplicates():
    processor, provider = make_processor()

    def history(name, *args, **kwargs):
        provider.calls.append(("getMethodValue", name, kwargs))
        return {
            "2026-10-24T23:49:59Z": 0.0,
            "2026-10-24T23:50:00Z": 1.0,
            "2026-10-25T00:10:00+00:00": 2.0,
            "2026-10-25T00:20:00Z": 3.0,
        }

    provider.getMethodValue = history
    result = processor.read_history(
        "keller_hist", start="2026-10-25T00:50:00+01:00",
        end="2026-10-25T01:20:00+01:00", include_points=True,
    )
    assert [p["value"] for p in result["points"]] == [1.0, 2.0]
    assert result["statistics"]["count"] == 2
    assert [c[2]["date"].date().isoformat() for c in provider.calls] == [
        "2026-10-24", "2026-10-25",
    ]
    assert all(c[2]["date"].tzinfo == timezone.utc for c in provider.calls)


def test_history_preserves_source_offsets_and_orders_dst_fallback_by_instant():
    processor, provider = make_processor()

    def history(name, *args, **kwargs):
        return {
            "2026-10-25T02:15:00+01:00": 2.0,
            "2026-10-25T02:30:00+02:00": 1.0,
            "2026-10-25T02:30:00+01:00": 3.0,
        }

    provider.getMethodValue = history
    result = processor.read_history(
        "keller_hist", start="2026-10-25T00:00:00Z",
        end="2026-10-25T02:00:00Z", include_points=True,
    )
    assert [point["value"] for point in result["points"]] == [1.0, 2.0, 3.0]
    assert [point["time"] for point in result["points"]] == [
        "2026-10-25T02:30:00+02:00",
        "2026-10-25T02:15:00+01:00",
        "2026-10-25T02:30:00+01:00",
    ]
    assert result["statistics"]["first_time"].endswith("+02:00")
    assert result["statistics"]["last_time"].endswith("+01:00")


def test_history_rejects_naive_measurement_timestamps_instead_of_guessing_timezone():
    processor, provider = make_processor()
    provider.getMethodValue = lambda name, **kwargs: {"2026-10-24T12:00:00": 1.0}
    with pytest.raises(ValueError, match="offset"):
        processor.read_history(
            "keller_hist", start="2026-10-24T11:00:00Z",
            end="2026-10-24T13:00:00Z",
        )


def test_history_rejects_scalar_inferred_timestamps_in_absolute_window():
    processor, provider = make_processor()
    provider.getMethodValue = lambda name, **kwargs: 25.0
    with pytest.raises(ValueError, match="timestamp"):
        processor.read_history(
            "keller_hist", start="2026-10-24T11:00:00Z",
            end="2026-10-24T13:00:00Z",
        )


def test_history_filters_before_stats_and_bounded_sampling():
    processor, provider = make_processor()

    def history(name, **kwargs):
        return {
            "2026-10-24T06:59:59Z": 1000.0,
            "2026-10-24T07:00:00Z": 10.0,
            "2026-10-24T07:30:00Z": 20.0,
            "2026-10-24T07:59:59Z": 30.0,
            "2026-10-24T08:00:00Z": 1000.0,
        }

    provider.getMethodValue = history
    result = processor.read_history(
        "keller_hist", start="2026-10-24T07:00:00Z",
        end="2026-10-24T08:00:00Z",
        include_points=True, max_points=2,
    )
    assert result["statistics"] == {
        "count": 3, "numeric_count": 3, "minimum": 10.0,
        "maximum": 30.0, "mean": 20.0,
        "first_time": "2026-10-24T07:00:00+00:00",
        "last_time": "2026-10-24T07:59:59+00:00",
    }
    assert result["sampled"] is True
    assert result["total_points"] == 3
    assert [p["value"] for p in result["points"]] == [10.0, 30.0]


def test_absolute_window_applies_to_custom_range_provider():
    processor, provider = make_processor()
    processor.history_specs["keller_hist"]["mode"] = "range"

    def history(name, **kwargs):
        provider.calls.append(("getMethodValue", name, kwargs))
        return {
            "2026-10-24T07:00:00Z": 1.0,
            "2026-10-24T08:00:00Z": 2.0,
            "2026-10-25T07:00:00Z": 3.0,
            "2026-10-25T08:00:00Z": 4.0,
        }

    provider.getMethodValue = history
    result = processor.read_history(
        "keller_hist", start="2026-10-24T07:00:00Z",
        end="2026-10-25T08:00:00Z", include_points=True,
    )
    assert [p["value"] for p in result["points"]] == [1.0, 2.0, 3.0]
    assert provider.calls[0][2] == {
        "date": datetime(2026, 10, 24, tzinfo=timezone.utc), "days": 2,
    }


def test_recent_cache_wins_on_same_instant_even_with_different_offsets():
    processor, provider = make_processor()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    start = now - timedelta(minutes=3)
    end = now + timedelta(minutes=1)
    persisted_time = (now - timedelta(minutes=2)).isoformat()
    cache_time = (now - timedelta(minutes=2)).astimezone(
        timezone(timedelta(hours=1))
    ).isoformat()
    new_time = (now - timedelta(minutes=1)).isoformat()

    def history(name, **kwargs):
        if kwargs.get("currentValues"):
            return {cache_time: 22.0, new_time: 23.0}
        return {persisted_time: 20.0}

    provider.getMethodValue = history
    result = processor.read_history(
        "keller_hist", start=start.isoformat(), end=end.isoformat(), include_points=True,
    )
    assert [point["value"] for point in result["points"]] == [22.0, 23.0]
    assert result["statistics"]["count"] == 2
    assert result["total_points"] == 2
    assert result["points"][0]["time"] == cache_time


def test_cache_is_not_queried_for_old_or_range_history():
    processor, provider = make_processor()
    processor.read_history(
        "keller_hist", start="2026-10-24T00:00:00Z", end="2026-10-25T00:00:00Z",
    )
    assert not any(c[2].get("currentValues") for c in provider.calls)

    processor.history_specs["keller_hist"]["mode"] = "range"
    provider.calls.clear()
    provider.getMethodValue = lambda name, **kwargs: (
        provider.calls.append(("getMethodValue", name, kwargs)) or {}
    )
    processor.read_history(
        "keller_hist", start="2026-10-24T00:00:00Z", end="2026-10-25T00:00:00Z",
    )
    assert len(provider.calls) == 1
    assert provider.calls[0][2]["days"] == 1


def test_unavailable_or_scalar_cache_does_not_change_persisted_history():
    processor, provider = make_processor()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    original = provider.getMethodValue

    def bad_cache(name, **kwargs):
        if kwargs.get("currentValues"):
            return 99.0
        if "date" in kwargs:
            return {now.isoformat(): 20.0}
        return original(name, **kwargs)

    provider.getMethodValue = bad_cache
    result = processor.read_history(
        "keller_hist", start=(now - timedelta(minutes=1)).isoformat(),
        end=(now + timedelta(minutes=1)).isoformat(), include_points=True,
    )
    assert result["statistics"]["count"] == 1
    assert result["points"][0]["value"] == 20.0


def test_point_lookup_passes_equivalent_aware_instants_to_provider():
    processor, provider = make_processor()
    original = provider.getMethodValue

    def get_method(name, **kwargs):
        if kwargs.get("fullDay") is False:
            provider.calls.append(("getMethodValue", name, kwargs))
            return 321.5
        return original(name, **kwargs)

    provider.getMethodValue = get_method
    for date in ["2026-10-20T14:30:00Z", "2026-10-20T15:30:00+01:00"]:
        result = processor.read_historical_value("keller_hist", date=date)
        assert result["value"] == 321.5
        assert result["requested_time"] == date
        assert provider.calls[-1][2] == {
            "date": datetime(2026, 10, 20, 14, 30, tzinfo=timezone.utc),
            "days": 0, "fullDay": False,
        }


@pytest.mark.parametrize("invalid", [
    "2026-10-20", "2026-10-20T14:30", "2026-10-20T24:30:00",
    "2026-02-30T12:30:00Z", "2026-10-20T14:30:00",
    "2026-10-20T14:30:00+24:00", "2026-10-20 14:30:00Z",
    "2999-01-01T10:00:00Z", "", None,
])
def test_point_lookup_requires_valid_nonfuture_aware_timestamp(invalid):
    processor, provider = make_processor()
    with pytest.raises(ValueError):
        processor.read_historical_value("keller_hist", date=invalid)
    assert not provider.calls


def test_point_lookup_respects_allowlist_and_rejects_non_per_day_method():
    processor, provider = make_processor()
    with pytest.raises(ValueError):
        processor.read_historical_value(
            "not_allowed", date="2026-10-20T14:30:00Z",
        )
    processor.history_specs["keller_hist"]["mode"] = "range"
    with pytest.raises(ValueError, match="per-day"):
        processor.read_historical_value(
            "keller_hist", date="2026-10-20T14:30:00Z",
        )
    assert not provider.calls
