"""Conservative JSON conversion for ValueProvider results."""
from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta, timezone
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


def _timestamp(value: Any) -> tuple[str, bool]:
    """Normalize a real date/time to ISO 8601; flag date-only inferred midnight.

    A timestamp without a timezone retains that characteristic: assigning
    UTC or local time to an originally naive timestamp would misrepresent it.
    """
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()  # pandas.Timestamp, datetime-compatible
    if isinstance(value, datetime):
        if str(value) == "NaT":
            raise ValueError("History contains a missing timestamp (NaT)")
        return value.isoformat(), False
    if isinstance(value, date):
        return datetime.combine(value, time.min).isoformat(), True
    if isinstance(value, str):
        try:
            if len(value) == 10:
                parsed_date = date.fromisoformat(value)
                return datetime.combine(parsed_date, time.min).isoformat(), True
            return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat(), False
        except ValueError as exc:
            raise ValueError(
                f"History timestamp is not an ISO 8601 date/time: {value!r}"
            ) from exc
    # numpy datetime64 may expose .item() as datetime/date depending on unit.
    if hasattr(value, "item"):
        other = value.item()
        if other is not value:
            return _timestamp(other)
    raise ValueError(
        "History contains a non-datetime index. Use datetime/ISO 8601 timestamps."
    )


def _point(timestamp: Any, value: Any) -> dict[str, Any]:
    normalized, inferred = _timestamp(timestamp)
    result = {"time": normalized, "value": json_safe(value)}
    if inferred:
        result["time_inferred"] = True
    return result


def series_points(
    value: Any,
    *,
    day: int,
    reference_time: datetime | None = None,
    allow_scalar: bool = True,
) -> list[dict[str, Any]]:
    """Convert ValueProvider history into properly timestamped points.

    - A timestamp-indexed Pandas Series or mapping keeps the original times.
    - A sequence must carry timestamps, as (time, value) or {time, value}.
    - DummyData.hist returns one synthetic scalar per requested day. For
      that *specific* per-day contract, approximate the sample instant as
      query_time - day_offset; flag this synthesized time explicitly.
    - Never fabricate timestamps for a naked multi-value sequence.
    """
    if value is None:
        return []

    if hasattr(value, "items"):  # pandas Series or a time-to-value mapping
        return [_point(stamp, item) for stamp, item in value.items()]

    if isinstance(value, (list, tuple)):
        result = []
        for item in value:
            if isinstance(item, dict) and "value" in item:
                if "time" in item:
                    stamp = item["time"]
                elif "timestamp" in item:
                    stamp = item["timestamp"]
                else:
                    raise ValueError("History row has no time or timestamp")
                result.append(_point(stamp, item["value"]))
            elif isinstance(item, (tuple, list)) and len(item) == 2:
                result.append(_point(item[0], item[1]))
            else:
                raise ValueError(
                    "History sequence has no timestamps. "
                    "Return (timestamp, value) pairs or a datetime-indexed Series."
                )
        return result

    if not allow_scalar:
        raise ValueError(
            "A range history must contain timestamped values, not a scalar."
        )
    if isinstance(value, (str, bytes, dict)):
        raise ValueError("History result does not contain timestamped data")
    # Per-day scalar is an estimated daily sample, not an actual measurement
    # timestamp. Work in UTC so daylight saving transitions cannot shift it.
    anchor = reference_time if reference_time is not None else datetime.now(timezone.utc)
    if anchor.tzinfo is None or anchor.utcoffset() is None:
        raise ValueError("reference_time must be timezone-aware")
    stamp = (anchor.astimezone(timezone.utc) - timedelta(days=day)).isoformat()
    return [{"time": stamp, "value": json_safe(value), "time_inferred": True}]


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
