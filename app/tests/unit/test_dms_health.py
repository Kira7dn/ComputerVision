from domain.dms_health import requires_person_inference, resolve_dms_health


def test_dms_requires_the_shared_person_tracker() -> None:
    assert requires_person_inference({"dms": True})
    assert requires_person_inference({"face_recognition": True})
    assert not requires_person_inference({"front_assistance": True})


def test_dms_never_reports_ok_without_a_driver_observation() -> None:
    health = resolve_dms_health(
        "OK",
        (),
        {
            "driver_person_count": 0,
            "face_detected": True,
        },
    )

    assert health.status == "PARTIAL"
    assert health.message == "driver person track unavailable"
    assert health.observation_ready is False


def test_dms_reports_monitoring_only_when_person_and_face_are_visible() -> None:
    health = resolve_dms_health(
        "OK",
        (),
        {
            "driver_person_count": 1,
            "face_detected": True,
        },
    )

    assert health.status == "MONITORING"
    assert health.observation_ready is True


def test_dms_alert_takes_precedence_over_partial_sensor_visibility() -> None:
    health = resolve_dms_health(
        "ALERT",
        ("Smoking",),
        {
            "driver_person_count": 1,
            "face_detected": False,
        },
    )

    assert health.status == "ALERT"
    assert health.observation_ready is True
