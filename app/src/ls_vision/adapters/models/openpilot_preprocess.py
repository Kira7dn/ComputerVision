"""Bounded NumPy/OpenCV port of openpilot road-model preprocessing."""

from __future__ import annotations

import cv2
import numpy as np

from ls_vision.domain.front_assistance import FrontCalibration

MODEL_WIDTH = 512
MODEL_HEIGHT = 256

VIEW_FROM_DEVICE = np.array(
    [[0.0, 1.0, 0.0], [0.0, 0.0, 1.0], [1.0, 0.0, 0.0]],
    dtype=np.float32,
)
MEDMODEL_INTRINSICS = np.array(
    [[910.0, 0.0, 256.0], [0.0, 910.0, 47.6], [0.0, 0.0, 1.0]],
    dtype=np.float32,
)
SBIGMODEL_INTRINSICS = np.array(
    [[455.0, 0.0, 256.0], [0.0, 455.0, 151.8], [0.0, 0.0, 1.0]],
    dtype=np.float32,
)


def _rotation_from_euler(rpy: tuple[float, float, float]) -> np.ndarray:
    roll, pitch, yaw = rpy
    sr, cr = np.sin(roll), np.cos(roll)
    sp, cp = np.sin(pitch), np.cos(pitch)
    sy, cy = np.sin(yaw), np.cos(yaw)
    return np.array(
        [
            [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
            [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
            [-sp, cp * sr, cp * cr],
        ],
        dtype=np.float32,
    )


def warp_matrix(calibration: FrontCalibration, *, big: bool) -> np.ndarray:
    intrinsics = np.asarray(calibration.intrinsics, dtype=np.float32)
    model_intrinsics = SBIGMODEL_INTRINSICS if big else MEDMODEL_INTRINSICS
    # openpilot/common/transformations/model.py builds model-frame-from-calib
    # as K_model @ view_from_device, then inverts that complete transform.
    # Matrix multiplication is not commutative; reversing these operands warps
    # the camera frame into an unrelated projective space.
    calib_from_model = np.linalg.inv(model_intrinsics @ VIEW_FROM_DEVICE)
    camera_from_calib = intrinsics @ VIEW_FROM_DEVICE @ _rotation_from_euler(calibration.rpy_calib)
    return camera_from_calib @ calib_from_model


def _bgr_to_i420_planes(frame: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    height, width = frame.shape[:2]
    packed = cv2.cvtColor(frame, cv2.COLOR_BGR2YUV_I420).reshape(-1)
    y_size = height * width
    uv_size = y_size // 4
    y = packed[:y_size].reshape(height, width)
    u = packed[y_size : y_size + uv_size].reshape(height // 2, width // 2)
    v = packed[y_size + uv_size : y_size + (2 * uv_size)].reshape(height // 2, width // 2)
    return y, u, v


def _frames_to_tensor(y: np.ndarray, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    return np.concatenate(
        (y[0::2, 0::2], y[1::2, 0::2], y[0::2, 1::2], y[1::2, 1::2], u, v),
        axis=0,
    ).reshape(6, MODEL_HEIGHT // 2, MODEL_WIDTH // 2)


def warp_transforms(
    calibration: FrontCalibration,
    *,
    big: bool,
) -> tuple[np.ndarray, np.ndarray]:
    """Build full-resolution and chroma transforms once per calibration."""
    matrix = warp_matrix(calibration, big=big)
    uv_scale = np.array(
        [[0.5, 0.0, 0.0], [0.0, 0.5, 0.0], [0.0, 0.0, 1.0]],
        dtype=np.float32,
    )
    return matrix, uv_scale @ matrix @ np.linalg.inv(uv_scale)


def _warp_model_planes(
    planes: tuple[np.ndarray, np.ndarray, np.ndarray],
    transforms: tuple[np.ndarray, np.ndarray],
) -> np.ndarray:
    y, u, v = planes
    matrix, uv_matrix = transforms
    y_warped = cv2.warpPerspective(
        y,
        matrix,
        (MODEL_WIDTH, MODEL_HEIGHT),
        flags=cv2.INTER_NEAREST | cv2.WARP_INVERSE_MAP,
        borderMode=cv2.BORDER_CONSTANT,
    )
    u_warped = cv2.warpPerspective(
        u,
        uv_matrix,
        (MODEL_WIDTH // 2, MODEL_HEIGHT // 2),
        flags=cv2.INTER_NEAREST | cv2.WARP_INVERSE_MAP,
        borderMode=cv2.BORDER_CONSTANT,
    )
    v_warped = cv2.warpPerspective(
        v,
        uv_matrix,
        (MODEL_WIDTH // 2, MODEL_HEIGHT // 2),
        flags=cv2.INTER_NEAREST | cv2.WARP_INVERSE_MAP,
        borderMode=cv2.BORDER_CONSTANT,
    )
    return _frames_to_tensor(y_warped, u_warped, v_warped).astype(
        np.uint8,
        copy=False,
    )


def prepare_model_frames(
    frame: np.ndarray,
    calibration: FrontCalibration,
    *,
    narrow_transforms: tuple[np.ndarray, np.ndarray] | None = None,
    wide_transforms: tuple[np.ndarray, np.ndarray] | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Prepare narrow and wide tensors with one full-frame color conversion."""
    if frame.ndim != 3 or frame.shape[2] < 3:
        raise ValueError("front model frame must be HxWx3 BGR")
    if frame.shape[1] != calibration.source_width or frame.shape[0] != calibration.source_height:
        raise ValueError("front frame resolution does not match calibration")
    planes = _bgr_to_i420_planes(np.ascontiguousarray(frame[:, :, :3]))
    narrow = narrow_transforms or warp_transforms(calibration, big=False)
    wide = wide_transforms or warp_transforms(calibration, big=True)
    return _warp_model_planes(planes, narrow), _warp_model_planes(planes, wide)


def prepare_model_frame(
    frame: np.ndarray,
    calibration: FrontCalibration,
    *,
    big: bool,
) -> np.ndarray:
    if frame.ndim != 3 or frame.shape[2] < 3:
        raise ValueError("front model frame must be HxWx3 BGR")
    if frame.shape[1] != calibration.source_width or frame.shape[0] != calibration.source_height:
        raise ValueError("front frame resolution does not match calibration")
    planes = _bgr_to_i420_planes(np.ascontiguousarray(frame[:, :, :3]))
    return _warp_model_planes(planes, warp_transforms(calibration, big=big))
