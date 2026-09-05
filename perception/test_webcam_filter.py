import numpy as np
import pytest

from perception.webcam_filter import WebcamFilter


def test_late_fix_replays_later_fixes_and_gating_like_chronological_input():
    observations = [
        ("a", 0.0, [0, 0, 0]),
        ("b", 0.4, [0.3, 0, 0]),
        ("c", 0.8, [0.7, 0, 0]),
        ("outlier", 0.9, [100, 0, 0]),
    ]
    chronological = WebcamFilter()
    delayed = WebcamFilter()
    for event in observations:
        chronological.observe(*event, now=1.0)
    for index in [0, 2, 3, 1]:
        delayed.observe(*observations[index], now=1.0)
    expected, actual = chronological.at(1.0), delayed.at(1.0)
    assert actual["position_map_m"] == pytest.approx(expected["position_map_m"])
    assert actual["velocity_map_mps"] == pytest.approx(expected["velocity_map_mps"])
    assert np.allclose(actual["state_covariance"], expected["state_covariance"])
    assert actual["fix_decisions"] == expected["fix_decisions"]
    assert actual["fix_decisions"]["outlier"] == "rejected"
    assert actual["last_fix_capture_time"] == 0.8
    assert actual["velocity_map_mps"][0] > 0.5


def test_outlier_and_delayed_arrival_never_refresh_capture_age():
    tracker = WebcamFilter()
    tracker.observe("first", 0, [0, 0, 0], now=0)
    outlier = tracker.observe("bad", 0.4, [100, 0, 0], now=0.5)
    assert outlier["observation_status"] == "rejected"
    assert outlier["fix_age_s"] == 0.5
    assert outlier["confidence"] == "amber"
    late = tracker.observe("late", 0.2, [0, 0, 0], now=1.5)
    assert late["fix_age_s"] == 1.3
    assert late["confidence"] == "amber"
    assert tracker.at(2.2)["confidence"] == "red"
    assert tracker.at(2.2)["flight_approved"] is False


def test_empty_and_initial_state_do_not_invent_velocity_measurements():
    tracker = WebcamFilter()
    empty = tracker.at(0)
    assert empty["position_map_m"] is None
    assert empty["confidence"] == "red"
    result = tracker.observe("first", 0, [1, 2, 3], now=0)
    assert result["velocity_map_mps"] == [0, 0, 0]
    assert np.diag(result["state_covariance"])[3:].tolist() == [1, 1, 1]
    assert tracker.at(0.499)["confidence"] == "green"
    assert tracker.at(0.5)["confidence"] == "amber"
    assert tracker.at(2)["confidence"] == "red"


def test_checkpoint_pruning_preserves_prediction_covariance_and_last_accepted_capture():
    bounded = WebcamFilter(horizon_s=0.5, max_events=3)
    reference = WebcamFilter(horizon_s=100, max_events=100)
    for index in range(30):
        timestamp = index * 0.1
        for tracker in [bounded, reference]:
            tracker.observe(str(index), timestamp, [timestamp * 0.2, 0, 0], now=timestamp)
        assert bounded.at(timestamp)["retained_event_count"] <= 3
    for now in [3, 4, 10]:
        actual, expected = bounded.at(now), reference.at(now)
        assert actual["position_map_m"] == pytest.approx(expected["position_map_m"])
        assert actual["velocity_map_mps"] == pytest.approx(expected["velocity_map_mps"])
        assert np.allclose(actual["state_covariance"], expected["state_covariance"])
        assert actual["last_fix_capture_time"] == expected["last_fix_capture_time"]
        assert actual["confidence"] == expected["confidence"]
    assert actual["retained_event_count"] == 0


def test_late_fix_after_checkpoint_matches_unpruned_replay():
    bounded = WebcamFilter(horizon_s=1, max_events=4)
    reference = WebcamFilter(horizon_s=10)
    for tracker in [bounded, reference]:
        for index in range(10):
            tracker.observe(str(index), index * 0.2, [index * 0.1, 0, 0], now=index * 0.2)
        tracker.observe("delayed", 1.5, [0.76, 0, 0], now=1.9)
    actual, expected = bounded.at(1.9), reference.at(1.9)
    assert actual["position_map_m"] == pytest.approx(expected["position_map_m"])
    assert np.allclose(actual["state_covariance"], expected["state_covariance"])


def test_duplicates_and_events_older_than_count_checkpoint_leave_state_unchanged():
    tracker = WebcamFilter(max_events=2)
    tracker.observe("a", 0, [0, 0, 0], now=0)
    tracker.observe("b", 0.1, [0, 0, 0], now=0.1)
    before = tracker.observe("c", 0.2, [0, 0, 0], now=0.2)
    duplicate = tracker.observe("b", 0.15, [100, 0, 0], now=0.2)
    old = tracker.observe("a", 0, [100, 0, 0], now=0.2)
    assert duplicate["observation_status"] == "duplicate"
    assert old["observation_status"] == "too_old"
    assert old["position_map_m"] == before["position_map_m"]
    assert old["state_covariance"] == before["state_covariance"]
    assert tracker.observe("ancient", 0.1, [100, 0, 0], now=3)["observation_status"] == "too_old"


@pytest.mark.parametrize("time", [float("nan"), float("inf"), -1])
def test_invalid_or_regressing_arrival_times_are_rejected(time):
    tracker = WebcamFilter()
    tracker.at(1)
    with pytest.raises(ValueError, match="monotonic"):
        tracker.at(time)
    with pytest.raises(ValueError, match="monotonic"):
        tracker.at(0.9)


@pytest.mark.parametrize(
    "capture,position",
    [
        (2, [0, 0, 0]),
        (float("nan"), [0, 0, 0]),
        (-1, [0, 0, 0]),
        (0, [0, 0]),
        (0, [float("inf"), 0, 0]),
    ],
)
def test_invalid_capture_events_are_rejected(capture, position):
    with pytest.raises(ValueError, match="invalid captured position"):
        WebcamFilter().observe("fix", capture, position, now=1)


def test_covariance_remains_symmetric_positive_under_repeated_updates():
    tracker = WebcamFilter()
    for index in range(50):
        result = tracker.observe(str(index), index * 0.05, [index * 0.01, 0, 0], now=index * 0.05)
        covariance = np.asarray(result["state_covariance"])
        assert np.allclose(covariance, covariance.T)
        assert np.linalg.eigvalsh(covariance).min() > 0
