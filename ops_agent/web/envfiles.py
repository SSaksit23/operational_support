"""Read KEY=VALUE files next to the app (.env.local from `neon deploy`, .env.ops for our own keys).

Values already set in the environment win, so Cloud Run settings are never overridden.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FILES = (".env.ops", ".env.local", ".env")


def load(root: Path = ROOT) -> None:
    for name in FILES:
        p = root / name
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.removeprefix("export ").strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)
    # Neon writes DATABASE_URL; the store reads OPS_DB_DSN.
    if not os.environ.get("OPS_DB_DSN") and os.environ.get("DATABASE_URL"):
        os.environ["OPS_DB_DSN"] = os.environ["DATABASE_URL"]
