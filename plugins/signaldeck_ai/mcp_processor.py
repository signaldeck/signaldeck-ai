"""SignalDeck MCP processor: native config.values/config.methods bindings."""
from __future__ import annotations

import json
from threading import RLock
from typing import Any

from jsonschema import Draft202012Validator
from signaldeck_sdk import DisplayProcessor, DisplayData

from .mcp_server import MCPRuntime
from .serialization import bounded_sample, json_safe, series_points, summarize_points


class MCPDisplayData(DisplayData):
    def __init__(self, ctx, action_hash, processor):
        super().__init__(ctx, action_hash)
        self.processor = processor
        self.name = processor.name
        self.status = processor.runtime.status if processor.runtime else "pending"
        self.url = processor.runtime.url if processor.runtime else ""
        self.tools = processor.tool_descriptions()


class MCPServerProcessor(DisplayProcessor):
    """Expose *only* explicitly configured ValueProvider aliases over MCP.

    config.values and config.methods use the unmodified SignalDeck SDK
    syntax. 'description' / 'kind' add only MCP exposure metadata.
    """

    def __init__(self, name, config, ctx, valueProvider, collect_data):
        super().__init__(name, config, ctx, valueProvider, collect_data)
        self._read_lock = RLock()
        self.value_specs = self._specs("values")
        all_methods = self._specs("methods")
        self.history_specs = {
            key: item for key, item in all_methods.items()
            if item.get("kind") == "history"
        }
        self.action_specs = {
            key: item for key, item in all_methods.items()
            if item.get("kind") == "action"
        }
        self._validate()
        self.runtime: MCPRuntime | None = None

    def _specs(self, key: str) -> dict[str, dict[str, Any]]:
        result = {}
        for raw in self.config.get(key, []):
            if not isinstance(raw, dict):
                raise ValueError(f"{key}: each entry must be an object")
            name = raw.get("name")
            if not isinstance(name, str) or not name or name in result:
                raise ValueError(f"{key}: missing or duplicate alias name")
            if not isinstance(raw.get("from"), str) or not raw["from"]:
                raise ValueError(f"{key}.{name}: a ValueProvider 'from' reference is required")
            if not isinstance(raw.get("description"), str) or not raw["description"].strip():
                raise ValueError(f"{key}.{name}: an MCP description is required")
            result[name] = dict(raw)
        return result

    def _validate(self) -> None:
        for name, item in self.value_specs.items():
            if item.get("type") not in {"field", "method"}:
                raise ValueError(f"values.{name}: type must be field or method")
        for item in self.config.get("methods", []):
            if item.get("kind") not in {"history", "action"}:
                raise ValueError(
                    f"methods.{item.get('name')}: kind must be history or action"
                )
        for name, item in self.history_specs.items():
            if not 1 <= int(item.get("max_days", 30)) <= 366:
                raise ValueError(f"methods.{name}: max_days must be 1..366")
            if item.get("mode", "per_day") not in {"per_day", "range"}:
                raise ValueError(f"methods.{name}: mode must be per_day or range")
        for name, item in self.action_specs.items():
            schema = item.get("parameters")
            if not isinstance(schema, dict) or schema.get("type") != "object":
                raise ValueError(f"methods.{name}: action parameters require an object schema")
            if schema.get("additionalProperties") is not False:
                raise ValueError(f"methods.{name}: action schema must forbid additionalProperties")
            Draft202012Validator.check_schema(schema)

    def tool_descriptions(self) -> list[dict[str, Any]]:
        result = [{"name": "get_values", "description": "Read current values"}]
        if self.history_specs:
            result.append({"name": "get_history", "description": "Read configured histories"})
        if self.action_specs:
            result.append({"name": "execute_action", "description": "Run configured actions"})
        return result

    def read_values(self) -> dict[str, Any]:
        with self._read_lock:
            # Processor.refresh uses the normal ValueProvider APIs to resolve
            # references by their configured 'from' names.
            self.refresh()
            return {
                name: {
                    "description": spec["description"],
                    "value": json_safe(getattr(self, name)),
                    **({"unit": spec["unit"]} if "unit" in spec else {}),
                }
                for name, spec in self.value_specs.items()
            }

    def read_history(
        self, name: str, *, days: int = 1,
        include_points: bool = False, max_points: int = 200,
    ) -> dict[str, Any]:
        if name not in self.history_specs:
            raise ValueError("Unknown or unauthorized history")
        spec = self.history_specs[name]
        if type(days) is not int or days < 1 or days > int(spec.get("max_days", 30)):
            raise ValueError("Requested history range is not permitted")
        if type(max_points) is not int or not 1 <= max_points <= 1000:
            raise ValueError("max_points must be between 1 and 1000")
        alias = spec["from"]
        with self._read_lock:
            points = []
            if spec.get("mode", "per_day") == "range":
                value = self.valueProvider.getMethodValue(alias, days=days)
                points.extend(series_points(value, day=days))
            else:
                # PersistData.hist(days=N, fullDay=True) fetches ONE day at
                # offset N, not an entire N-day range. Iterate explicitly.
                for offset in range(days):
                    value = self.valueProvider.getMethodValue(
                        alias, days=offset, fullDay=True
                    )
                    points.extend(series_points(value, day=offset))
        points.sort(key=lambda p: str(p["time"]))
        result = {
            "name": name,
            "description": spec["description"],
            "days": days,
            "statistics": summarize_points(points),
        }
        if "unit" in spec:
            result["unit"] = spec["unit"]
        if include_points:
            result["points"] = bounded_sample(points, max_points)
            result["sampled"] = len(points) > max_points
            result["total_points"] = len(points)
        return result

    def execute_action(self, name: str, arguments: dict[str, Any]) -> Any:
        if name not in self.action_specs:
            raise ValueError("Unknown or unauthorized action")
        if not isinstance(arguments, dict):
            raise ValueError("Action arguments must be an object")
        if len(json.dumps(arguments, ensure_ascii=False).encode("utf-8")) > 16_384:
            raise ValueError("Action arguments too large")
        spec = self.action_specs[name]
        Draft202012Validator(spec["parameters"]).validate(arguments)
        with self._read_lock:
            result = self.valueProvider.getMethodValue(spec["from"], **arguments)
        return json_safe(result)

    def getTemplate(self, value):
        return "ai/mcp.html"

    def getDisplayData(self, value, actionHash, **kwargs):
        return MCPDisplayData(self.ctx, actionHash, self)

    def shutdown(self):
        if self.runtime:
            self.runtime.stop()
            self.runtime = None
