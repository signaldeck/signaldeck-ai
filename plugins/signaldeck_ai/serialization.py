"""Conservative JSON conversion for ValueProvider results."""
from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any


def json_safe(value: Any, *, depth: int = 0) -> Any:
    """Convert typical SignalDeck/Pandas values without leaking object internals."""
    if depth > 12:
        raise ValueError("Nested data exceeds the supported depth")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if hasattr(value, "item"):  # numpy scalar
        return json_safe(value.item(), depth=depth + 1)
    if hasattr(value, "to_pydatetime"):  # pandas Timestamp
        return json_safe(value.to_pydatetime(), depth=depth + 1)
    if isinstance(value, (list, tuple)):
        if len(value) > 10000:
            raise ValueError("Value list too large")
        return [json_safe(v, depth=depth + 1) for v in value]
    if isinstance(value, dict):
        if len(value) > 10000:
            raise ValueError("Value dictionary too large")
        return {str(k): json_safe(v, depth=depth + 1) for k, v in value.items()}
    raise TypeError(f"Unsupported ValueProvider result type: {type(value).__name__}")


def series_points(value: Any, *, day: int) -> list[dict[str, Any]]:
    """Turn a history result into timestamp/value pairs.

    SignalDeck PersistData.hist(..., fullDay=True) typically returns a
    pandas Series. Other method exports may return a scalar or sequence.
    """
    if value is None:
        return []
    if hasattr(value, "items"):  # pandas Series or mapping
        return [
            {"time": json_safe(timestamp), "value": json_safe(val)}
            for timestamp, val in value.items()
        ]
    if isinstance(value, (list, tuple)):
        return [
            {"time": f"day-{day}:index-{index}", "value": json_safe(item)}
            for index, item in enumerate(value)
        ]
    return [{"time": f"day-{day}", "value": json_safe(value)}]


def summarize_points(points: list[dict[str, Any]]) -> dict[str, Any]:
    numeric = [
        p["value"] for p in points
        if isinstance(p["value"], (float, int)) and not isinstance(p["value"], bool)
    ]
    result: dict[str, Any] = {"count": len(points), "numeric_count": len(numeric)}
    if numeric:
        result.update(
            minimum=min(numeric),
            maximum=max(numeric),
            mean=sum(numeric) / len(numeric),
        )
    if points:
        result.update(first_time=points[0]["time"], last_time=points[-1]["time"])
    return result


def bounded_sample(points: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Even sampling for display only; summary always uses complete history."""
    if len(points) <= limit:
        return points
    if limit == 1:
        return [points[-1]]
    return [points[(i * (len(points) - 1)) // (limit - 1)] for i in range(limit)]
