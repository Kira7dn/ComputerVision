"""In-pipeline face detection, ArcFace embedding and gallery matching."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import onnxruntime as ort

LOG = logging.getLogger("deepstream.face")
_UNTRACKED = (1 << 64) - 1
_REFERENCE = np.array(
    [[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
     [41.5493, 92.3655], [70.7299, 92.2041]], dtype=np.float32
)


def _similarity_to_confidence(cosine_similarity: float) -> float:
    """Match Frigate's ArcFace cosine-to-confidence mapping."""
    return float(1.0 / (1.0 + np.exp(-20.0 * (cosine_similarity - 0.5))))


@dataclass(frozen=True)
class GalleryEntry:
    label: str
    embedding: np.ndarray
    source: str


class TrackRecognitionScheduler:
    """Limit face inference by elapsed time for each active track."""

    def __init__(
        self,
        interval_ms: int | float = 400,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        # Keep the runtime contract bounded to the 300-500 ms target from the
        # performance report even when a malformed config value is supplied.
        self.interval_ms = max(300.0, min(500.0, float(interval_ms)))
        self.interval_seconds = self.interval_ms / 1000.0
        self._clock = clock
        self._last_attempt_at: dict[int, float] = {}

    def now(self) -> float:
        return float(self._clock())

    def due(self, track_id: int, now: float | None = None) -> bool:
        current = self.now() if now is None else float(now)
        previous = self._last_attempt_at.get(track_id)
        return previous is None or current - previous >= self.interval_seconds

    def mark_attempt(self, track_id: int, now: float | None = None) -> None:
        self._last_attempt_at[track_id] = self.now() if now is None else float(now)

    def forget(self, track_id: int) -> None:
        self._last_attempt_at.pop(track_id, None)


def _select_onnx_providers(requested: list[str], available: list[str]) -> list[str]:
    """Return configured providers that are actually available to ONNX Runtime."""
    available_set = set(available)
    selected = [provider for provider in requested if provider in available_set]
    if not selected and "CPUExecutionProvider" in available_set:
        selected.append("CPUExecutionProvider")
    elif "CPUExecutionProvider" in available_set and "CPUExecutionProvider" not in selected:
        selected.append("CPUExecutionProvider")
    return selected


class FaceRecognitionEngine:
    """Recognize faces belonging to currently tracked person objects."""

    def __init__(
        self,
        config: dict[str, Any],
        trace_sink: Callable[[int, dict[str, Any], np.ndarray | None], None] | None = None,
    ):
        section = config.get("recognition", {}) or {}
        policy = section.get("face", {}) or {}
        face = section.get("face_runtime", {}) or {}
        self.enabled = bool(section.get("enabled", False))
        self.camera_id = str((config.get("input", {}) or {}).get("camera", "camera"))
        input_config = config.get("input", {}) or {}
        self.output_width = int(input_config.get("width", 0) or 0)
        self.output_height = int(input_config.get("height", 0) or 0)
        self.unknown_score = float(policy.get("unknown_score", 0.8))
        self.threshold = float(policy.get("recognition_threshold", 0.9))
        self.detector_threshold = float(face.get("detection_threshold", 0.5))
        self.min_area = int(face.get("min_area", 1200))
        self.max_attempts = int(policy.get("max_attempts", 12))
        self.max_attempts_after_recognition = max(
            0, int(policy.get("max_attempts_after_recognition", 6))
        )
        self.min_faces = max(2, int(policy.get("min_faces", 2)))
        self.identity_switch_similarity = float(policy.get("identity_switch_similarity", 0.65))
        self.identity_switch_frames = max(5, int(policy.get("identity_switch_frames", 5)))
        self.area_cap = int(policy.get("area_cap", 4000))
        self.detector_path = Path(str(face.get("detector_model", "")))
        self.recognizer_path = Path(str(face.get("recognizer_model", "")))
        self.library_path = Path(str(face.get("library_directory", "")))
        self.trace_enabled = bool(face.get("trace_enabled", True))
        self.trace_sink = trace_sink
        self.recognition_scheduler = TrackRecognitionScheduler(
            face.get("recognition_interval_ms", 400)
        )
        self.provider_preference = [
            str(provider)
            for provider in face.get(
                "providers",
                [
                    "TensorrtExecutionProvider",
                    "CUDAExecutionProvider",
                    "CPUExecutionProvider",
                ],
            )
        ]
        self.require_gpu_provider = bool(face.get("require_gpu_provider", False))
        self.active_providers: list[str] = []
        self.gallery: list[GalleryEntry] = []
        self._track_names: dict[int, str] = {}
        self._track_scores: dict[int, float] = {}
        self._started_tracks: set[int] = set()
        self._track_last_seen: dict[int, int] = {}
        self._track_observations: dict[int, int] = {}
        self._track_candidate_scores: dict[int, dict[str, float]] = {}
        self._track_candidate_weights: dict[int, dict[str, float]] = {}
        self._track_candidate_counts: dict[int, dict[str, int]] = {}
        self._track_attempts: dict[int, int] = {}
        self._track_locked: set[int] = set()
        self._track_identity_embeddings: dict[int, np.ndarray] = {}
        self._track_identity_switches: dict[int, int] = {}
        self._track_alternative_names: dict[int, str] = {}
        self._track_alternative_counts: dict[int, int] = {}
        self._track_embedding_means: dict[int, np.ndarray] = {}
        self._track_embedding_counts: dict[int, int] = {}
        self._track_last_confirmed: dict[int, int] = {}
        self._track_face_misses: dict[int, int] = {}
        self.max_disappeared = max(2, int((config.get("input", {}) or {}).get("fps", 5)) * 2)
        self.latest_frame: np.ndarray | None = None
        self.last_processed_frame: np.ndarray | None = None
        self.last_processed_frame_number: int | None = None
        self._frames_by_pts: dict[int, np.ndarray] = {}
        self._pts_order: list[int] = []
        self._last_decode_info: dict[str, int] = {}
        self.detector = None
        self.session = None
        LOG.info(
            "face runtime: camera=%s enabled=%s trace_enabled=%s",
            self.camera_id,
            self.enabled,
            self.trace_enabled,
        )
        if not self.enabled:
            LOG.info("face recognition disabled by recognition.enabled")
            return
        self._load_models()
        self._load_gallery()
        LOG.info("face recognition enabled: gallery=%d threshold=%.2f", len(self.gallery), self.threshold)

    def _load_models(self) -> None:
        if not self.detector_path.is_file():
            raise FileNotFoundError(f"face detector model not found: {self.detector_path}")
        if not self.recognizer_path.is_file():
            raise FileNotFoundError(f"face recognizer model not found: {self.recognizer_path}")
        creator = getattr(cv2, "FaceDetectorYN_create", None)
        if creator is None:
            creator = cv2.FaceDetectorYN.create
        self.detector = creator(
            str(self.detector_path), "", (320, 320), self.detector_threshold, 0.3, 5000
        )
        available = list(ort.get_available_providers())
        selected = _select_onnx_providers(self.provider_preference, available)
        gpu_providers = {"TensorrtExecutionProvider", "CUDAExecutionProvider"}
        if self.require_gpu_provider and not gpu_providers.intersection(selected):
            raise RuntimeError(
                "GPU face provider requested but unavailable; "
                f"requested={self.provider_preference} available={available}"
            )
        if not selected:
            raise RuntimeError(f"No usable ONNX Runtime provider; available={available}")
        self.session = ort.InferenceSession(str(self.recognizer_path), providers=selected)
        self.active_providers = list(self.session.get_providers())
        if not gpu_providers.intersection(self.active_providers):
            LOG.warning(
                "face recognizer is running on CPU; requested=%s available=%s active=%s",
                self.provider_preference,
                available,
                self.active_providers,
            )
        else:
            LOG.info("face recognizer GPU provider active: %s", self.active_providers)
        LOG.info(
            "face models loaded: detector=%s recognizer=%s requested_providers=%s "
            "available_providers=%s active_providers=%s",
            self.detector_path,
            self.recognizer_path,
            self.provider_preference,
            available,
            self.active_providers,
        )

    def _load_gallery(self) -> None:
        if not self.library_path.is_dir():
            LOG.warning("face gallery directory does not exist: %s; all faces remain unknown", self.library_path)
            return
        for image_path in sorted(self.library_path.rglob("*")):
            if image_path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
                continue
            image = cv2.imread(str(image_path))
            if image is None:
                continue
            label = image_path.parent.name if image_path.parent != self.library_path else image_path.stem
            embedding = self._embedding(image)
            self.gallery.append(GalleryEntry(label, embedding, str(image_path)))

    @staticmethod
    def _build_class_mean(embeddings: list[np.ndarray]) -> np.ndarray | None:
        if not embeddings:
            return None
        values = np.stack(embeddings).astype(np.float32, copy=False)
        if len(values) < 5:
            mean = values.mean(axis=0)
            return mean / (np.linalg.norm(mean) + 1e-9)
        keep = np.ones(len(values), dtype=bool)
        floor = max(5, int(np.ceil(len(values) * 0.7)))
        for _ in range(3):
            mean = values[keep].mean(axis=0)
            mean /= np.linalg.norm(mean) + 1e-9
            normalized = values / (np.linalg.norm(values, axis=1, keepdims=True) + 1e-9)
            cosine = normalized @ mean
            new_keep = cosine >= 0.30
            if int(new_keep.sum()) < floor:
                new_keep = np.zeros(len(values), dtype=bool)
                new_keep[np.argsort(-cosine)[:floor]] = True
            if np.array_equal(new_keep, keep):
                break
            keep = new_keep
        mean = values[keep].mean(axis=0)
        return mean / (np.linalg.norm(mean) + 1e-9)

    def _detect_all(self, image: np.ndarray) -> list[np.ndarray]:
        height, width = image.shape[:2]
        self.detector.setInputSize((width, height))
        _, faces = self.detector.detect(image)
        if faces is None or len(faces) == 0:
            return []
        return [
            np.asarray(face, dtype=np.float32)
            for face in sorted(faces, key=lambda row: float(row[-1]), reverse=True)
            if float(face[-1]) >= self.detector_threshold
        ]

    def _detect(self, image: np.ndarray) -> np.ndarray | None:
        faces = self._detect_all(image)
        return faces[0] if faces else None

    def _align(self, image: np.ndarray, face: np.ndarray) -> np.ndarray:
        landmarks = face[4:14].reshape(5, 2).astype(np.float32)
        transform, _ = cv2.estimateAffinePartial2D(landmarks, _REFERENCE, method=cv2.LMEDS)
        if transform is None:
            x, y, w, h = [max(0, int(v)) for v in face[:4]]
            return cv2.resize(image[y:y + h, x:x + w], (112, 112))
        return cv2.warpAffine(image, transform, (112, 112), borderMode=cv2.BORDER_CONSTANT)

    def _embedding(self, image: np.ndarray) -> np.ndarray:
        input_name = self.session.get_inputs()[0].name
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        height, width = rgb.shape[:2]
        if width != 112 or height != 112:
            if width > height:
                new_height = max(4, int(((height / width) * 112) // 4 * 4))
                rgb = cv2.resize(rgb, (112, new_height))
            else:
                new_width = max(4, int(((width / height) * 112) // 4 * 4))
                rgb = cv2.resize(rgb, (new_width, 112))
            padded = np.zeros((112, 112, 3), dtype=np.uint8)
            y = (112 - rgb.shape[0]) // 2
            x = (112 - rgb.shape[1]) // 2
            padded[y:y + rgb.shape[0], x:x + rgb.shape[1]] = rgb
            rgb = padded
        tensor = np.transpose((rgb.astype(np.float32) / 127.5) - 1.0, (2, 0, 1))[None, ...]
        result = np.asarray(self.session.run(None, {input_name: tensor})[0]).reshape(-1)
        norm = float(np.linalg.norm(result))
        return result / norm if norm > 0 else result

    def _trace(self, track_id: int, data: dict[str, Any]) -> None:
        if not self.trace_enabled or self.trace_sink is None:
            return
        self.trace_sink(track_id, {"ts": time.time(), **data}, self.last_processed_frame)

    def _start_track(self, track_id: int, frame_number: int, bbox: list[int]) -> None:
        self._track_last_seen[track_id] = frame_number
        self._track_observations[track_id] = 0
        self._track_candidate_scores[track_id] = {}
        self._track_candidate_weights[track_id] = {}
        self._track_candidate_counts[track_id] = {}
        self._track_attempts[track_id] = 0
        self._track_identity_switches[track_id] = 0
        self._track_alternative_names.pop(track_id, None)
        self._track_alternative_counts[track_id] = 0
        self._track_face_misses[track_id] = 0
        self._track_embedding_means.pop(track_id, None)
        self._track_embedding_counts[track_id] = 0
        self._trace(track_id, {"event": "track_start", "frame": frame_number, "track_id": track_id, "person_bbox": bbox})

    def _clear_identity(self, track_id: int, frame_number: int, reason: str) -> None:
        if track_id not in self._track_last_seen:
            return
        previous_name = self._track_names.get(track_id, "unknown")
        self._track_candidate_scores[track_id] = {}
        self._track_candidate_weights[track_id] = {}
        self._track_candidate_counts[track_id] = {}
        self._track_attempts[track_id] = 0
        self._track_locked.discard(track_id)
        self._track_names.pop(track_id, None)
        self._track_scores.pop(track_id, None)
        self._track_last_confirmed.pop(track_id, None)
        self._track_identity_switches[track_id] = 0
        self._track_alternative_names.pop(track_id, None)
        self._track_alternative_counts[track_id] = 0
        self._track_identity_embeddings.pop(track_id, None)
        self._track_embedding_means.pop(track_id, None)
        self._track_embedding_counts[track_id] = 0
        self._trace(track_id, {"event": "identity_cleared", "frame": frame_number, "previous_name": previous_name, "reason": reason})

    def _record_face_miss(self, track_id: int, frame_number: int, reason: str) -> None:
        misses = self._track_face_misses.get(track_id, 0) + 1
        self._track_face_misses[track_id] = misses
        # A detector miss is not evidence that the person changed. Keep a
        # confirmed identity until the tracker ends; otherwise the live label
        # becomes unknown between two face-detector samples.
        self._trace(
            track_id,
            {"event": "face_miss", "frame": frame_number, "reason": reason, "misses": misses},
        )

    def _reset_identity(self, track_id: int, frame_number: int, embedding: np.ndarray) -> None:
        previous_name = self._track_names.get(track_id, "unknown")
        self._track_candidate_scores[track_id] = {}
        self._track_candidate_weights[track_id] = {}
        self._track_candidate_counts[track_id] = {}
        self._track_attempts[track_id] = 0
        self._track_locked.discard(track_id)
        self._track_names.pop(track_id, None)
        self._track_scores.pop(track_id, None)
        self._track_identity_embeddings[track_id] = embedding.copy()
        self._track_embedding_means[track_id] = embedding.copy()
        self._track_embedding_counts[track_id] = 1
        self._track_identity_switches[track_id] = 0
        self._trace(track_id, {"event": "identity_switch", "frame": frame_number, "previous_name": previous_name})

    def _observe_identity(self, track_id: int, frame_number: int, embedding: np.ndarray) -> None:
        anchor = self._track_identity_embeddings.get(track_id)
        if anchor is None:
            self._track_identity_embeddings[track_id] = embedding.copy()
            return
        similarity = float(np.dot(anchor, embedding))
        if track_id in self._track_locked and similarity < self.identity_switch_similarity:
            switches = self._track_identity_switches.get(track_id, 0) + 1
            self._track_identity_switches[track_id] = switches
            self._trace(
                track_id,
                {
                    "event": "identity_mismatch",
                    "frame": frame_number,
                    "similarity": similarity,
                    "count": switches,
                },
            )
            return
        self._track_identity_switches[track_id] = 0
        self._track_alternative_names.pop(track_id, None)
        self._track_alternative_counts[track_id] = 0
        self._track_identity_embeddings[track_id] = (anchor * 0.8 + embedding * 0.2)
        norm = float(np.linalg.norm(self._track_identity_embeddings[track_id]))
        if norm > 0:
            self._track_identity_embeddings[track_id] /= norm

    def _finish_track(self, track_id: int, frame_number: int) -> None:
        if track_id not in self._track_last_seen:
            return
        candidates = self._track_candidate_scores.get(track_id, {})
        counts = self._track_candidate_counts.get(track_id, {})
        final_name = self._track_names.get(track_id, "unknown")
        final_score = self._track_scores.get(track_id, 0.0)
        if candidates:
            candidate_name = max(candidates, key=candidates.get)
            weights = self._track_candidate_weights.get(track_id, {})
            candidate_score = candidates[candidate_name] / max(1e-9, weights.get(candidate_name, 1.0))
            candidate_count = counts.get(candidate_name, 0)
            tied = any(name != candidate_name and candidate_count == count for name, count in counts.items())
            if candidate_count > 0 and not tied and candidate_score >= self.threshold:
                final_name, final_score = candidate_name, candidate_score
            else:
                final_name, final_score = "unknown", candidate_score
        self._trace(track_id, {"event": "track_end", "frame": frame_number, "track_id": track_id, "name": final_name, "score": final_score, "observations": self._track_observations.get(track_id, 0), "candidates": counts})
        self._track_last_seen.pop(track_id, None)
        self._track_observations.pop(track_id, None)
        self._track_candidate_scores.pop(track_id, None)
        self._track_candidate_weights.pop(track_id, None)
        self._track_candidate_counts.pop(track_id, None)
        self._track_attempts.pop(track_id, None)
        self._track_locked.discard(track_id)
        self._track_names.pop(track_id, None)
        self._track_scores.pop(track_id, None)
        self._track_identity_embeddings.pop(track_id, None)
        self._track_identity_switches.pop(track_id, None)
        self._track_alternative_names.pop(track_id, None)
        self._track_alternative_counts.pop(track_id, None)
        self._track_embedding_means.pop(track_id, None)
        self._track_embedding_counts.pop(track_id, None)
        self._track_last_confirmed.pop(track_id, None)
        self._track_face_misses.pop(track_id, None)
        self._started_tracks.discard(track_id)
        self.recognition_scheduler.forget(track_id)

    def _record_candidate(self, track_id: int, label: str, score: float, area: int) -> tuple[str, float]:
        if track_id in self._track_locked:
            return self._track_names.get(track_id, "unknown"), self._track_scores.get(track_id, 0.0)
        scores = self._track_candidate_scores.setdefault(track_id, {})
        weights = self._track_candidate_weights.setdefault(track_id, {})
        counts = self._track_candidate_counts.setdefault(track_id, {})
        if not label or label == "unknown" or score <= self.unknown_score:
            return "unknown", 0.0
        counts[label] = counts.get(label, 0) + 1
        weight = min(area, self.area_cap) * (score - self.unknown_score) * 10.0
        scores[label] = scores.get(label, 0.0) + score * weight
        weights[label] = weights.get(label, 0.0) + weight
        best_label = max(scores, key=scores.get)
        if counts[best_label] < self.min_faces or any(
            name != best_label and counts[best_label] == count
            for name, count in counts.items()
        ):
            return "unknown", 0.0
        aggregate_score = scores[best_label] / max(1e-9, weights[best_label])
        if aggregate_score < self.threshold:
            return "unknown", aggregate_score
        self._track_names[track_id] = best_label
        self._track_scores[track_id] = aggregate_score
        self._track_locked.add(track_id)
        return best_label, aggregate_score

    def update_jpeg(self, content: bytes, pts: int | None = None) -> None:
        image = cv2.imdecode(np.frombuffer(content, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is not None:
            self.latest_frame = image
            if pts is not None and pts >= 0 and pts < (1 << 63):
                self._frames_by_pts[pts] = image
                self._pts_order.append(pts)
                while len(self._pts_order) > 30:
                    old = self._pts_order.pop(0)
                    self._frames_by_pts.pop(old, None)

    def _enforce_unique_identities(
        self, frame_number: int, results: dict[int, dict[str, Any]]
    ) -> None:
        by_name: dict[str, list[tuple[int, float]]] = {}
        for track_id in results:
            name = self._track_names.get(track_id, "unknown")
            if name != "unknown":
                by_name.setdefault(name, []).append(
                    (track_id, self._track_scores.get(track_id, 0.0))
                )
        for name, candidates in by_name.items():
            if len(candidates) < 2:
                continue
            winner = max(candidates, key=lambda item: item[1])[0]
            for loser, _ in candidates:
                if loser == winner:
                    continue
                self._clear_identity(loser, frame_number, f"duplicate_identity:{name}")
                results[loser] = {
                    "track_id": loser,
                    "camera": self.camera_id,
                    "name": "unknown",
                    "score": 0.0,
                    "state": "unknown",
                }

    def _decode_bgrx(self, buffer: Any, width: int, height: int) -> np.ndarray | None:
        if not hasattr(buffer, "map"):
            return None
        try:
            import gi
            gi.require_version("Gst", "1.0")
            from gi.repository import Gst
            success, mapped = buffer.map(Gst.MapFlags.READ)
            if not success:
                return None
            try:
                expected = width * height * 4
                if mapped.size < expected or height <= 0:
                    return None
                pixels = mapped.size // 4
                if mapped.size % 4:
                    return None
                aspect = width / height
                actual_height = max(1, int(round((pixels / aspect) ** 0.5)))
                actual_width = pixels // actual_height
                if actual_width * actual_height != pixels:
                    return None
                self._last_decode_info = {"mapped_size": int(mapped.size), "width": actual_width, "height": actual_height, "metadata_width": width, "metadata_height": height}
                raw = np.frombuffer(mapped.data, dtype=np.uint8, count=mapped.size)
                bgrx = raw.reshape((actual_height, actual_width, 4))
                return bgrx[:, :, :3].copy()
            finally:
                buffer.unmap(mapped)
        except Exception as exc:
            LOG.debug("CPU BGRx frame mapping failed: %s", exc)
            return None

    def decode_bgrx_frame(self, buffer: Any, width: int, height: int) -> np.ndarray | None:
        """Expose the stride-aware CPU frame decoder to other ROI stages."""
        return self._decode_bgrx(buffer, width, height)

    def process_frame(
        self,
        frame: np.ndarray,
        persons: list[tuple[int, float, float, float, float]],
        frame_number: int,
    ) -> dict[int, dict[str, Any]]:
        """Run recognition on a copied frame outside the GStreamer pad probe."""
        if not self.enabled or frame is None or frame.size == 0:
            return {}
        results: dict[int, dict[str, Any]] = {}
        active_track_ids: set[int] = set()
        due_track_ids: set[int] = set()
        now = self.recognition_scheduler.now()
        for track_id, left, top, right, bottom in persons:
            track_id = int(track_id)
            if track_id in {0, _UNTRACKED}:
                continue
            active_track_ids.add(track_id)
            bbox = [int(left), int(top), int(right), int(bottom)]
            previous_seen = self._track_last_seen.get(track_id)
            if previous_seen is not None and frame_number - previous_seen > self.max_disappeared:
                self._finish_track(track_id, frame_number)
                previous_seen = None
            if previous_seen is None:
                self._start_track(track_id, frame_number, bbox)
            else:
                self._track_last_seen[track_id] = frame_number
            self._track_observations[track_id] = self._track_observations.get(track_id, 0) + 1
            if track_id in self._track_names:
                self._track_last_confirmed[track_id] = frame_number
            results[track_id] = {
                "track_id": track_id,
                "camera": self.camera_id,
                "name": self._track_names.get(track_id, "unknown"),
                "score": self._track_scores.get(track_id, 0.0),
                "state": (
                    "recognized"
                    if track_id in self._track_names
                    and self._track_names[track_id] != "unknown"
                    else "unknown"
                ),
            }
            attempt_limit = (
                self.max_attempts_after_recognition
                if track_id in self._track_locked
                else self.max_attempts
            )
            if (
                self._track_attempts.get(track_id, 0) < attempt_limit
                and self.recognition_scheduler.due(track_id, now)
            ):
                due_track_ids.add(track_id)
        for track_id in list(self._track_last_seen):
            if (
                track_id not in active_track_ids
                and frame_number - self._track_last_seen[track_id] > self.max_disappeared
            ):
                self._finish_track(track_id, frame_number)
        if not due_track_ids:
            return results
        for track_id in due_track_ids:
            self.recognition_scheduler.mark_attempt(track_id, now)
        self.last_processed_frame = frame
        self.last_processed_frame_number = frame_number
        output_width = self.output_width or frame.shape[1]
        output_height = self.output_height or frame.shape[0]
        scale_x = frame.shape[1] / output_width if output_width > 0 else 1.0
        scale_y = frame.shape[0] / output_height if output_height > 0 else 1.0
        for track_id, left_value, top_value, right_value, bottom_value in persons:
            track_id = int(track_id)
            if track_id in {0, _UNTRACKED}:
                continue
            result = {
                "track_id": track_id,
                "camera": self.camera_id,
                "name": self._track_names.get(track_id, "unknown"),
                "score": self._track_scores.get(track_id, 0.0),
                "state": (
                    "recognized"
                    if track_id in self._track_names
                    and self._track_names[track_id] != "unknown"
                    else "unknown"
                ),
                "attempted": False,
            }
            results[track_id] = result
            if track_id not in due_track_ids:
                continue
            self._track_attempts[track_id] = self._track_attempts.get(track_id, 0) + 1
            result["attempted"] = True
            raw_left = int(left_value * scale_x)
            raw_top = int(top_value * scale_y)
            raw_right = int(right_value * scale_x)
            raw_bottom = int(bottom_value * scale_y)
            pad_x = max(16, int((raw_right - raw_left) * 0.20))
            pad_y = max(16, int((raw_bottom - raw_top) * 0.20))
            left = max(0, raw_left - pad_x)
            top = max(0, raw_top - pad_y)
            right = min(frame.shape[1], raw_right + pad_x)
            bottom = min(frame.shape[0], raw_bottom + pad_y)
            person = frame[top:bottom, left:right]
            if person.size == 0 or person.shape[0] * person.shape[1] < self.min_area:
                continue
            detected = self._detect(person)
            if detected is None:
                self._trace(
                    track_id,
                    {
                        "event": "attempt",
                        "frame": frame_number,
                        "result": "no_face",
                        "person_bbox": [left, top, right, bottom],
                    },
                )
                self._record_face_miss(track_id, frame_number, "no_face")
                continue
            if not self.gallery:
                self._trace(
                    track_id,
                    {
                        "event": "attempt",
                        "frame": frame_number,
                        "result": "unknown",
                        "reason": "gallery_empty",
                        "face_bbox": detected[:4].astype(int).tolist(),
                        "face_score": float(detected[-1]),
                        "gallery_count": 0,
                        "person_bbox": [left, top, right, bottom],
                    },
                )
                continue
            embedding = self._embedding(self._align(person, detected))
            self._observe_identity(track_id, frame_number, embedding)
            if (
                track_id in self._track_locked
                and self._track_identity_switches.get(track_id, 0)
                >= self.identity_switch_frames
            ):
                self._clear_identity(track_id, frame_number, "persistent_face_mismatch")
                self._observe_identity(track_id, frame_number, embedding)
            count = self._track_embedding_counts.get(track_id, 0)
            mean = self._track_embedding_means.get(track_id)
            if mean is None or count <= 0:
                mean = embedding.copy()
                count = 0
            else:
                mean = mean * count + embedding
            count += 1
            mean /= max(1, count)
            mean_norm = float(np.linalg.norm(mean))
            if mean_norm > 0:
                mean /= mean_norm
            self._track_embedding_means[track_id] = mean
            self._track_embedding_counts[track_id] = count
            best_label, best_score, best_cosine = "unknown", 0.0, 0.0
            for entry in self.gallery:
                cosine = float(np.dot(embedding, entry.embedding))
                confidence = _similarity_to_confidence(cosine)
                if confidence > best_score:
                    best_label, best_score, best_cosine = entry.label, confidence, cosine
            current_name = self._track_names.get(track_id, "unknown")
            if best_score <= self.unknown_score:
                self._record_face_miss(track_id, frame_number, "below_threshold")
            stable_label, stable_score = "unknown", 0.0
            if current_name != "unknown":
                if (
                    best_label != current_name
                    and best_score > self.unknown_score
                    and best_score >= self.threshold
                ):
                    if self._track_alternative_names.get(track_id) == best_label:
                        self._track_alternative_counts[track_id] = (
                            self._track_alternative_counts.get(track_id, 0) + 1
                        )
                    else:
                        self._track_alternative_names[track_id] = best_label
                        self._track_alternative_counts[track_id] = 1
                    if self._track_alternative_counts[track_id] >= self.identity_switch_frames:
                        self._reset_identity(track_id, frame_number, embedding)
                        stable_label, stable_score = self._record_candidate(
                            track_id, best_label, best_score, person.shape[0] * person.shape[1]
                        )
                    else:
                        stable_label, stable_score = (
                            current_name,
                            self._track_scores.get(track_id, 0.0),
                        )
                else:
                    self._track_alternative_names.pop(track_id, None)
                    self._track_alternative_counts[track_id] = 0
                    stable_label, stable_score = (
                        current_name,
                        self._track_scores.get(track_id, 0.0),
                    )
            elif best_score > self.unknown_score:
                stable_label, stable_score = self._record_candidate(
                    track_id, best_label, best_score, person.shape[0] * person.shape[1]
                )
            if stable_label != "unknown":
                self._track_last_confirmed[track_id] = frame_number
                self._track_face_misses[track_id] = 0
            result.update(
                {
                    "name": stable_label,
                    "score": stable_score,
                    "state": "recognized" if stable_label != "unknown" else "unknown",
                }
            )
            self._trace(
                track_id,
                {
                    "event": "attempt",
                    "frame": frame_number,
                    "result": best_label if best_score > self.unknown_score else "unknown",
                    "best_label": best_label,
                    "stable_result": stable_label,
                    "score": best_score,
                    "stable_score": stable_score,
                    "cosine": best_cosine,
                    "face_score": float(detected[-1]),
                    "face_bbox": detected[:4].astype(int).tolist(),
                    "gallery_count": len(self.gallery),
                    "observations": self._track_observations.get(track_id, 0),
                    "person_bbox": [left, top, right, bottom],
                },
            )
        self._enforce_unique_identities(frame_number, results)
        return results

    def process(self, buffer: Any, frame_meta: Any, frame_number: int) -> dict[int, dict[str, Any]]:
        if not self.enabled:
            return {}
        results: dict[int, dict[str, Any]] = {}
        active_track_ids: set[int] = set()
        due_track_ids: set[int] = set()
        now = self.recognition_scheduler.now()
        node = frame_meta.obj_meta_list
        while node is not None:
            try:
                import pyds
                obj = pyds.NvDsObjectMeta.cast(node.data)
            except StopIteration:
                break
            except Exception:
                node = node.next
                continue
            node = node.next
            if str(getattr(obj, "obj_label", "")) != "person":
                continue
            track_id = int(obj.object_id)
            if track_id in {0, _UNTRACKED}:
                continue
            active_track_ids.add(track_id)
            bbox = [int(obj.rect_params.left), int(obj.rect_params.top), int(obj.rect_params.left + obj.rect_params.width), int(obj.rect_params.top + obj.rect_params.height)]
            previous_seen = self._track_last_seen.get(track_id)
            if previous_seen is not None and frame_number - previous_seen > self.max_disappeared:
                self._finish_track(track_id, frame_number)
                previous_seen = None
            if previous_seen is None:
                self._start_track(track_id, frame_number, bbox)
            else:
                self._track_last_seen[track_id] = frame_number
            self._track_observations[track_id] = self._track_observations.get(track_id, 0) + 1
            if track_id in self._track_names:
                self._track_last_confirmed[track_id] = frame_number
            results[track_id] = {"track_id": track_id, "camera": self.camera_id, "name": self._track_names.get(track_id, "unknown"), "score": self._track_scores.get(track_id, 0.0), "state": "recognized" if track_id in self._track_names and self._track_names[track_id] != "unknown" else "unknown"}
            attempt_limit = (
                self.max_attempts_after_recognition
                if track_id in self._track_locked
                else self.max_attempts
            )
            if (
                self._track_attempts.get(track_id, 0) < attempt_limit
                and self.recognition_scheduler.due(track_id, now)
            ):
                due_track_ids.add(track_id)
        for track_id in list(self._track_last_seen):
            if track_id not in active_track_ids and frame_number - self._track_last_seen[track_id] > self.max_disappeared:
                self._finish_track(track_id, frame_number)
        if not due_track_ids:
            return results
        # Mark scheduled attempts before mapping the buffer. If a frame cannot
        # be mapped, the track still waits for the next cadence window instead
        # of retrying on every incoming frame.
        for track_id in due_track_ids:
            self.recognition_scheduler.mark_attempt(track_id, now)
        width = int(getattr(frame_meta, "source_frame_width", 0) or 0)
        height = int(getattr(frame_meta, "source_frame_height", 0) or 0)
        frame = self._decode_bgrx(buffer, width, height) if width > 0 and height > 0 else None
        pts = int(getattr(buffer, "pts", -1))
        if frame is None:
            frame = self._frames_by_pts.get(pts)
        if frame is None and self._pts_order:
            nearest = min(self._pts_order, key=lambda value: abs(value - pts))
            if abs(nearest - pts) <= 500_000_000:
                frame = self._frames_by_pts.get(nearest)
        if frame is None:
            frame = self.latest_frame
        if frame is None:
            return {}
        self.last_processed_frame = frame
        self.last_processed_frame_number = frame_number
        output_width = self.output_width or frame.shape[1]
        output_height = self.output_height or frame.shape[0]
        scale_x = frame.shape[1] / output_width if output_width > 0 else 1.0
        scale_y = frame.shape[0] / output_height if output_height > 0 else 1.0
        node = frame_meta.obj_meta_list
        while node is not None:
            try:
                import pyds
                obj = pyds.NvDsObjectMeta.cast(node.data)
            except StopIteration:
                break
            except Exception:
                node = node.next
                continue
            node = node.next
            if str(getattr(obj, "obj_label", "")) != "person":
                continue
            track_id = int(obj.object_id)
            if track_id in {0, _UNTRACKED}:
                continue
            result = {"track_id": track_id, "camera": self.camera_id, "name": self._track_names.get(track_id, "unknown"), "score": self._track_scores.get(track_id, 0.0), "state": "recognized" if track_id in self._track_names and self._track_names[track_id] != "unknown" else "unknown", "attempted": False}
            results[track_id] = result
            if track_id not in due_track_ids:
                continue
            self._track_attempts[track_id] = self._track_attempts.get(track_id, 0) + 1
            result["attempted"] = True
            raw_left = int(obj.rect_params.left * scale_x)
            raw_top = int(obj.rect_params.top * scale_y)
            raw_right = int((obj.rect_params.left + obj.rect_params.width) * scale_x)
            raw_bottom = int((obj.rect_params.top + obj.rect_params.height) * scale_y)
            pad_x = max(16, int((raw_right - raw_left) * 0.20))
            pad_y = max(16, int((raw_bottom - raw_top) * 0.20))
            left = max(0, raw_left - pad_x)
            top = max(0, raw_top - pad_y)
            right = min(frame.shape[1], raw_right + pad_x)
            bottom = min(frame.shape[0], raw_bottom + pad_y)
            person = frame[top:bottom, left:right]
            if person.size == 0 or person.shape[0] * person.shape[1] < self.min_area:
                continue
            # The person detector already limits the search area. Avoid a
            # full-frame face pass and run the face detector only on this ROI.
            detected = self._detect(person)
            if detected is None:
                self._trace(track_id, {"event": "attempt", "frame": frame_number, "result": "no_face", "person_bbox": [left, top, right, bottom]})
                self._record_face_miss(track_id, frame_number, "no_face")
                continue
            if not self.gallery:
                face_box = detected[:4].astype(int).tolist()
                self._trace(track_id, {"event": "attempt", "frame": frame_number, "result": "unknown", "reason": "gallery_empty", "face_bbox": face_box, "face_score": float(detected[-1]), "gallery_count": 0, "person_bbox": [left, top, right, bottom]})
                continue
            embedding = self._embedding(self._align(person, detected))
            self._observe_identity(track_id, frame_number, embedding)
            if (
                track_id in self._track_locked
                and self._track_identity_switches.get(track_id, 0)
                >= self.identity_switch_frames
            ):
                self._clear_identity(track_id, frame_number, "persistent_face_mismatch")
                self._observe_identity(track_id, frame_number, embedding)
            count = self._track_embedding_counts.get(track_id, 0)
            mean = self._track_embedding_means.get(track_id)
            if mean is None or count <= 0:
                mean = embedding.copy()
                count = 0
            else:
                mean = mean * count + embedding
            count += 1
            mean /= max(1, count)
            mean_norm = float(np.linalg.norm(mean))
            if mean_norm > 0:
                mean /= mean_norm
            self._track_embedding_means[track_id] = mean
            self._track_embedding_counts[track_id] = count
            best_label, best_score, best_cosine = "unknown", 0.0, 0.0
            for entry in self.gallery:
                cosine = float(np.dot(embedding, entry.embedding))
                confidence = _similarity_to_confidence(cosine)
                if confidence > best_score:
                    best_label, best_score, best_cosine = entry.label, confidence, cosine
            current_name = self._track_names.get(track_id, "unknown")
            if best_score <= self.unknown_score:
                self._record_face_miss(track_id, frame_number, "below_threshold")
            stable_label, stable_score = "unknown", 0.0
            if current_name != "unknown":
                if best_label != current_name and best_score > self.unknown_score and best_score >= self.threshold:
                    if self._track_alternative_names.get(track_id) == best_label:
                        self._track_alternative_counts[track_id] = self._track_alternative_counts.get(track_id, 0) + 1
                    else:
                        self._track_alternative_names[track_id] = best_label
                        self._track_alternative_counts[track_id] = 1
                    if self._track_alternative_counts[track_id] >= self.identity_switch_frames:
                        self._reset_identity(track_id, frame_number, embedding)
                        stable_label, stable_score = self._record_candidate(
                            track_id, best_label, best_score, person.shape[0] * person.shape[1]
                        )
                    else:
                        stable_label, stable_score = current_name, self._track_scores.get(track_id, 0.0)
                else:
                    self._track_alternative_names.pop(track_id, None)
                    self._track_alternative_counts[track_id] = 0
                    stable_label, stable_score = current_name, self._track_scores.get(track_id, 0.0)
            elif best_score > self.unknown_score:
                stable_label, stable_score = self._record_candidate(track_id, best_label, best_score, person.shape[0] * person.shape[1])
            if stable_label != "unknown":
                self._track_last_confirmed[track_id] = frame_number
                self._track_face_misses[track_id] = 0
            result.update({"name": stable_label, "score": stable_score, "state": "recognized" if stable_label != "unknown" else "unknown"})
            self._trace(track_id, {"event": "attempt", "frame": frame_number, "result": best_label if best_score > self.unknown_score else "unknown", "best_label": best_label, "stable_result": stable_label, "score": best_score, "stable_score": stable_score, "cosine": best_cosine, "face_score": float(detected[-1]), "face_bbox": detected[:4].astype(int).tolist(), "gallery_count": len(self.gallery), "observations": self._track_observations.get(track_id, 0), "person_bbox": [left, top, right, bottom]})
            obj.text_params.display_text = f"person {stable_label} {stable_score:.2f}"
        self._enforce_unique_identities(frame_number, results)
        return results

    def current_label(self, track_id: int, frame_number: int | None = None) -> tuple[str, float]:
        if track_id in {0, _UNTRACKED}:
            return "unknown", 0.0
        if track_id not in self._track_last_seen:
            return "unknown", 0.0
        return self._track_names.get(track_id, "unknown"), self._track_scores.get(track_id, 0.0)

    def close(self) -> None:
        for track_id in list(self._track_last_seen):
            self._finish_track(track_id, self._track_last_seen[track_id])
