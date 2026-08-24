"""``python -m straticate`` — run the backend using :class:`Settings`.

Kept to one line of behaviour on purpose: everything the entry point does
lives in :func:`straticate.main.serve`, so the module executed by ``-m`` and
the function a test calls are the same code path.
"""

from straticate.main import serve

if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    serve()
