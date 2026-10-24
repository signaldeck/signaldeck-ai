from datetime import datetime, timezone

import pytest

from signaldeck_ai.serialization import (
    bounded_sample, json_safe, series_points, summarize_points,
)


def test_json_serialization_and_invalid_floats():
    assert json_safe({"x": float("nan"), "t": datetime(2026, 10, 1, tzinfo=timezone.utc)}) == {
        "x": None, "t": "2026-10-01T00:00:00+00:00"
    }
    with pytest.raises(TypeError):
        json_safe(object())


def test_statistics_use_entire_series_not_sample():
    points = series_points({"2026-10-01": 10, "2026-10-02": 50, "2026-10-03": 20}, day=1)
    result = summarize_points(points)
    assert result["maximum"] == 50
    assert result["count"] == 3
    assert len(bounded_sample(points, 2)) == 2


def test_sampling_includes_endpoints():
    points = [{"time": str(i), "value": i} for i in range(30)]
    sample = bounded_sample(points, 10)
    assert sample[0]["value"] == 0
    assert sample[-1]["value"] == 29
