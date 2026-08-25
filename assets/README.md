# Workspace assets

Local media and deployable model inputs used by Camera development and acceptance tooling.

- `assets/models/`: deployable detector and recognition model artifacts only.
- `assets/face_library/`: local face enrollment library.
- `assets/fixtures/mock_videos/`: deterministic local media fixtures.

Assets are workspace-level inputs shared by development and acceptance tooling; runtime data is stored outside the source checkout.
Upstream model source and training runs belong under `training/vendor/` and `training/runs/`.
