"""Safe, explicitly published aliases for SignalDeck Cmd commands and scripts.

MCP clients only see a named action, description, and permitted parameter
schema. They never submit a Cmd command line, Python method, or script source.
"""
from __future__ import annotations

import json
import math
import re
import uuid
from concurrent.futures import Future
from typing import Any

from jsonschema import Draft202012Validator

# Cmd splits its command line on spaces and the script interpreter interpolates
# variables into text. Restrict dynamic string inputs to literal safe tokens.
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_./:@+-]{1,128}$")
# Free text is opt-in, only for the final command argument. Cmd splits the
# command line at spaces, so the recipient command must be designed to gather
# the trailing words (e.g. socketio_send <room> <message...>).
MAX_FREE_TEXT_LENGTH = 500
_EMPTY_SCHEMA = {
    "type": "object", "properties": {},
    "required": [], "additionalProperties": False,
}


def _primitive(value: Any) -> bool:
    return type(value) in (str, int, float, bool)


def _validate_parameter_schema(schema: Any, location: str) -> set[str]:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ValueError(f"{location}: parameters must be an object schema")
    if schema.get("additionalProperties") is not False:
        raise ValueError(f"{location}: additionalProperties must be false")
    Draft202012Validator.check_schema(schema)
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    if not isinstance(properties, dict) or not isinstance(required, list):
        raise ValueError(f"{location}: properties and required must be provided")
    if set(required) != set(properties):
        raise ValueError(f"{location}: every exposed argument must be required")

    for key, definition in properties.items():
        if not isinstance(key, str) or not key or not isinstance(definition, dict):
            raise ValueError(f"{location}: invalid parameter")
        kind = definition.get("type")
        if kind not in {"string", "number", "integer", "boolean"}:
            raise ValueError(f"{location}.{key}: primitive parameter type required")
        # Refuse complex schemas: keep the exposed surface easy to audit.
        permitted = {"type", "description", "enum"}
        if kind == "string":
            permitted |= {"minLength", "maxLength"}
        elif kind in {"number", "integer"}:
            permitted |= {"minimum", "maximum"}
        if set(definition) - permitted:
            raise ValueError(f"{location}.{key}: unsupported schema constraints")
        if kind == "string":
            choices = definition.get("enum")
            if choices is None:
                limit = definition.get("maxLength")
                minimum = definition.get("minLength", 1)
                if (type(limit) is not int or not 1 <= limit <= MAX_FREE_TEXT_LENGTH
                        or type(minimum) is not int or not 1 <= minimum <= limit):
                    raise ValueError(
                        f"{location}.{key}: free text needs maxLength "
                        f"1..{MAX_FREE_TEXT_LENGTH} and optional minLength >= 1"
                    )
            else:
                if not isinstance(choices, list) or not 1 <= len(choices) <= 64:
                    raise ValueError(f"{location}.{key}: string must have a bounded enum")
                if any(type(choice) is not str or not _SAFE_TOKEN.fullmatch(choice)
                       for choice in choices):
                    raise ValueError(f"{location}.{key}: string enum must use safe tokens")
                for bound in ("minLength", "maxLength"):
                    if bound in definition and (
                        type(definition[bound]) is not int
                        or definition[bound] < 0
                    ):
                        raise ValueError(f"{location}.{key}: invalid {bound}")
                if ("minLength" in definition and "maxLength" in definition
                        and definition["minLength"] > definition["maxLength"]):
                    raise ValueError(f"{location}.{key}: minLength exceeds maxLength")
        elif kind in {"number", "integer"}:
            choices = definition.get("enum")
            if choices is None:
                lower, upper = definition.get("minimum"), definition.get("maximum")
                if (not isinstance(lower, (int, float))
                        or not isinstance(upper, (int, float))
                        or isinstance(lower, bool) or isinstance(upper, bool)
                        or not math.isfinite(lower) or not math.isfinite(upper)
                        or lower > upper):
                    raise ValueError(
                        f"{location}.{key}: numeric parameters need finite bounds or enum"
                    )
            elif not isinstance(choices, list) or not 1 <= len(choices) <= 64:
                raise ValueError(f"{location}.{key}: numeric enum required")
            if choices is not None and any(
                type(choice) not in ((int,) if kind == "integer" else (int, float))
                or not math.isfinite(choice) for choice in choices
            ):
                raise ValueError(f"{location}.{key}: invalid numeric enum")
        elif "enum" in definition:
            if definition["enum"] not in ([True], [False], [True, False], [False, True]):
                raise ValueError(f"{location}.{key}: invalid boolean enum")
    return set(properties)


def _validate_token_spec(token: Any, params: set[str], location: str) -> set[str]:
    if isinstance(token, dict):
        if set(token) != {"param"} or token["param"] not in params:
            raise ValueError(f"{location}: unknown parameter reference")
        return {token["param"]}
    if not _primitive(token):
        raise ValueError(f"{location}: only primitive literals or parameter references")
    if type(token) is str and not _SAFE_TOKEN.fullmatch(token):
        raise ValueError(f"{location}: literal command tokens must be safe single tokens")
    if type(token) is float and not math.isfinite(token):
        raise ValueError(f"{location}: non-finite literal")
    return set()


def _validate_free_text(value: str) -> None:
    """Reject controls, Unicode format marks and ambiguous surrounding space.

    Free text can contain spaces, punctuation and normal Unicode. It cannot
    add new ScriptParser statements, control chars or terminal escapes.
    """
    if (type(value) is not str or not value
            or value != value.strip() or not value.isprintable()):
        raise ValueError(
            "Free text must be nonempty printable text without leading/trailing "
            "whitespace, line breaks, control or format characters"
        )


def _token_to_string(value: Any) -> str:
    if type(value) is bool:
        return "true" if value else "false"
    return str(value)


class RunActions:
    """An opt-in registry of named Cmd and script invocations."""

    def __init__(self, specs: Any):
        if not isinstance(specs, list):
            raise ValueError("mcp_data.runs must be a list")
        self.specs: dict[str, dict[str, Any]] = {}
        self.cmd: Any = None
        for item in specs:
            if not isinstance(item, dict):
                raise ValueError("mcp_data.runs entries must be objects")
            name, description = item.get("name"), item.get("description")
            if not isinstance(name, str) or not name or name in self.specs:
                raise ValueError("mcp_data.runs: missing or duplicate name")
            if not isinstance(description, str) or not description.strip():
                raise ValueError(f"mcp_data.runs.{name}: description required")
            location = f"mcp_data.runs.{name}"
            if ("command" in item) == ("script" in item):
                raise ValueError(f"{location}: specify exactly one command or script")
            target = item.get("command", item.get("script"))
            if not isinstance(target, str) or not target.strip():
                raise ValueError(f"{location}: nonempty command or script required")

            schema = item.get("parameters", _EMPTY_SCHEMA)
            param_names = _validate_parameter_schema(schema, location)
            used: set[str] = set()
            if "command" in item:
                if set(item) - {"name", "description", "command", "args", "parameters"}:
                    raise ValueError(f"{location}: unknown command configuration keys")
                args = item.get("args", [])
                if not isinstance(args, list) or len(args) > 32:
                    raise ValueError(f"{location}: args must be an array of <=32 tokens")
                for idx, token in enumerate(args):
                    used |= _validate_token_spec(token, param_names, f"{location}.args[{idx}]")
            else:
                if set(item) - {"name", "description", "script", "variables", "parameters"}:
                    raise ValueError(f"{location}: unknown script configuration keys")
                variables = item.get("variables", {})
                if not isinstance(variables, dict) or len(variables) > 32:
                    raise ValueError(f"{location}: variables must be an object of <=32 entries")
                for key, token in variables.items():
                    if not isinstance(key, str) or not key:
                        raise ValueError(f"{location}: invalid variable")
                    used |= _validate_token_spec(token, param_names, f"{location}.variables.{key}")
            free_text_params = {
                key for key, definition in schema.get("properties", {}).items()
                if definition.get("type") == "string" and "enum" not in definition
            }
            if free_text_params:
                if "script" in item:
                    # Script variables are interpolated into arbitrary command
                    # positions. Do not allow free text in this path.
                    raise ValueError(
                        f"{location}: free text is supported only for Cmd commands"
                    )
                for index, token in enumerate(args):
                    if (isinstance(token, dict)
                            and token.get("param") in free_text_params
                            and index != len(args) - 1):
                        raise ValueError(
                            f"{location}: free text must be the final Cmd argument"
                        )
            if used != param_names:
                raise ValueError(f"{location}: parameters must map to all declared arguments")
            self.specs[name] = {**item, "parameters": schema}

    def bind(self, cmd: Any) -> None:
        """Called by SignalDeck after Cmd and configured scripts are loaded."""
        for name, spec in self.specs.items():
            if "command" in spec:
                target = spec["command"]
                if target not in cmd.commands:
                    raise ValueError(f"mcp_data.runs.{name}: unknown Cmd command")
                if target in cmd.alias:
                    raise ValueError(
                        f"mcp_data.runs.{name}: command shadowed by a Cmd alias"
                    )
            else:
                script = cmd.getScript(spec["script"])
                if script is None:
                    raise ValueError(f"mcp_data.runs.{name}: unknown Cmd script")
                defined = {variable.name for variable in script.variables}
                if set(spec.get("variables", {})) - defined:
                    raise ValueError(
                        f"mcp_data.runs.{name}: unknown script variable"
                    )
        self.cmd = cmd

    def descriptions(self) -> list[dict[str, Any]]:
        # Deliberately expose neither command names nor script names.
        return [
            {
                "name": name, "description": spec["description"],
                "parameters": spec["parameters"],
            }
            for name, spec in self.specs.items()
        ]

    def execute(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, str]:
        if name not in self.specs:
            raise ValueError("Unknown or unauthorized named run action")
        if self.cmd is None:
            raise RuntimeError("SignalDeck Cmd is not registered")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ValueError("Run arguments must be a JSON object")
        if len(json.dumps(arguments, ensure_ascii=False).encode("utf-8")) > 4096:
            raise ValueError("Run arguments are too large")
        spec = self.specs[name]
        Draft202012Validator(spec["parameters"]).validate(arguments)
        if any(type(value) is float and not math.isfinite(value)
               for value in arguments.values()):
            raise ValueError("Non-finite run arguments are not allowed")
        for key, definition in spec["parameters"].get("properties", {}).items():
            if definition.get("type") == "string" and "enum" not in definition:
                _validate_free_text(arguments[key])

        def resolve(value):
            return arguments[value["param"]] if isinstance(value, dict) else value

        if "command" in spec:
            target = spec["command"]
            if target not in self.cmd.commands or target in self.cmd.alias:
                raise ValueError("Configured Cmd command is no longer available")
            tokens = [target] + [
                _token_to_string(resolve(value)) for value in spec.get("args", [])
            ]
            future = self.cmd.run(
                [" ".join(tokens)], name="mcp-run-" + uuid.uuid4().hex
            )
        else:
            script_name = spec["script"]
            if self.cmd.getScript(script_name) is None:
                raise ValueError("Configured Cmd script is no longer available")
            # Cmd stores task/current/stop state by script name; avoid
            # overlapping runs overwriting these references.
            previous = self.cmd.tasks.get(script_name)
            if previous is not None and not previous.done():
                raise ValueError("The selected script is already running")
            variables = {
                key: resolve(value)
                for key, value in spec.get("variables", {}).items()
            }
            future = self.cmd.runScript(script_name, **variables)

        if future is not None and future.done():
            # Report immediate scheduler/parser failure if already available.
            future.result()
        return {"name": name, "status": "scheduled"}
