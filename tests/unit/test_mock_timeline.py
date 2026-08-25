import pytest

from domain.mock_timeline import (
    frame_index_for_timestamp,
    media_time_for_timestamp,
    normalized_timeline_phase,
    timeline_phase_seconds,
)


def test_four_cameras_share_one_normalized_phase() -> None:
    timestamp = 1_777_777_777.25
    period = 191.1

    phase = normalized_timeline_phase(timestamp, period)

    assert frame_index_for_timestamp(timestamp, period, 1_911) == int(phase * 1_911)
    assert frame_index_for_timestamp(timestamp, period, 3_822) == int(phase * 3_822)
    assert media_time_for_timestamp(timestamp, period, 191.08) == pytest.approx(
        phase * 191.08
    )
    assert media_time_for_timestamp(timestamp, period, 191.12) == pytest.approx(
        phase * 191.12
    )


def test_restart_resumes_absolute_group_phase_instead_of_frame_zero() -> None:
    period = 10.0
    epoch = 100.0

    assert frame_index_for_timestamp(102.5, period, 100, epoch) == 25
    assert frame_index_for_timestamp(107.5, period, 100, epoch) == 75


def test_shared_timeline_wraps_at_group_period() -> None:
    assert timeline_phase_seconds(110.25, 10.0, 100.0) == pytest.approx(0.25)
    assert frame_index_for_timestamp(110.25, 10.0, 100, 100.0) == 2


@pytest.mark.parametrize("period", [0.0, -1.0, float("inf")])
def test_invalid_period_is_rejected(period: float) -> None:
    with pytest.raises(ValueError, match="period_seconds"):
        timeline_phase_seconds(1.0, period)
