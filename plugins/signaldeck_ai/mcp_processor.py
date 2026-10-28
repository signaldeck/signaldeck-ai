"""MCP exposure configuration layered over native SignalDeck ValueProvider mappings."""
from __future__ import annotations

import json
import re
from datetime import datetime, time, timedelta, timezone
from flask import request
from jsonschema import ValidationError
from threading import RLock
from typing import Any

from jsonschema import Draft202012Validator
from signaldeck_sdk import DisplayProcessor, DisplayData

from .mcp_server import MCPRuntime
from .run_actions import RunActions
from .serialization import bounded_sample, json_safe, merge_history_points, series_points, summarize_points


_AWARE_TIMESTAMP_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d"
    r"(?:\.\d{1,9})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)\Z"
)


def _parse_instant(value: str, parameter: str) -> datetime:
    """Require an unambiguous RFC 3339 instant; never assume the server timezone."""
    if not isinstance(value, str) or _AWARE_TIMESTAMP_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{parameter} must be an ISO 8601 timestamp with Z or an explicit UTC offset")
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{parameter} must be a valid timezone-aware timestamp") from exc
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError(f"{parameter} must include a UTC offset")
    return instant.astimezone(timezone.utc)


def _point_instant(point: dict[str, Any]) -> datetime:
    if point.get("time_inferred"):
        raise ValueError("Absolute history windows require actual measurement timestamps")
    # The server may return different offsets on opposite sides of a DST change.
    return _parse_instant(point["time"], "History measurement time")


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
        self, name: str, *, start: str, end: str,
        include_points: bool = False, max_points: int | None = 200,
    ) -> dict[str, Any]:
        """Read the absolute half-open interval [start, end), irrespective of server TZ.

        PersistData.hist supports full UTC calendar days when passed aware UTC
        midnights. Query the intersecting days, then filter exact instants.
        """
        if name not in self.history_specs:
            raise ValueError("Unknown or unauthorized data row")
        spec = self.history_specs[name]
        start_utc = _parse_instant(start, "start")
        end_utc = _parse_instant(end, "end")
        if end_utc <= start_utc:
            raise ValueError("end must be after start")
        if end_utc - start_utc > timedelta(days=spec.get("max_days", 30)):
            raise ValueError("Requested history range exceeds max_days")
        now_utc = datetime.now(timezone.utc)
        if start_utc > now_utc:
            raise ValueError("Future history starts are not permitted")
        if max_points is not None and (type(max_points) is not int or not 1 <= max_points <= 1000):
            raise ValueError("max_points must be between 1 and 1000")
        first_day = start_utc.date()
        last_day = (end_utc - timedelta(microseconds=1)).date()
        day_count = (last_day - first_day).days + 1

        binding = self._method_bindings[name]
        provider_method = binding["from"]
        with self._read_lock:
            points = []
            if spec.get("mode", "per_day") == "range":
                # Custom range methods accept a timezone-aware UTC start date
                # and count of UTC days; the requested bounds are filtered below.
                result = self.valueProvider.getMethodValue(
                    provider_method,
                    date=datetime.combine(first_day, time.min, tzinfo=timezone.utc),
                    days=day_count,
                )
                points.extend(series_points(result, day=0, allow_scalar=False))
            else:
                for offset in range(day_count):
                    requested_day = first_day + timedelta(days=offset)
                    result = self.valueProvider.getMethodValue(
                        provider_method,
                        date=datetime.combine(requested_day, time.min, tzinfo=timezone.utc),
                        days=0, fullDay=True,
                    )
                    points.extend(series_points(result, day=0, allow_scalar=False))

            # Read the live buffer for recent windows. Filter by absolute
            # timestamps, so cached samples in a different local calendar day
            # cannot contaminate the result.
            if (spec.get("mode", "per_day") == "per_day"
                    and end_utc > now_utc - timedelta(days=1)):
                try:
                    cached = self.valueProvider.getMethodValue(
                        provider_method, currentValues=True,
                    )
                    cache_points = series_points(
                        cached, day=0, allow_scalar=False,
                    )
                    # The optional cache must never invalidate otherwise
                    # usable persisted history. Validate every cached instant
                    # here, even if that sample is outside the requested range.
                    for point in cache_points:
                        _point_instant(point)
                except (ValueError, TypeError, KeyError, AttributeError) as exc:
                    # Optional cache support must not break a valid persistent
                    # history read (e.g. DummyData returns a scalar).
                    self.logger.debug(
                        "No compatible timestamped history cache for %s: %s",
                        name, exc,
                    )
                else:
                    points = merge_history_points(points, cache_points)

        # Deduplicate inclusive UTC day boundaries, retain source offsets and
        # order by actual instants (lexicographic ISO order fails at DST fall-back).
        points = merge_history_points(points, [])
        points = [point for point in points if start_utc <= _point_instant(point) < end_utc]
        points.sort(key=_point_instant)
        output = {
            "name": name,
            "description": spec["description"],
            "start": start,
            "end": end,
            "statistics": summarize_points(points),
        }
        if "unit" in spec:
            output["unit"] = spec["unit"]
        if include_points:
            # Only the MCP adapter requests max_points=None. It must publish
            # all measurements so workspace-based analysis is deterministic.
            output["points"] = points if max_points is None else bounded_sample(points, max_points)
            output["sampled"] = max_points is not None and len(points) > max_points
            output["total_points"] = len(points)
        return output

    def read_historical_value(self, name: str, *, date: str) -> dict[str, Any]:
        """Look up one historical value at a timezone-aware instant.

        PersistData.hist(fullDay=False) selects the datastore's best
        historical value for the supplied instant; it does not guarantee
        a measurement with precisely that timestamp.
        """
        if name not in self.history_specs:
            raise ValueError("Unknown or unauthorized data row")
        spec = self.history_specs[name]
        if spec.get("mode", "per_day") != "per_day":
            raise ValueError("Point-in-time lookup requires a per-day history method")
        requested = _parse_instant(date, "date")
        if requested > datetime.now(timezone.utc):
            raise ValueError("Future history dates are not permitted")

        provider_method = self._method_bindings[name]["from"]
        with self._read_lock:
            result = self.valueProvider.getMethodValue(
                provider_method, date=requested, days=0, fullDay=False,
            )
        output = {
            "name": name,
            "description": spec["description"],
            "requested_time": date,
            "value": json_safe(result),
        }
        if "unit" in spec:
            output["unit"] = spec["unit"]
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
                     "start": (datetime.now(timezone.utc) - timedelta(hours=2)).replace(microsecond=0).isoformat(),
                     "end": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                     "include_points": True}
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
        if tool_name == "get_history":
            # FastMCP already returns the JSON-compatible structured result.
            # History is separately size-limited by the MCP adapter, whereas
            # json_safe would reject valid results with over 10,000 points.
            return {"tool": tool_name, "result": result}
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
