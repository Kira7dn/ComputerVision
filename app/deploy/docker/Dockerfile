ARG DEEPSTREAM_IMAGE=nvcr.io/nvidia/deepstream:7.1-gc-triton-devel@sha256:a658c53e66901295683ae4dc5afd8fec8779e2e0da1e44040e16540f49b2473e
ARG TARGET_PLATFORM=generic
ARG ONNXRUNTIME_INDEX_URL=https://pypi.jetson-ai-lab.io/jp6/cu126

FROM node:22-bookworm-slim AS web-build

WORKDIR /opt/ls-vision-web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM ${DEEPSTREAM_IMAGE}

# Build arguments declared before FROM are only available while resolving the
# base image. Re-declare the target-specific arguments in the final stage so
# Jetson receives the aarch64 runtime dependencies.
ARG TARGET_PLATFORM=generic
ARG ONNXRUNTIME_INDEX_URL=https://pypi.jetson-ai-lab.io/jp6/cu126

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/opt/camera-safety/app/src \
    CAMERA_CONFIG=/opt/camera-safety/config/production.yaml \
    CAMERA_RUNTIME_ROOT=/opt/camera-safety \
    CAMERA_WEB_ROOT=/opt/camera-safety/web \
    GST_PLUGIN_PATH=/opt/nvidia/deepstream/deepstream-7.1/lib/gst-plugins:/usr/lib/aarch64-linux-gnu/gstreamer-1.0 \
    GST_PLUGIN_SYSTEM_PATH_1_0=/usr/lib/aarch64-linux-gnu/gstreamer-1.0 \
    LD_LIBRARY_PATH=/usr/lib/aarch64-linux-gnu:/usr/local/lib/python3.10/dist-packages/nvidia/cudnn/lib:/usr/local/lib/python3.10/dist-packages/nvidia/cublas/lib:/opt/nvidia/deepstream/deepstream-7.1/lib

WORKDIR /opt/camera-safety/app
RUN sed -i 's|http://ports.ubuntu.com|https://ports.ubuntu.com|g' /etc/apt/sources.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends --reinstall \
        gstreamer1.0-libav libavcodec58 libavutil56 libavformat58 libavfilter7 \
        libswscale5 libpostproc55 libvpx7 libflac8 libmp3lame0 libsndfile1 \
        libx264-163 libx265-199 libxvidcore4 libchromaprint1 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements-runtime.txt ./requirements-runtime.txt
COPY requirements-runtime-jetson.txt ./requirements-runtime-jetson.txt
RUN if [ "$TARGET_PLATFORM" = "jetson" ]; then \
      python3 -m pip install --no-cache-dir -r requirements-runtime-jetson.txt && \
      python3 -m pip install --no-cache-dir --extra-index-url "$ONNXRUNTIME_INDEX_URL" "onnxruntime-gpu==1.23.0"; \
    else \
      python3 -m pip install --no-cache-dir -r requirements-runtime.txt; \
    fi
COPY pyproject.toml ./pyproject.toml
COPY src ./src
RUN python3 -m pip install --no-cache-dir --no-deps .
COPY config /opt/camera-safety/config
COPY --from=web-build /opt/ls-vision-web/dist /opt/camera-safety/web

HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=6 \
  CMD curl -fsS http://127.0.0.1:18080/health/live >/dev/null || exit 1

ENTRYPOINT ["python3", "-m", "container"]
