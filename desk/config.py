from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from .usage import PRICES


def _chat_model(raw: str | None) -> str:
    """DESK_CHAT_MODEL: only a model that has a price in desk/usage.py, so every call's cost is logged. That is Claude Haiku 5.5
    alone. A bad value stops the desk at startup, naming the setting."""
    model = raw or "claude-haiku-5-5"
    if model not in PRICES:
        raise ValueError(f"DESK_CHAT_MODEL must be one of: {', '.join(PRICES)}")
    return model


def _text_numbers(raw: str | None) -> dict[str, str]:
    """DESK_TEXT_NUMBERS: a JSON object from a receiving number to a facility id. A bad value stops the desk at startup, naming
    the setting, instead of failing later inside a webhook or a worker pass."""
    try:
        value = json.loads(raw or "{}")
    except ValueError as exc:
        raise ValueError("DESK_TEXT_NUMBERS is not valid JSON") from exc
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise ValueError("DESK_TEXT_NUMBERS must be a JSON object that maps a number to a facility id")
    return value


def _two_units(raw: str | None) -> tuple[str, str] | None:
    """FMS_CHECKS_LIVE_UNITS: two unit ids, comma separated. Anything else stops the desk at startup, naming the setting."""
    if not raw:
        return None
    units = tuple(u.strip() for u in raw.split(","))
    if len(units) != 2 or not all(units) or units[0] == units[1]:
        raise ValueError("FMS_CHECKS_LIVE_UNITS must name two different unit ids, comma separated")
    return units[0], units[1]


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
    # which facility a receiving number belongs to, for the numbers Cubby sends without a facility (a number shared by several)
    text_numbers: dict[str, str] = field(default_factory=dict)
    # in the preview, any visitor may start a live run on a seeded text, within these limits ($1 a day in all,
    # ten full cases per visitor a day). Without a model key there is no run, whatever this says.
    public_investigate: bool = False
    daily_budget_usd: float = 1.0
    visitor_cases_per_day: int = 10
    # the bot check in front of the two public write paths (desk/guard.py); it is on only when both keys are set
    turnstile_sitekey: str | None = None
    turnstile_secret: str | None = None
    # the one address the check must have been passed on; the check is on only with all three set, and the desk never takes it from the request
    turnstile_host: str | None = None
    # the live access check on the Evaluation page: two unit ids on an FMS test account (one locked, one open), and credentials that
    # can read units and nothing else. Unset, the check is off.
    live_check_units: tuple[str, str] | None = None
    live_check_client_id: str | None = field(default=None, repr=False)
    live_check_client_secret: str | None = field(default=None, repr=False)

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
            chat_model=_chat_model(os.environ.get("DESK_CHAT_MODEL")),
            staff_token=os.environ.get("DESK_STAFF_TOKEN") or None,
            public_view=os.environ.get("DESK_PUBLIC_VIEW") == "1",
            demo_seed=os.environ.get("DESK_DEMO_SEED") == "1",
            run_id=os.environ.get("DESK_RUN_ID", "local"),
            marketplace_url=os.environ.get("DESK_MARKETPLACE_URL") or None,
            webhook_token=os.environ.get("DESK_WEBHOOK_TOKEN") or None,
            text_numbers=_text_numbers(os.environ.get("DESK_TEXT_NUMBERS")),
            public_investigate=os.environ.get("DESK_PUBLIC_INVESTIGATE") == "1",
            daily_budget_usd=float(os.environ.get("DESK_DAILY_BUDGET_USD", "1")),
            visitor_cases_per_day=int(os.environ.get("DESK_VISITOR_CASES_PER_DAY", "10")),
            turnstile_sitekey=os.environ.get("DESK_TURNSTILE_SITEKEY") or None,
            turnstile_secret=os.environ.get("DESK_TURNSTILE_SECRET") or None,
            turnstile_host=os.environ.get("DESK_TURNSTILE_HOST") or None,
            live_check_units=_two_units(os.environ.get("FMS_CHECKS_LIVE_UNITS")),
            live_check_client_id=os.environ.get("STORA_READ_CLIENT_ID") or None,
            live_check_client_secret=os.environ.get("STORA_READ_CLIENT_SECRET") or None,
        )
