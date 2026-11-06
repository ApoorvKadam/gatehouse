from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    fms_base_url: str
    fms_api_key: str
    fms_timeout_seconds: float
    reconcile_every_seconds: float
    operator_token: str | None
    # only for running on a laptop: without a token the API refuses every request unless this is set
    allow_without_token: bool = False

    @staticmethod
    def from_env() -> "Settings":
        return Settings(
            database_url=os.environ.get("DESK_DATABASE_URL", "sqlite:///./data/desk.db"),
            fms_base_url=os.environ.get("DESK_FMS_BASE_URL", "http://localhost:8100"),
            fms_api_key=os.environ.get("DESK_FMS_API_KEY", "sim_org_northstar_full"),
            fms_timeout_seconds=float(os.environ.get("DESK_FMS_TIMEOUT_SECONDS", "5")),
            reconcile_every_seconds=float(os.environ.get("DESK_RECONCILE_EVERY_SECONDS", "10")),
            operator_token=os.environ.get("DESK_OPERATOR_TOKEN") or None,
            allow_without_token=os.environ.get("DESK_ALLOW_WITHOUT_TOKEN") == "1",
        )
