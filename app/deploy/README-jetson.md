# Jetson deployment

Development and production both use one `DMS` worker from Dahua channel 5 plus
four synchronized mock views: `camera_front`, `camera_back`, `camera_left` and
`camera_right`. The front worker runs the openpilot road-model adapter in
camera-only shadow mode. DMS credentials remain environment-owned.

They are separate application endpoints. Development is served through Vite
at `http://127.0.0.1:5173`; production is served at `http://vision.local`.
Their deployment lifecycle and runtime directories differ. Existing source
mapping, topology and function ownership are endpoint contracts and must not
be rewritten while changing CV logic.

The runtime is deliberately separate from the LeOS `tbox.service`. Its local
dashboard/API is exposed on port `18080`; MediaMTX publishes the annotated
stream on the local RTSP/WebRTC/HLS ports.

`DMS` uses the DMS profile from the LeOS reference. The DeepStream
worker keeps RTSP decode, person detection/tracking, NVOSD and MediaMTX, while
the DMS adapter runs the single Soham ONNX model plus MediaPipe
FaceMesh. It publishes the canonical alerts `Smoking`, `Drinking`, `Eating`,
and `No Seatbelt`. Pose, eyes, phone, fatigue, missing-face and uncertain
observations feed one time-based awareness policy; critical attention creates
one `Driver Inattention` lifecycle with the current reasons in its metadata. DMS
state, metrics and boxes are available from `/api/live-metadata` and
`/api/metrics`. Each smoothed alert is also persisted as a DMS `START`,
`UPDATE`, `END` event with evidence snapshots; DMS events deliberately do not
emit Telegram notifications.

The DMS camera must enable the shared person detector even when its other
camera functions are disabled. Model detections cannot become public alerts
until they are associated with a current driver track and pass the temporal
event policy. Operator status is fail-closed: `MONITORING` means both driver
and face are visible, `PARTIAL` identifies a missing observation modality,
and `NO_DRIVER` replaces the old false-positive `OK` state. Raw detections stay
bounded diagnostics; confirmed overlay boxes are reduced to one strongest box
per canonical behavior and driver. Soham is the sole object-model evidence
source so one behavior is not inferred twice by overlapping models.

Cabin DMS uses the canonical Soham object model, FaceMesh metrics and the shared
`DriverAttentionPolicy`. No Openpilot cabin model or shadow inference is
packaged for Jetson. This does not affect the separate Openpilot-derived front
assistance model.

`camera_front` publishes the annotated `camera_front` RTSP/WebRTC/HLS stream,
lane/path/lead metadata and advisory-only `vision_ldw_left`,
`vision_ldw_right` and `vision_fcw` lifecycle events. It does not read CAN,
T-Box telemetry or vehicle-control state, and it never actuates the vehicle.
Its preprocessing uses the pinned openpilot warp order, lane and road-edge
outputs retain full XYZ geometry, and the overlay projects them with the same
intrinsic/extrinsic calibration. Lane geometry below probability `0.5` and
non-metric stretched paths are not rendered.
The TensorRT-to-CUDA provider fallback is observable in runtime status; CPU is
not permitted for Jetson shadow inference. Phase status and the remaining
hardware gates are canonical in `docs/architecture/Platform.md` section 13.

## Deployment entrypoints

The source of truth and the only LS-Vision deployment owner is `Camera/app`:

```powershell
Set-Location D:\BusinessAnalyze\Camera
npm run deploy
```

This command deploys the native DeepStream/MediaMTX runtime, models, dashboard
bundle and Camera-owned ingress/mDNS units. It never deploys or restarts
`tbox.service`/`tbox-gpio.service`. The accepted T-Box boundary release no
longer owns port 80, so Camera removes the retired
`50-ls-vision-ingress.conf` compatibility drop-in during production deploy.
Conversely, LeOS `tbox_lab deploy-app` must not install, stop, start, restart
or health-check LS-Vision/MediaMTX. The Camera
command succeeds only after `/health/ready`, every configured camera worker and
its HLS stream are ready. The default SSH target is `jetson-nano`.

The Camera command owns its deployment implementation and does not start the
legacy Docker Compose runtime.
Before packaging, the deploy step builds `app/web` and the native API serves
the generated `app/web/dist` bundle on port `18080`.

## Jetson development hot reload

The production deploy remains release-based. For hardware development, first
install the native runtime once, then deploy the isolated development service:

```powershell
npm run deploy -- -JetsonAlias jetson-nano
npm run deploy -- -Development -JetsonAlias jetson-nano
npm install --prefix app/web
npm run dev -- -JetsonAlias jetson-nano
```

`ls-vision-dev.service` runs the backend, runner and an isolated MediaMTX on the Jetson.
`jetson_sync.py` watches `Camera/app` and synchronizes source/config changes
over SSH; the backend supervisor restarts the API and workers automatically.
Vite runs locally on port `5173`, proxies local ports `18080`, `8888` and
`8889` through SSH to the Jetson development ports `28080`, `28888` and
`28889`, and provides frontend HMR. Development RTSP uses `28554`; production
keeps `8554`. The development service uses
`/opt/ls-vision-dev` for source, state, evidence and logs, while reusing the
native DeepStream/model runtime under `/opt/ls-vision`.

Production and development are designed to run concurrently without sharing
dashboard, RTSP, HLS, WebRTC or metadata ports. Production remains the owner of
`vision.local` throughout a development session. `ls-vision-dev.service` must
remain disabled at boot and is started only by `npm run dev`. Stop the local
hot-reload session with `Ctrl+C`; cleanup disables and stops only the remote
development service.
