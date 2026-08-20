"""Standalone recognition lifecycle for the Camera Safety package.

This module owns track identity and aggregation only. Inference adapters are
optional and must return ``RawRecognition``; no identity or plate is invented
when a model is unavailable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RecognitionTask(str, Enum):
    FACE = "face"
    LPR = "lpr"


@dataclass(frozen=True, slots=True)
class TrackKey:
    camera_id: str
    stream_epoch: str
    track_id: str


@dataclass(frozen=True, slots=True)
class RawRecognition:
    value: str | None
    score: float
    detail_bbox: tuple[int, int, int, int] | None = None
    area: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RecognitionUpdate:
    task: RecognitionTask
    key: TrackKey
    frame_time: float
    raw_value: str | None
    raw_score: float
    aggregate_value: str | None
    aggregate_score: float
    object_bbox: tuple[int, int, int, int]
    detail_bbox: tuple[int, int, int, int] | None
    publish: bool
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FacePolicy:
    unknown_score: float = 0.8
    recognition_threshold: float = 0.9
    min_faces: int = 1
    max_attempts: int = 12
    max_attempts_after_recognition: int = 6
    area_cap: int = 4000
    identity_switch_similarity: float = 0.65
    identity_switch_frames: int = 5


@dataclass(frozen=True, slots=True)
class LprPolicy:
    detect_fps: int = 5
    recognition_threshold: float = 0.9
    cluster_threshold: float = 0.85
    min_plate_length: int = 0
    plate_format: str | None = None


def _jaro_winkler(left: str, right: str) -> float:
    if left == right:
        return 1.0
    if not left or not right:
        return 0.0
    distance = max(len(left), len(right)) // 2 - 1
    left_matches = [False] * len(left)
    right_matches = [False] * len(right)
    matches = 0
    for index, value in enumerate(left):
        start = max(0, index - distance)
        end = min(index + distance + 1, len(right))
        for candidate in range(start, end):
            if right_matches[candidate] or value != right[candidate]:
                continue
            left_matches[index] = True
            right_matches[candidate] = True
            matches += 1
            break
    if not matches:
        return 0.0
    left_order = [value for index, value in enumerate(left) if left_matches[index]]
    right_order = [value for index, value in enumerate(right) if right_matches[index]]
    transpositions = sum(a != b for a, b in zip(left_order, right_order, strict=False)) / 2
    jaro = (
        matches / len(left)
        + matches / len(right)
        + (matches - transpositions) / matches
    ) / 3
    prefix = 0
    for left_value, right_value in zip(left, right, strict=False):
        if left_value != right_value or prefix == 4:
            break
        prefix += 1
    return jaro + prefix * 0.1 * (1 - jaro)


def _weighted_face_vote(
    results: list[RawRecognition], policy: FacePolicy
) -> tuple[str | None, float]:
    counts: dict[str, int] = {}
    weighted_scores: dict[str, float] = {}
    total_weights: dict[str, float] = {}
    for result in results:
        name = result.value
        if not name or name == "unknown":
            continue
        counts[name] = counts.get(name, 0) + 1
        weight = min(result.area, policy.area_cap)
        weight *= (result.score - policy.unknown_score) * 10
        weighted_scores[name] = weighted_scores.get(name, 0.0) + result.score * weight
        total_weights[name] = total_weights.get(name, 0.0) + weight
    if not weighted_scores:
        return None, 0.0
    best_name = max(weighted_scores, key=weighted_scores.get)
    if counts[best_name] < policy.min_faces:
        return None, 0.0
    if any(name != best_name and counts[best_name] == count for name, count in counts.items()):
        return None, 0.0
    total_weight = total_weights[best_name]
    if total_weight == 0:
        return None, 0.0
    return best_name, weighted_scores[best_name] / total_weight


def _lpr_representative(
    variants: list[RawRecognition], threshold: float
) -> tuple[RawRecognition, list[list[RawRecognition]]]:
    clusters: list[list[RawRecognition]] = []
    for variant in variants:
        for cluster in clusters:
            similarities = [
                _jaro_winkler(variant.value or "", item.value or "") for item in cluster
            ]
            if sum(similarities) / len(similarities) >= threshold:
                cluster.append(variant)
                break
        else:
            clusters.append([variant])
    best = max(clusters, key=lambda cluster: (len(cluster), max(item.score for item in cluster)))
    return max(best, key=lambda item: item.score), clusters


class RecognitionCore:
    """Track-scoped cadence and aggregation for recognition decisions."""

    def __init__(self, config: dict[str, Any]) -> None:
        recognition = config.get("recognition", {})
        self.face_policy = FacePolicy(**recognition.get("face", {}))
        self.lpr_policy = LprPolicy(**recognition.get("lpr", {}))
        self.enabled = bool(recognition.get("enabled", False))
        self.stream_epoch = str(recognition.get("stream_epoch", "deepstream"))
        self.face_history: dict[TrackKey, list[RawRecognition]] = {}
        self.lpr_history: dict[TrackKey, list[RawRecognition]] = {}
        self._ended: set[TrackKey] = set()
        self._last_seen: dict[TrackKey, float] = {}

    def touch(self, key: TrackKey, frame_time: float) -> None:
        if key not in self._ended:
            self._last_seen[key] = frame_time

    def should_attempt_face(self, key: TrackKey, sub_label: str | None) -> bool:
        history = self.face_history.get(key, [])
        if sub_label and not history:
            return False
        if len(history) < self.face_policy.max_attempts_after_recognition:
            return True
        if sub_label:
            return False
        return len(history) < self.face_policy.max_attempts

    def observe(
        self,
        task: RecognitionTask,
        key: TrackKey,
        frame_time: float,
        object_bbox: tuple[int, int, int, int],
        result: RawRecognition | None,
        *,
        sub_label: str | None = None,
    ) -> RecognitionUpdate | None:
        if not self.enabled or key in self._ended:
            return None
        self.touch(key, frame_time)
        if task is RecognitionTask.FACE:
            if not self.should_attempt_face(key, sub_label) or result is None:
                return None
            history = self.face_history.setdefault(key, [])
            history.append(result)
            name, score = _weighted_face_vote(history, self.face_policy)
            return RecognitionUpdate(
                task, key, frame_time, result.value, result.score, name, score,
                object_bbox, result.detail_bbox,
                name is not None and score >= self.face_policy.recognition_threshold,
                "master_weighted_vote" if name else "master_vote_not_ready",
                {"history_size": len(history)},
            )
        if result is None or not result.value or result.score < self.lpr_policy.recognition_threshold:
            return None
        history = self.lpr_history.setdefault(key, [])
        history.append(result)
        window = max(1, self.lpr_policy.detect_fps) * 5
        if len(history) > window:
            del history[:-window]
        representative, clusters = _lpr_representative(history, self.lpr_policy.cluster_threshold)
        publish = len(representative.value or "") >= self.lpr_policy.min_plate_length
        reason = "master_variant_representative"
        if self.lpr_policy.plate_format and not re.fullmatch(self.lpr_policy.plate_format, representative.value or ""):
            publish = False
            reason = "format_mismatch"
        return RecognitionUpdate(
            task, key, frame_time, result.value, result.score, representative.value,
            representative.score, object_bbox, result.detail_bbox, publish, reason,
            {"history_size": len(history), "cluster_sizes": tuple(len(c) for c in clusters)},
        )

    def end_track(self, key: TrackKey, reason: str) -> dict[str, Any]:
        self._ended.add(key)
        self._last_seen.pop(key, None)
        self.face_history.pop(key, None)
        self.lpr_history.pop(key, None)
        return {"track_id": key.track_id, "camera": key.camera_id, "reason": reason}

    def active_tracks(self) -> tuple[TrackKey, ...]:
        return tuple(self._last_seen)
