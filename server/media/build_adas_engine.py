#!/usr/bin/env python3
"""Build the trusted fixed-shape YOLOv8n TensorRT FP16 engine for this GPU."""

import argparse
import hashlib
import json
from pathlib import Path

import tensorrt as trt
from ultralytics import YOLO


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--weights', default='yolov8n.pt')
    parser.add_argument('--output-dir', default=str(Path(__file__).resolve().parent.parent / 'models'))
    args = parser.parse_args()

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    model = YOLO(args.weights)
    exported = Path(model.export(
        format='onnx', imgsz=(384, 640), batch=1, dynamic=False,
        simplify=False, opset=18, device=0, half=True,
    )).resolve()
    target = output_dir / 'yolov8n.engine'
    logger = trt.Logger(trt.Logger.WARNING)
    builder = trt.Builder(logger)
    # TensorRT 11 removed weak-typing precision flags. Export an FP16 ONNX and
    # preserve its types with a strongly-typed network.
    network = builder.create_network(
        1 << int(trt.NetworkDefinitionCreationFlag.STRONGLY_TYPED)
    )
    parser_ = trt.OnnxParser(network, logger)
    if not parser_.parse(exported.read_bytes()):
        errors = '\n'.join(str(parser_.get_error(i)) for i in range(parser_.num_errors))
        raise RuntimeError(f'TensorRT ONNX parse failed:\n{errors}')
    config = builder.create_builder_config()
    config.set_memory_pool_limit(trt.MemoryPoolType.WORKSPACE, 1024 * 1024 * 1024)
    serialized = builder.build_serialized_network(network, config)
    if serialized is None:
        raise RuntimeError('TensorRT failed to build the serialized engine')
    target.write_bytes(serialized)
    manifest = {
        'model': target.name,
        'sha256': sha256(target),
        'source_weights': str(args.weights),
        'source_onnx': exported.name,
        'tensorrt_version': trt.__version__,
        'precision': 'fp16',
        'batch': 1,
        'input_shape': [1, 3, 384, 640],
    }
    (output_dir / 'yolov8n.engine.json').write_text(
        json.dumps(manifest, indent=2), encoding='utf-8'
    )
    print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    main()
