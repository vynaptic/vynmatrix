"""Paper-only adaptations of the established account and binding services."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from lib_application.db.models import Broker, LinkedBrokerAccount, UserStrategyBinding
from lib_application.services import binding_control
from lib_application.services.account_onboarding import (
    BrokerAccountIn,
    onboard_account,
    owner_scope,
)
from lib_application.services.binding_control import BindingControlError
from lib_application.services.control_audit import ERROR, append_audit
from lib_application.services.research_admission import (
    ResearchWorkflowError,
    require_research_admission,
)
from lib_common.logging import get_logger

from . import ui_queries
from .research_http import check_research_queries, read_research_body, research_response
from .ui_api import BindIn, BindingPatchIn, _safety_snapshot

logger = get_logger(__name__)


class PaperAccountIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config_key: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    base_ccy: str = Field(pattern=r"^[A-Z][A-Z0-9]{2,9}$")
    paper_initial_equity: Decimal = Field(gt=0)
    paper_initial_cash: Decimal = Field(ge=0)


def _paper_account(session: Any, owner_id: str, account_id: int) -> None:
    found = session.scalar(
        select(LinkedBrokerAccount.account_id)
        .join(Broker, Broker.broker_id == LinkedBrokerAccount.broker_id)
        .where(
            LinkedBrokerAccount.account_id == account_id,
            LinkedBrokerAccount.user_id == owner_id,
            LinkedBrokerAccount.environment == "paper",
            Broker.code == "paper",
        )
    )
    if found is None:
        raise HTTPException(status_code=404, detail="Local paper account not found")


def _paper_snapshot(session: Any, owner_id: str, safety: dict[str, Any]) -> dict[str, Any]:
    control = ui_queries.control(session, owner_id)
    accounts = control["accounts"]
    if accounts is None:
        raise HTTPException(status_code=503, detail="Paper account state unavailable")
    accounts = [
        row for row in accounts if row["broker_code"] == "paper" and row["environment"] == "paper"
    ]
    ids = {row["account_id"] for row in accounts}
    control = {
        **control,
        "accounts": accounts,
        "bindings": None
        if control["bindings"] is None
        else [row for row in control["bindings"] if row["account_id"] in ids],
    }
    strategies = ui_queries.strategies(session, owner_id)
    strategies["strategies"] = [
        {
            **row,
            "bindings": [binding for binding in row["bindings"] if binding["account_id"] in ids],
        }
        for row in strategies["strategies"]
    ]
    pnl = ui_queries.pnl(session, owner_id, days=90)
    pnl["accounts"] = [row for row in pnl["accounts"] if row["account_id"] in ids]
    fills = ui_queries.fills(session, owner_id, limit=50, before=None, account_ids=ids)
    return research_response(
        safety=safety, control=control, strategies=strategies, pnl=pnl, fills=fills
    )


def register_research_paper(
    app: FastAPI,
    *,
    session_factory: Callable[[], Any],
    require_admin: Callable[..., None],
    secrets_provider: Any,
) -> None:
    router = APIRouter(
        prefix="/api/ui/research/paper", dependencies=[Depends(require_admin)], tags=["owner-paper"]
    )
    safety = _safety_snapshot()

    @contextmanager
    def owner_session() -> Iterator[tuple[Any, str]]:
        try:
            with session_factory() as session, owner_scope(session) as owner_id:
                yield session, owner_id
        except ResearchWorkflowError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    def mutate(
        operation: Callable[[Any, str], dict[str, Any]],
        *,
        action: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            with owner_session() as (session, owner_id):
                require_research_admission(session)
                result = operation(session, owner_id)
                session.commit()
                return research_response(**result)
        except BindingControlError as exc:
            # The failed write was rolled back. Preserve its original refusal
            # and record only bounded field metadata in a separate transaction.
            if action is not None:
                try:
                    with owner_session() as (session, owner_id):
                        append_audit(
                            session,
                            user_id=owner_id,
                            action=action,
                            request_payload=metadata or {},
                            response_payload={"detail": exc.detail},
                            status=ERROR,
                        )
                        session.commit()
                except (SQLAlchemyError, ValueError, RuntimeError):
                    logger.warning(
                        "Paper binding refusal was not audited", action=action, exc_info=True
                    )
            raise

    def require_paper() -> None:
        if safety["execution_mode"] != "paper" or safety["allow_live"]:
            raise HTTPException(
                status_code=409,
                detail="Paper controls require paper mode with the live gate disabled",
            )

    @router.get("")
    def paper(request: Request) -> dict[str, Any]:
        check_research_queries(request, set())
        with owner_session() as (session, owner_id):
            return _paper_snapshot(session, owner_id, safety)

    @router.post("/accounts")
    async def account_create(request: Request) -> dict[str, Any]:
        check_research_queries(request, set())
        require_paper()
        body = await read_research_body(request, PaperAccountIn, limit=16 * 1024)
        try:
            payload = BrokerAccountIn(
                **body.model_dump(exclude_none=True), broker_code="paper", environment="paper"
            )
        except ValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail="Paper cash must not exceed equity; all account fields must be valid",
            ) from exc
        result: dict[str, Any] = await run_in_threadpool(
            mutate,
            lambda session, _owner_id: {
                "account": onboard_account(session, payload, secrets_provider).model_dump(
                    mode="json"
                )
            },
        )
        return result

    @router.post("/bindings")
    async def binding_create(request: Request) -> dict[str, Any]:
        check_research_queries(request, set())
        require_paper()
        payload = await read_research_body(request, BindIn, limit=4096)

        def apply(session: Any, owner_id: str) -> dict[str, Any]:
            _paper_account(session, owner_id, payload.broker_account_id)
            row = binding_control.create_binding(
                session,
                owner_id=owner_id,
                strategy_id=payload.strategy_id,
                broker_account_id=payload.broker_account_id,
                mode=payload.mode,
            )
            return {"binding": {"binding_id": row.binding_id, "mode": binding_control.mode_of(row)}}

        result: dict[str, Any] = await run_in_threadpool(
            mutate,
            apply,
            action="binding.create",
            metadata={"fields": ["mode"], "strategy_id": payload.strategy_id, "mode": payload.mode},
        )
        return result

    @router.post("/bindings/{binding_id}")
    async def binding_update(binding_id: int, request: Request) -> dict[str, Any]:
        check_research_queries(request, set())
        require_paper()
        payload = await read_research_body(request, BindingPatchIn, limit=16 * 1024)

        def apply(session: Any, owner_id: str) -> dict[str, Any]:
            account_id = session.scalar(
                select(UserStrategyBinding.broker_account_id).where(
                    UserStrategyBinding.binding_id == binding_id,
                    UserStrategyBinding.user_id == owner_id,
                )
            )
            if account_id is None:
                raise HTTPException(status_code=404, detail="Paper binding not found")
            _paper_account(session, owner_id, account_id)
            row = binding_control.patch_binding(
                session,
                owner_id=owner_id,
                binding_id=binding_id,
                expected=payload.expected,
                changes=payload.changes,
            )
            return {"binding": {"binding_id": row.binding_id, "mode": binding_control.mode_of(row)}}

        result: dict[str, Any] = await run_in_threadpool(
            mutate,
            apply,
            action="binding.patch",
            metadata={"fields": sorted(payload.changes), "binding_id": binding_id},
        )
        return result

    app.include_router(router)
