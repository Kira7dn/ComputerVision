# Test layout

Camera Safety tests now live under `app/tests`:

```powershell
python -m pytest app/tests -q
python app/tests/e2e/run_camera_safety_e2e.py
```

The old tracker/edge-runtime test runners were removed from this workspace.
`tools/` remains for model, fixture, and reporting utilities; it is
not the Camera Safety runtime test entrypoint.
