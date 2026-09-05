"""API request/response schemas for the scoring engine.

Pydantic models that describe the FastAPI surface area. Kept separate
from ``api.py`` so the route handlers in ``api.py`` can stay focused on
orchestration without competing with model definitions for screen
real estate.

Schema versioning lives in :data:`SIGNAL_SCHEMA_VERSION` — clients send
the version they expect via the ``X-Signal-Schema-Version`` header.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Schema metadata
# ---------------------------------------------------------------------------

#: Current API contract version. Bumped when the wire format changes
#: incompatibly; clients announce what they expect via the
#: ``X-Signal-Schema-Version`` header so the server can warn on drift.
SIGNAL_SCHEMA_VERSION = "v1"

#: Actions safe to persist into the canonical-signals table. The DB
#: ``CHECK`` constraint enforces the same set; keeping this in code lets
#: the API surface return a 400 *before* hitting the DB.
VALID_DB_ACTIONS = {"long", "short", "flat", "hold", "open_spread", "close_spread"}

#: Strategy type the signal-ingest endpoints accept. Anything else is
#: rejected with HTTP 422 to avoid silently storing typos.
VALID_STRATEGY_TYPES = {"indicator"}


# ---------------------------------------------------------------------------
# Strategy insight payloads (``POST /api/v1/signals``)
# ---------------------------------------------------------------------------


class SignalInsight(BaseModel):
    """Direction/magnitude block of a strategy signal request."""

    direction: str = Field(..., pattern="^(Up|Down|Flat)$")
    magnitude: float = Field(default=0.0, ge=0.0, le=1.0)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    horizon: str = Field(default="1D")


class InsightSignalPayload(BaseModel):
    """Provider-neutral wire format posted by :class:`HttpSignalEmitter`."""

    ts: str  # ISO timestamp string
    strategy_id: str
    symbol: str
    insight: SignalInsight
    run_id: str | None = None  # Top-level for cross-container correlation
    context: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Bindings, scores, instruments
# ---------------------------------------------------------------------------


class BindingResponse(BaseModel):
    """Read-only admin response for user-strategy bindings.

    Threshold fields align with the DB schema
    (``UserStrategyBinding``):
    - ``asset_score_threshold`` — required, default ``0.6``.
    - ``sector_score_threshold`` — optional.
    - ``market_score_threshold`` — optional.
    """

    user_id: str
    strategy_id: str | None = None  # NULL = any strategy
    asset_score_threshold: float = 0.6
    asset_filter: list[str] = Field(default_factory=list)
    sector_filter: list[str] = Field(default_factory=list)
    execution_mode: str | None = None
    sizing_profile: dict[str, Any] = Field(default_factory=dict)
    risk_caps: dict[str, Any] = Field(default_factory=dict)
    sector_score_threshold: float | None = None
    market_score_threshold: float | None = None  # 3-tier threshold support
    allowed_brokers: list[str] = Field(default_factory=list)
    autopilot: bool = False  # True = auto-execute; False = manual approval
    entries_enabled: bool = False
    exits_enabled: bool = False
    # Policy-based mode selection (BD-3). Leave ``execution_mode`` unset and set
    # these to let best/auto ranking pick among the permitted modes by policy.
    # An explicit ``execution_mode`` takes precedence and forces a fixed mode.
    execution_modes_allowed: list[str] = Field(default_factory=list)
    mode_selection_policy: str | None = None  # best_return / lowest_risk / highest_sharpe
    preferred_mode: str | None = None


class ScoreResponse(BaseModel):
    """Score payload returned by ingest + score-query endpoints."""

    target: str
    scope: str
    score: float
    computed_at: datetime
    components: list[dict[str, Any]]
    metadata: dict[str, Any]


class InstrumentConfig(BaseModel):
    """Read-only instrument hierarchy returned by the operator API."""

    symbol: str
    settlement_currency: str
    asset_class: str | None = None
    sector: str | None = None
    industry: str | None = None
    index: str | None = None


class OutboxRedriveRequest(BaseModel):
    """Fenced operator request for one dead-lettered execution command."""

    reason: str = Field(min_length=1, max_length=1000)
    expected_generation: int = Field(ge=0)


__all__ = [
    "SIGNAL_SCHEMA_VERSION",
    "VALID_DB_ACTIONS",
    "VALID_STRATEGY_TYPES",
    "BindingResponse",
    "InsightSignalPayload",
    "InstrumentConfig",
    "OutboxRedriveRequest",
    "ScoreResponse",
    "SignalInsight",
]
