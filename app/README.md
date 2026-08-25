# LS-Vision package

`app/src` is the canonical DeepStream runtime source root. `server/`
remains the independent ADAS/FTP/archive boundary.

Development runs against the native Jetson development service:

```powershell
npm install --prefix app/web
npm run dev
```

`dev` starts source synchronization, the Jetson development service, SSH tunnels
and local Vite HMR. The isolated runtime uses `/opt/ls-vision-dev` and does not
share production evidence/state directories.

The three package entrypoints are:

```powershell
npm run dev
npm run check
npm run deploy -- -JetsonAlias jetson-nano
```

Add `-Development` to `deploy` when publishing the isolated Jetson development
service. Production deploy uses the default `deploy-app` action.

Run the package test suite and the real Compose E2E separately:

```powershell
python -m pytest app/tests -q
python app/tests/e2e/run_camera_safety_e2e.py
```

The E2E report is accepted only when the Docker health, MediaMTX HLS output,
three worker freshness, event API, and restart persistence gates all pass.

Models and the face library are read-only named volumes. Evidence, SQLite
state, queue, and logs are writable named volumes; they are not stored on
`/mnt/d` or in the source checkout.
