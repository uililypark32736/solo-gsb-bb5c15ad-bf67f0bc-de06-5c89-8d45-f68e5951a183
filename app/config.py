"""Runtime configuration via environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Settings:
    db_path: str = os.getenv("WAKE_DB_PATH", "/data/wake.db")
    app_name: str = "runway-wake-separation"
    host: str = os.getenv("WAKE_HOST", "0.0.0.0")
    port: int = int(os.getenv("WAKE_PORT", "8000"))


settings = Settings()
