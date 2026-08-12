# Integration tests

Integration tests exercise multiple real Frigate components together. The current runtime
integration entrypoint is in `tools/tests/e2e/`; focused Frigate component integration tests
remain under `frigate/tests/` because they share that package's fixtures and import root.
