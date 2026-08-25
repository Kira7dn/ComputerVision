# Jetson deployment

The Jetson profile runs only `DMS` through the standalone DeepStream
runtime. It expects the Dahua LAN address to be `192.168.1.229` and the Jetson
Ethernet interface to be `192.168.1.10/24`.

The runtime is deliberately separate from the LeOS `tbox.service`. Its local
dashboard/API is exposed on port `18080`; MediaMTX publishes the annotated
stream on the local RTSP/WebRTC/HLS ports.

`DMS` uses the DMS profile from the LeOS reference. The DeepStream
worker keeps RTSP decode, person detection/tracking, NVOSD and MediaMTX, while
the DMS adapter runs the Chaitanya and Soham ONNX models plus MediaPipe
FaceMesh. It publishes the canonical alerts `Smoking`, `Drinking`, `Eating`,
`Phone Usage`, `Distracted`, `Drowsy`, `Yawning`, `Eyes Closed`, `Head Away`,
and `No Seatbelt`, with the reference 3-frame-on/2-frame-off smoother. DMS
state, metrics and boxes are available from `/api/live-metadata` and
`/api/metrics`. Each smoothed alert is also persisted as a DMS `START`,
`UPDATE`, `END` event with evidence snapshots; DMS events deliberately do not
emit Telegram notifications.

## Deployment entrypoints

The source of truth remains `Camera/app`, while LeOS owns the Jetson deploy
contract. Both supported commands call the same `tbox_lab deploy-app` path:

From the LeOS repository:

```powershell
.\services\tbox\factory\tbox_lab.ps1 deploy-app `
  -JetsonAlias jetson-nano `
  -CameraRoot D:\BusinessAnalyze\Camera
```

From the Camera repository:

```powershell
Set-Location D:\BusinessAnalyze\Camera
npm run deploy
```

This is the one-step deploy for a new T-Box. It deploys the LeOS T-Box
application, WiFi/GPIO services, native DeepStream/MediaMTX runtime, models,
dashboard bundle and systemd units. The command succeeds only after
`tbox.service`, `tbox-gpio.service`, the Camera service, `/health/ready`, every
configured camera worker and its HLS stream are ready. The default SSH target
is `jetson-nano`; override it explicitly with `-JetsonAlias` when provisioning
another named device.

The Camera command is a wrapper only; it does not create a second deployment
implementation and it does not start the legacy Docker Compose runtime.
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

`ls-vision-dev.service` runs the backend, runner and MediaMTX on the Jetson.
`jetson_sync.py` watches `Camera/app` and synchronizes source/config changes
over SSH; the backend supervisor restarts the API and workers automatically.
Vite runs locally on port `5173`, proxies the API and stream through SSH
tunnels to the Jetson's ports `18080`, `8888` and `8889`, and provides frontend
HMR. The development service uses
`/opt/ls-vision-dev` for source, state, evidence and logs, while reusing the
native DeepStream/model runtime under `/opt/ls-vision`.

Do not run the production `ls-vision.service` or another development runtime
at the same time as the Jetson development service, because they compete for
the camera and ports. Stop the local hot-reload session with `Ctrl+C`; the
remote service can be stopped with `ssh jetson-nano 'sudo systemctl stop
ls-vision-dev.service'` before returning to production.
