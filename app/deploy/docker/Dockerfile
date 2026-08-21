ARG DEEPSTREAM_IMAGE=nvcr.io/nvidia/deepstream:7.1-gc-triton-devel@sha256:a658c53e66901295683ae4dc5afd8fec8779e2e0da1e44040e16540f49b2473e

FROM node:22-bookworm-slim AS web-build

WORKDIR /opt/ls-vision-web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM ${DEEPSTREAM_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/opt/camera-safety/app/src \
    CAMERA_CONFIG=/opt/camera-safety/config/production.yaml \
    CAMERA_RUNTIME_ROOT=/opt/camera-safety \
    CAMERA_WEB_ROOT=/opt/camera-safety/web \
    GST_PLUGIN_PATH=/opt/nvidia/deepstream/deepstream-7.1/lib/gst-plugins

WORKDIR /opt/camera-safety/app
COPY requirements-runtime.txt ./requirements-runtime.txt
RUN python3 -m pip install --no-cache-dir -r requirements-runtime.txt
COPY pyproject.toml ./pyproject.toml
COPY src ./src
RUN python3 -m pip install --no-cache-dir --no-deps .
COPY config /opt/camera-safety/config
COPY --from=web-build /opt/ls-vision-web/dist /opt/camera-safety/web

HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=6 \
  CMD curl -fsS http://127.0.0.1:18080/health/live >/dev/null || exit 1

ENTRYPOINT ["python3", "-m", "container"]
