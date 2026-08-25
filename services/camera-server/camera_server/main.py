"""Production entrypoint for the complete camera runtime."""

from .runtime import run

if __name__ == "__main__":
    raise SystemExit(run())
