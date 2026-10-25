from datetime import date, datetime, timezone

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


def test_scalar_dummy_history_uses_inferred_utc_timestamp():
    reference = datetime(2026, 10, 25, 9, 10, tzinfo=timezone.utc)
    points = series_points(18.5, day=2, reference_time=reference)
    assert points == [{
        "time": "2026-10-23T09:10:00+00:00",
        "value": 18.5,
        "time_inferred": True,
    }]


def test_real_datetime_series_preserves_measurement_timestamp():
    stamp = datetime(2026, 10, 23, 9, 30, tzinfo=timezone.utc)
    points = series_points({stamp: 18.3}, day=5)
    assert points == [{"time": "2026-10-23T09:30:00+00:00", "value": 18.3}]
    assert "time_inferred" not in points[0]


def test_date_only_is_explicitly_marked_as_inferred_midnight():
    points = series_points({date(2026, 10, 22): 19.0}, day=0)
    assert points == [{
        "time": "2026-10-22T00:00:00",
        "value": 19.0, "time_inferred": True,
    }]


def test_timestamped_rows_and_pairs_are_supported():
    rows = [
        {"time": "2026-10-22T08:00:00+02:00", "value": 12},
        ("2026-10-22T09:00:00+02:00", 13),
    ]
    points = series_points(rows, day=0)
    assert [row["value"] for row in points] == [12, 13]
    assert all("T" in row["time"] for row in points)


def test_unstamped_series_and_range_scalars_are_rejected():
    with pytest.raises(ValueError, match="no timestamps"):
        series_points([17.0, 18.0], day=0)
    with pytest.raises(ValueError, match="range history"):
        series_points(18.0, day=7, allow_scalar=False)
    with pytest.raises(ValueError, match="timestamp"):
        series_points({"day-0": 18.0}, day=0)


def test_scalar_history_on_explicit_date_uses_inferred_day_not_now():
    points = series_points(17.4, day=0, sample_date=date(2026, 2, 28))
    assert points == [{
        "time": "2026-02-28T00:00:00", "value": 17.4, "time_inferred": True,
    }]
