from ls_vision.domain.driver_attention import AttentionObservation, DriverAttentionPolicy


def _observation(timestamp: float, **overrides: object) -> AttentionObservation:
    values = {
        "timestamp": timestamp,
        "driver_present": True,
        "face_detected": True,
        "pose": False,
        "eyes": False,
        "phone": False,
        "fatigue": False,
        "uncertain": False,
        "source": "current",
    }
    values.update(overrides)
    return AttentionObservation(**values)  # type: ignore[arg-type]


def test_attention_escalates_by_elapsed_time_not_frame_count() -> None:
    policy = DriverAttentionPolicy(
        {"alert_seconds": [5.0, 8.0, 13.0], "recovery_confirm_seconds": 2.0}
    )
    states = {}
    for index in range(0, 132):
        timestamp = index / 10.0
        states[round(timestamp, 1)] = policy.update(
            _observation(timestamp, phone=True)
        )

    assert states[4.9].alert_level == "none"
    assert states[5.1].alert_level == "warning"
    assert states[8.1].alert_level == "critical"
    assert states[8.1].event_active is True
    assert states[13.1].alert_level == "emergency"
    assert states[13.1].reasons == ("phone",)


def test_attention_no_face_counts_only_when_driver_is_present() -> None:
    policy = DriverAttentionPolicy({"alert_seconds": [1.0, 2.0, 3.0]})
    policy.update(_observation(0.0, face_detected=False, pose=None, eyes=None))
    state = policy.update(
        _observation(2.1, face_detected=False, pose=None, eyes=None)
    )
    assert state.state == "distracted"
    assert "no_face" in state.reasons

    empty = policy.update(
        _observation(2.2, driver_present=False, face_detected=False)
    )
    assert empty.state == "no_driver"
    assert empty.score is None
    assert empty.event_active is False


def test_attention_event_recovers_only_after_attentive_confirmation() -> None:
    policy = DriverAttentionPolicy(
        {
            "alert_seconds": [1.0, 2.0, 3.0],
            "recovery_confirm_seconds": 1.0,
        }
    )
    critical = None
    for index in range(22):
        critical = policy.update(_observation(index / 10.0, eyes=True))
    assert critical is not None
    assert critical.event_active is True

    first = policy.update(_observation(2.2))
    assert first.attentive is True
    assert first.event_active is True
    recovered = first
    for index in range(23, 44):
        recovered = policy.update(_observation(index / 10.0))
    assert recovered.score is not None and recovered.score > 67
    assert recovered.event_active is False


def test_uncertain_model_never_counts_as_attentive() -> None:
    policy = DriverAttentionPolicy({"alert_seconds": [1.0, 2.0, 3.0]})
    policy.update(_observation(0.0, uncertain=True))
    state = policy.update(_observation(1.1, uncertain=True))
    assert state.attentive is False
    assert state.reasons == ("uncertain",)
    assert state.alert_level == "warning"
