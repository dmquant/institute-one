"""`python -m app` — run the server."""
from __future__ import annotations

import argparse

import uvicorn

from .config import get_settings


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(prog="institute-one")
    parser.add_argument("--host", default=settings.host)
    parser.add_argument("--port", type=int, default=settings.port)
    parser.add_argument("--reload", action="store_true", help="dev only — never when daemonized")
    args = parser.parse_args()
    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
