#!/usr/bin/env python3
"""Print non-secret runtime settings as shell-safe assignments.

Service scripts execute this with the project's venv Python from the repository
root, so pydantic-settings applies the exact same environment and .env rules as
the application. No token or provider key is exposed.
"""
from __future__ import annotations

import shlex

from app.config import get_settings


def main() -> None:
    settings = get_settings()
    values = {
        "INSTITUTE_RUNTIME_HOME": str(settings.home_dir.resolve()),
        "INSTITUTE_RUNTIME_HOST": settings.host,
        "INSTITUTE_RUNTIME_PORT": str(settings.port),
    }
    for name, value in values.items():
        print(f"{name}={shlex.quote(value)}")


if __name__ == "__main__":
    main()
