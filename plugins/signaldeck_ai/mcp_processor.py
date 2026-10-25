"""MCP exposure configuration layered over native SignalDeck ValueProvider mappings."""
from __future__ import annotations

import json
from datetime import date as calendar_date, datetime, time, timedelta
from flask import request
from jsonschema import ValidationError
from threading import RLock
from typing import Any

from jsonschema import Draft202012Validator
from signaldeck_sdk import DisplayProcessor, DisplayData

from .mcp_server import MCPRuntime
from .run_actions import RunActions
from .serialization import bounded_sample, json_safe, merge_history_points, series_points, summarize_points


class MCPDisplayData(DisplayData):
    def __init__(self, ctx, action_hash, processor):
        super().__init__(ctx, action_hash)
        self.processor = processor
        self.name = processor.name
        self.status = processor.runtime.status if processor.runtime else "pending"
        self.url = processor.runtime.url if processor.runtime else ""
        self.instructions = (processor.config.get("mcp", {}) or {}).get("instructions", "")
        self.tools = processor.registered_tool_catalog()


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
        self.run_actions = RunActions(mcp_data.get("runs", []))
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
        if self.run_actions.specs:
            result.append({"name": "run", "description": "Start a permitted named action"})
        return result

    def registerCommands(self, cmd):
        self.run_actions.bind(cmd)

    def run_named_action(self, name: str, arguments: dict[str, Any] | None = None):
        return self.run_actions.execute(name, arguments)

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
        self, name: str, *, date: str, days: int = 1,
        include_points: bool = False, max_points: int = 200,
    ) -> dict[str, Any]:
        """Read a date-anchored span, starting on the requested calendar day.

        PersistData.hist subtracts `days` from `date`: consequently each
        individual day must be queried with days=0 and fullDay=True.
        The days parameter of *this* method is instead a window length.
        """
        if name not in self.history_specs:
            raise ValueError("Unknown or unauthorized data row")
        spec = self.history_specs[name]
        if not isinstance(date, str) or len(date) != 10:
            raise ValueError("date must be a calendar date in YYYY-MM-DD format")
        try:
            target_day = calendar_date.fromisoformat(date)
        except ValueError as exc:
            raise ValueError("date must be a valid calendar date in YYYY-MM-DD format") from exc
        if target_day.isoformat() != date:
            raise ValueError("date must be a calendar date in YYYY-MM-DD format")
        if type(days) is not int or not 1 <= days <= spec.get("max_days", 30):
            raise ValueError("Requested history range is not permitted")
        if type(max_points) is not int or not 1 <= max_points <= 1000:
            raise ValueError("max_points must be between 1 and 1000")
        try:
            end_day = target_day + timedelta(days=days - 1)
        except OverflowError as exc:
            raise ValueError("Requested history range is outside valid dates") from exc
        # No day in the requested interval may lie in the future.
        if end_day > datetime.now().date():
            raise ValueError("Future history dates are not permitted")

        binding = self._method_bindings[name]
        provider_method = binding["from"]
        with self._read_lock:
            points = []
            if spec.get("mode", "per_day") == "range":
                # Custom range methods (unlike PersistData.hist) must accept
                # the explicit inclusive start date and window length, and
                # return timestamped records. They never receive an offset.
                result = self.valueProvider.getMethodValue(
                    provider_method,
                    date=datetime.combine(target_day, time.min), days=days,
                )
                points.extend(series_points(result, day=0, allow_scalar=False))
            else:
                for offset in range(days):
                    requested_day = target_day + timedelta(days=offset)
                    result = self.valueProvider.getMethodValue(
                        provider_method,
                        date=datetime.combine(requested_day, time.min),
                        days=0, fullDay=True,
                    )
                    points.extend(series_points(
                        result, day=0, sample_date=requested_day,
                    ))

            # Only the current calendar day can contain measurements that
            # have not yet reached the persistent store. PersistData.hist
            # exposes its in-memory deque via currentValues=True.
            if spec.get("mode", "per_day") == "per_day" and target_day <= datetime.now().date() <= end_day:
                try:
                    cached = self.valueProvider.getMethodValue(
                        provider_method, currentValues=True,
                    )
                    cache_points = series_points(
                        cached, day=0, allow_scalar=False,
                    )
                except (ValueError, TypeError, KeyError, AttributeError) as exc:
                    # Optional cache support must not break a valid persistent
                    # history read (e.g. DummyData returns a scalar).
                    self.logger.debug(
                        "No compatible timestamped history cache for %s: %s",
                        name, exc,
                    )
                else:
                    today = datetime.now().date()
                    cache_points = [
                        point for point in cache_points
                        if datetime.fromisoformat(point["time"]).date() == today
                    ]
                    points = merge_history_points(points, cache_points)

        # Keep source measurement timestamps unchanged. Scalar values without
        # timestamps are explicitly marked as inferred at the requested date.
        points.sort(key=lambda row: row["time"])
        output = {
            "name": name,
            "description": spec["description"],
            "date": date,
            "start_date": date,
            "end_date": end_day.isoformat(),
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

    def registered_tool_catalog(self) -> list[dict[str, Any]]:
        if self.runtime is None:
            return []
        tools = self.runtime.tool_catalog()
        for tool in tools:
            name = tool["name"]
            if name == "get_values":
                keys = list(self.value_specs)
                tool["example_args"] = {"fields": keys[:1]} if keys else {}
            elif name == "get_history":
                keys = list(self.history_specs)
                tool["example_args"] = (
                    {"name": keys[0],
                     "date": (datetime.now() - timedelta(days=1)).date().isoformat(),
                     "days": 2, "include_points": True}
                    if keys else {}
                )
            elif name == "run":
                keys = list(self.run_actions.specs)
                tool["example_args"] = {"name": keys[0], "arguments": {}} if keys else {}
            else:
                # Never prepopulate write parameters with potentially unsafe
                # commands or values. Explicit manual entry is required.
                tool["example_args"] = {}
        return tools

    def process(self, value, actionHash, file=None, **params):
        """Use the standard SignalDeck action dispatcher, without rerendering.

        The independent diagnostic UI stays mounted while results are shown.
        """
        if request.method != "POST":
            return {"ok": False, "error": "Nur POST-Anfragen sind erlaubt."}
        if request.headers.get("Origin") != f"{request.scheme}://{request.host}":
            return {"ok": False, "error": "Same-Origin-Anfrage erforderlich."}
        if file is not None:
            return {"ok": False, "error": "Dateiupload nicht erlaubt."}
        try:
            return {"ok": True, **self.performActions(value, actionHash, **params)}
        except (ValueError, TypeError, ValidationError) as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            self.logger.exception("MCP diagnostic tool execution failed")
            return {"ok": False, "error": "Fehler beim Aufruf des MCP-Tools."}

    def performActions(self, value, actionHash, **kwargs):
        if kwargs.get("mcp_action") != "execute_tool":
            raise ValueError("Unbekannte MCP-Diagnoseaktion.")
        if self.runtime is None:
            raise ValueError("MCP-Server nicht initialisiert.")
        tool_name = kwargs.get("tool_name")
        payload = kwargs.get("arguments", "{}")
        if not isinstance(tool_name, str) or not isinstance(payload, str):
            raise ValueError("Toolname und JSON-Argumente erforderlich.")
        if len(payload.encode("utf-8")) > 16_384:
            raise ValueError("Tool-Argumente überschreiten 16 KiB.")
        arguments = json.loads(payload)
        if not isinstance(arguments, dict):
            raise ValueError("Die Argumente müssen ein JSON-Objekt sein.")
        available = {tool["name"]: tool for tool in self.registered_tool_catalog()}
        if tool_name not in available:
            raise ValueError("Unbekanntes oder nicht freigegebenes MCP-Tool.")
        if available[tool_name]["is_action"]:
            if kwargs.get("confirm_write") != "true":
                raise ValueError("Schreibaktion muss ausdrücklich bestätigt werden.")
        result = self.runtime.call_registered_tool(tool_name, arguments)
        return {"tool": tool_name, "result": json_safe(result)}

    def getAdditionalJsFiles(self, value):
        return [("signaldeck_ai", "js/mcp_console.js")]

    def getAdditionalCssFiles(self, value):
        return [("signaldeck_ai", "css/mcp_console.css")]

    def getJS_functions_to_call_on_client(self, data):
        return {f"signaldeck-ai-mcp-{data.hash}": "initSignalDeckMCPConsole"}

    def getTemplate(self, value):
        return "ai/mcp.html"

    def getDisplayData(self, value, actionHash, **kwargs):
        return MCPDisplayData(self.ctx, actionHash, self)

    def shutdown(self):
        if self.runtime:
            self.runtime.stop()
            self.runtime = None
