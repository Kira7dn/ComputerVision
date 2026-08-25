import pytest

from ls_vision.application.fire_smoke_dataset import temporal_split, yolo_labels


def test_temporal_split_keeps_each_twenty_sample_block_together() -> None:
    assert {temporal_split(index) for index in range(0, 20)} == {"train"}
    assert {temporal_split(index) for index in range(140, 180)} == {"val"}
    assert {temporal_split(index) for index in range(180, 200)} == {"test"}


def test_yolo_labels_clamp_boxes_and_preserve_class_mapping() -> None:
    labels = yolo_labels(
        [
            {"class": "fire", "x1": -10, "y1": 10, "x2": 50, "y2": 60},
            {"class": "smoke", "x1": 50, "y1": 20, "x2": 120, "y2": 80},
            {"class": "person", "x1": 0, "y1": 0, "x2": 10, "y2": 10},
        ],
        width=100,
        height=100,
    )

    assert labels == [
        "0 0.25000000 0.35000000 0.50000000 0.50000000",
        "1 0.75000000 0.50000000 0.50000000 0.60000000",
    ]


def test_dataset_helpers_reject_invalid_dimensions_and_block_size() -> None:
    with pytest.raises(ValueError, match="block_size"):
        temporal_split(0, block_size=0)
    with pytest.raises(ValueError, match="dimensions"):
        yolo_labels([], 0, 100)
