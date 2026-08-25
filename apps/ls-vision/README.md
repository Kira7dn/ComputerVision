# LS-Vision

LS-Vision là runtime DeepStream canonical dưới package `src/ls_vision`.

```text
ls_vision.service
  ├─ ls_vision.interfaces.dashboard_api
  ├─ ls_vision.runner
  │   └─ ls_vision.application.camera_runtime per non-media-only camera
  └─ ls_vision.interfaces.mock_media_server when configured
```

## Commands

```powershell
npm test
npm run check
npm run dev
npm run deploy
npm run deploy -- -Action status
npm run deploy -- -Action rollback
```

`dev.yaml` và `production.yaml` là hai profile standalone. Runtime data không nằm trong source release.

## Layer ownership

- `domain`: value objects, policies và transition contracts; không import outer layers.
- `application`: scheduling, orchestration, ports và process entrypoints.
- `adapters`: DeepStream, models, persistence, notifications và media implementations.
- `interfaces`: HTTP/dashboard/ingress boundaries.
- `bootstrap`: config, paths, lifecycle và composition.

`media_only` là feed playback hiện hữu, không phải vision worker.
