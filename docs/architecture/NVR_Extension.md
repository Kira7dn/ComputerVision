# NVR Extension

```text
config.yaml
    ↓
topology compiler
    ↓
recognition / tracker
    ↓
runtime
```

| Capability/tính năng | `frigate` | `camera-recognition` | `tracker` | `camera-ngrok` |
| --- | :---: | :---: | :---: | :---: |
| Camera ingest, FFmpeg và decode | ✅ | — | ✅ | — |
| Object detection | ✅ | — | ✅ | — |
| Track association và track lifecycle | ✅ | — | ✅ | — |
| Detection/Track result stream | ✅ | — | ✅ | — |
| Frame/track lineage (`camera_id`, `frame_seq`, `source_pts`, `edge_epoch`) | ✅ | — | ✅ | — |
| Face/LPR candidate | ✅ | ✅ | — | — |
| Face/LPR crop | ✅ | ✅ | — | — |
| Face/plate bbox | ✅ | ✅ | — | — |
| Face/LPR evidence | ✅ | ✅ | ✅ | — |
| Recognition job admission, sequence và epoch guard | ✅ | — | — | — |
| Face/LPR model inference | — | ✅ | — | — |
| Face/LPR history, voting và `RecognitionCore` | — | ✅ | — | — |
| Recognition outcome | ✅ | ✅ | — | — |
| Recognition outcome: nhận/validate/publication | ✅ | — | — | — |
| Event metadata mapping và publication guard | ✅ | — | — | — |
| Canonical Event commit và correlation | ✅ | — | — | — |
| API và SQLite | ✅ | — | — | — |
| Media, recording và review | ✅ | — | ✅ | — |
| Notification outbox/worker | ✅ | — | — | — |
| Bounded queue, backpressure và stale-drop | ✅ | ✅ | ✅ | — |
| Health/readiness, reconnect và typed failure | ✅ | ✅ | ✅ | — |
| gRPC/mTLS, node identity và certificate lifecycle | ✅ | ✅ | ✅ | — |
| Model/config/schema version và hash | ✅ | ✅ | ✅ | — |
| Runtime metrics, trace và resource telemetry | ✅ | ✅ | ✅ | — |
| HTTP tunnel / public access | — | — | — | ✅ |
