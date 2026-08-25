"""Validate the deployment model contract and bounded CPU inference time."""

import argparse
import statistics
import sys
import time

import numpy as np
import onnxruntime as ort


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("model")
    parser.add_argument("--max-ms", type=float, default=200.0)
    args = parser.parse_args()

    session = ort.InferenceSession(args.model, providers=["CPUExecutionProvider"])
    model_input = session.get_inputs()[0]
    expected_shape = [1, 3, 320, 320]
    if model_input.shape != expected_shape:
        print(
            f"invalid model input: expected {expected_shape}, got {model_input.shape}",
            file=sys.stderr,
        )
        return 2

    tensor = np.zeros(expected_shape, dtype=np.float32)
    session.run(None, {model_input.name: tensor})
    durations = []
    for _ in range(5):
        started = time.perf_counter()
        session.run(None, {model_input.name: tensor})
        durations.append((time.perf_counter() - started) * 1000)

    median_ms = statistics.median(durations)
    print(f"input={model_input.shape} cpu_median_ms={median_ms:.2f}")
    if median_ms >= args.max_ms:
        print(
            f"CPU inference {median_ms:.2f} ms exceeds {args.max_ms:.2f} ms",
            file=sys.stderr,
        )
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
