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
    chat_model: str = "claude-haiku-5-5"
    # resolving a payment held for staff needs this second token as well; without it the route is closed
    staff_token: str | None = None
    # the hosted preview: anyone may read without a token; every change still needs the operator token
    public_view: bool = False
    # build the synthetic preview world at startup when the database is empty (desk/demo.py)
    demo_seed: bool = False
    # names this database's work items, so a reset under a new run never collides with kept history
    run_id: str = "local"
    # an outside auction site's listing feed; unset, an outside listing's status is reported as unknown
    marketplace_url: str | None = None
    # the shared secret Cubby's SMS Received webhook must present; Cubby documents no signing. Unset, the receiver is closed
    webhook_token: str | None = None

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
            chat_model=os.environ.get("DESK_CHAT_MODEL", "claude-haiku-5-5"),
            staff_token=os.environ.get("DESK_STAFF_TOKEN") or None,
            public_view=os.environ.get("DESK_PUBLIC_VIEW") == "1",
            demo_seed=os.environ.get("DESK_DEMO_SEED") == "1",
            run_id=os.environ.get("DESK_RUN_ID", "local"),
            marketplace_url=os.environ.get("DESK_MARKETPLACE_URL") or None,
            webhook_token=os.environ.get("DESK_WEBHOOK_TOKEN") or None,
        )
