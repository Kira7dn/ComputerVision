# LS-Vision package

`app/src` is the canonical DeepStream runtime source root. `server/`
remains the independent ADAS/FTP/archive boundary.

Development uses native WSL:

```powershell
npm run wsl:start
npm run wsl:status
npm run wsl:pause
npm run wsl:stop
```

Native WSL development includes Vite HMR for `app/web` and backend hot reload for `app/src`,
`app/config`, `.env.local` and the development MediaMTX configuration.
`wsl:pause` stops only the development runtime and keeps Ubuntu running. `wsl:stop` shuts down
all WSL distros, including Docker Desktop's WSL backend.

Production uses Docker Compose on WSL2 with NVIDIA Container Toolkit:

```powershell
npm run docker:config
npm run docker:build
npm run docker:start
```

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
