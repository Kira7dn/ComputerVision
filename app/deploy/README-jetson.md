# Jetson deployment

The Jetson profile runs only `camera_dahua` through the standalone DeepStream
runtime. It expects the Dahua LAN address to be `192.168.1.229` and the Jetson
Ethernet interface to be `192.168.1.10/24`.

The runtime is deliberately separate from the LeOS `tbox.service`. Its local
dashboard/API is exposed on port `18080`; MediaMTX publishes the annotated
stream on the local RTSP/WebRTC/HLS ports.

## Deployment entrypoints

The source of truth remains `Camera/app`, while LeOS owns the Jetson deploy
contract. Both supported commands call the same `tbox_lab deploy-app` path:

From the LeOS repository:

```powershell
.\services\tbox\factory\tbox_lab.ps1 deploy-app `
  -JetsonAlias jetson-default `
  -CameraRoot D:\BusinessAnalyze\Camera
```

From the Camera repository:

```powershell
Set-Location D:\BusinessAnalyze\Camera
npm run deploy:jetson
```

The Camera command is a wrapper only; it does not create a second deployment
implementation and it does not start the legacy Docker Compose runtime.
Before packaging, the deploy step builds `app/web` and the native API serves
the generated `app/web/dist` bundle on port `18080`.
