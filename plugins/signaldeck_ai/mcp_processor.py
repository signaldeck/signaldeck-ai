"""MCP exposure configuration layered over native SignalDeck ValueProvider mappings."""
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
    """Publish only explicitly listed config.mcp_data entries.

    config.values and config.methods keep their standard SignalDeck SDK format
    (name/type/from/args/params). config.mcp_data is a *separate* allowlist
    carrying LLM-facing descriptions. Its 'field' and 'func' keys reference
    the local names from those standard mappings, not provider export names.
    """

    def __init__(self, name, config, ctx, valueProvider, collect_data):
        super().__init__(name, config, ctx, valueProvider, collect_data)
        self._read_lock = RLock()

        self._value_bindings = self._bindings("values")
        self._method_bindings = self._bindings("methods")
        mcp_data = self.config.get("mcp_data", {})
        if not isinstance(mcp_data, dict):
            raise ValueError("mcp_data must be an object")

        self.value_specs = self._exposures(
            "values", mcp_data.get("values", []), "field", self._value_bindings
        )
        self.history_specs = self._exposures(
            "data_rows", mcp_data.get("data_rows", []), "func", self._method_bindings
        )
        # Actions are optional; they follow the same explicit allowlist
        # principle, without embedding MCP metadata in config.methods.
        self.action_specs = self._exposures(
            "actions", mcp_data.get("actions", []), "func", self._method_bindings
        )
        self._validate_exposures()
        self.runtime: MCPRuntime | None = None

    def _bindings(self, key: str) -> dict[str, dict[str, Any]]:
        mappings = self.config.get(key, [])
        if not isinstance(mappings, list):
            raise ValueError(f"{key} must be a list")
        result = {}
        for raw in mappings:
            if not isinstance(raw, dict):
                raise ValueError(f"{key}: each entry must be an object")
            name, source = raw.get("name"), raw.get("from")
            if not isinstance(name, str) or not name or name in result:
                raise ValueError(f"{key}: missing or duplicate local name")
            if not isinstance(source, str) or not source:
                raise ValueError(f"{key}.{name}: nonempty 'from' required")
            if key == "values" and raw.get("type") not in {"field", "method"}:
                raise ValueError(f"{key}.{name}: type must be 'field' or 'method'")
            result[name] = dict(raw)
        return result

    @staticmethod
    def _exposures(
        group: str,
        items: Any,
        identifier_key: str,
        allowed: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        if not isinstance(items, list):
            raise ValueError(f"mcp_data.{group} must be a list")
        result = {}
        for item in items:
            if not isinstance(item, dict):
                raise ValueError(f"mcp_data.{group}: each entry must be an object")
            identifier, description = item.get(identifier_key), item.get("description")
            if not isinstance(identifier, str) or not identifier or identifier in result:
                raise ValueError(f"mcp_data.{group}: missing or duplicate '{identifier_key}'")
            if identifier not in allowed:
                raise ValueError(
                    f"mcp_data.{group}.{identifier} references an unknown "
                    f"config.{ 'values' if identifier_key == 'field' else 'methods' } name"
                )
            if not isinstance(description, str) or not description.strip():
                raise ValueError(f"mcp_data.{group}.{identifier}: description required")
            # Preserve original MCP metadata while presenting a normalized
            # 'name' to the FastMCP tool descriptions and handlers.
            result[identifier] = {**item, "name": identifier}
        return result

    def _validate_exposures(self) -> None:
        for name, spec in self.history_specs.items():
            max_days = spec.get("max_days", 30)
            if type(max_days) is not int or not 1 <= max_days <= 366:
                raise ValueError(f"mcp_data.data_rows.{name}: max_days must be 1..366")
            if spec.get("mode", "per_day") not in {"per_day", "range"}:
                raise ValueError(f"mcp_data.data_rows.{name}: invalid mode")
        for name, spec in self.action_specs.items():
            schema = spec.get("parameters")
            if not isinstance(schema, dict) or schema.get("type") != "object":
                raise ValueError(f"mcp_data.actions.{name}: object parameters required")
            if schema.get("additionalProperties") is not False:
                raise ValueError(
                    f"mcp_data.actions.{name}: parameters must forbid additionalProperties"
                )
            Draft202012Validator.check_schema(schema)

    def tool_descriptions(self) -> list[dict[str, Any]]:
        result = []
        if self.value_specs:
            result.append({"name": "get_values", "description": "Read permitted current values"})
        if self.history_specs:
            result.append({"name": "get_history", "description": "Read permitted data rows"})
        if self.action_specs:
            result.append({"name": "execute_action", "description": "Run permitted actions"})
        return result

    def read_values(self, fields: list[str] | None = None) -> dict[str, Any]:
        """Read selected MCP-published values (or all if fields is omitted).

        Validate all requested local MCP names before accessing ValueProvider.
        """
        if fields is None:
            selected = list(self.value_specs)
        else:
            if not isinstance(fields, list):
                raise ValueError("fields must be an array of published MCP field names")
            if len(fields) > 256:
                raise ValueError("fields must contain at most 256 names")
            selected = []
            seen = set()
            for name in fields:
                if not isinstance(name, str) or name not in self.value_specs:
                    raise ValueError("Unknown or unauthorized MCP field")
                if name not in seen:
                    selected.append(name)
                    seen.add(name)

        with self._read_lock:
            # Respect native 'field'/'method' bindings, but never call
            # refresh() here: refresh() also invokes *unlisted* methods in
            # config.values and binds all config.methods. An MCP request
            # must evaluate only explicitly allowlisted sources.
            result = {}
            for name in selected:
                spec = self.value_specs[name]
                binding = self._value_bindings[name]
                if binding["type"] == "field":
                    value = self.valueProvider.getValue(binding["from"])
                else:
                    value = self.valueProvider.getMethodValue(
                        binding["from"],
                        *binding.get("args", []),
                        **binding.get("params", {}),
                    )
                result[name] = {
                    "description": spec["description"],
                    "value": json_safe(value),
                    **({"unit": spec["unit"]} if "unit" in spec else {}),
                }
            return result

    def read_history(
        self, name: str, *, days: int = 1,
        include_points: bool = False, max_points: int = 200,
    ) -> dict[str, Any]:
        if name not in self.history_specs:
            raise ValueError("Unknown or unauthorized data row")
        spec = self.history_specs[name]
        if type(days) is not int or not 1 <= days <= spec.get("max_days", 30):
            raise ValueError("Requested history range is not permitted")
        if type(max_points) is not int or not 1 <= max_points <= 1000:
            raise ValueError("max_points must be between 1 and 1000")

        # config.methods maps the local 'func' to the exported ValueProvider
        # method, as elsewhere in SignalDeck.
        binding = self._method_bindings[name]
        provider_method = binding["from"]
        with self._read_lock:
            points = []
            if spec.get("mode", "per_day") == "range":
                result = self.valueProvider.getMethodValue(provider_method, days=days)
                points.extend(series_points(result, day=days))
            else:
                # PersistData.hist(days=N, fullDay=True) returns one day at
                # offset N; iterate to collect a complete window.
                for offset in range(days):
                    result = self.valueProvider.getMethodValue(
                        provider_method, days=offset, fullDay=True
                    )
                    points.extend(series_points(result, day=offset))
        points.sort(key=lambda row: str(row["time"]))
        output = {
            "name": name,
            "description": spec["description"],
            "days": days,
            "statistics": summarize_points(points),
        }
        if "unit" in spec:
            output["unit"] = spec["unit"]
        if include_points:
            output["points"] = bounded_sample(points, max_points)
            output["sampled"] = len(points) > max_points
            output["total_points"] = len(points)
        return output

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
            result = self.valueProvider.getMethodValue(
                self._method_bindings[name]["from"], **arguments
            )
        return json_safe(result)

    def getTemplate(self, value):
        return "ai/mcp.html"

    def getDisplayData(self, value, actionHash, **kwargs):
        return MCPDisplayData(self.ctx, actionHash, self)

    def shutdown(self):
        if self.runtime:
            self.runtime.stop()
            self.runtime = None
