# LS-Vision package

`app/src` is the canonical DeepStream runtime source root. `server/`
remains the independent ADAS/FTP/archive boundary.

Development uses native WSL:

```powershell
.\deploy\powershell\start.ps1 -Action start -Mode Dev
.\deploy\powershell\status.ps1 -Mode Dev
.\deploy\powershell\stop.ps1 -Mode Dev
```

Production uses Docker Compose on WSL2 with NVIDIA Container Toolkit:

```powershell
docker compose -f .\deploy\docker\compose.yaml config
.\deploy\powershell\start.ps1 -Action start -Mode Production
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
