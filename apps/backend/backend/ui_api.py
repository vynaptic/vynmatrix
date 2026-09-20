"""Owner UI: static shell at ``/ui`` and its JSON API at ``/api/ui``.

The shell is public because a browser navigation cannot carry the admin header;
it holds no data. Every JSON route sits behind the same admin dependency as the
rest of the config API and resolves the deployment owner on the server; no route
accepts a caller-supplied user.

Most routes read. Three write, and they are the whole writable surface: the
owner may bind a released strategy to one of their own accounts and choose how
it trades. Nothing here places an order, holds a secret, or releases a strategy.
Every write names the field it changes, proves it saw the current value, and
leaves an audit row -- including when it is refused.

Adding a page means one route here, one read model in ``ui_queries`` and one
module under ``ui/pages``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from lib_application.services import binding_control
from lib_application.services.account_onboarding import owner_scope
from lib_application.services.binding_control import BindingControlError
from lib_application.services.control_audit import ERROR, append_audit
from lib_common.env_utils import parse_bool_env
from lib_common.logging import get_logger

from . import ui_queries

UI_DIRECTORY = Path(__file__).resolve().parent / "ui"
#: Written into the image at build time from the commit the build was given.
BUILD_INFO_PATH = Path(os.getenv("VM_BUILD_INFO_PATH") or "/app/BUILD_INFO.json")

# Same-origin only, no inline script or style: the shell ships no third-party
# code and must stay usable offline on the loopback address.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)

SessionFactory = Callable[[], Any]

logger = get_logger(__name__)


class BindIn(BaseModel):
    """Bind a strategy to an account. Everything else keeps its schema default."""

    model_config = ConfigDict(extra="forbid")
    strategy_id: str = Field(min_length=1, max_length=50)
    broker_account_id: int = Field(gt=0)
    mode: str


class BindingPatchIn(BaseModel):
    """A partial change, fenced by the value the owner believed each field held."""

    model_config = ConfigDict(extra="forbid")
    expected: dict[str, Any]
    changes: dict[str, Any]


class _UiFiles(StaticFiles):
    """Static shell with a Content-Security-Policy scoped to the UI only."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        return response


def _safety_snapshot() -> dict[str, Any]:
    """Execution-safety facts of this process, read once at composition time.

    The platform launcher pins these for every child; the UI only displays them.
    """
    return {
        "execution_mode": (os.getenv("EXECUTION_MODE") or "paper").strip().lower(),
        "allow_live": parse_bool_env("EXECUTION_ENGINE_ALLOW_LIVE", default=False),
        "environment": (os.getenv("ENVIRONMENT") or os.getenv("ENV") or "dev").strip().lower(),
    }


def _build_info() -> dict[str, Any] | None:
    """What this container was built from, read once at composition time.

    A development checkout run outside the image has no such file; the version
    endpoint then reports the record alone and says the image is unknown.
    """
    try:
        decoded = json.loads(BUILD_INFO_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(decoded, dict):
        return None
    return {
        "source_commit": str(decoded.get("source_commit") or ""),
        "source_dirty": str(decoded.get("source_dirty") or "") == "true",
        "build_time": str(decoded.get("build_time") or ""),
        "base_image": str(decoded.get("base_image") or ""),
    }


def register_ui(
    app: FastAPI,
    *,
    session_factory: SessionFactory,
    require_admin: Callable[..., None],
) -> None:
    """Mount the owner UI and its read API on the config API application."""
    safety = _safety_snapshot()
    image = _build_info()

    @contextmanager
    def _owner_session() -> Iterator[tuple[Any, str]]:
        with session_factory() as session, owner_scope(session) as owner_id:
            yield session, owner_id

    def _audit_refusal(action: str, payload: dict[str, Any], detail: str) -> None:
        """Record a refused write, which the rolled-back transaction cannot carry.

        Best effort by construction: a deployment whose audit write also fails
        must still return the original refusal to the owner, not a second error.
        """
        try:
            with session_factory() as session, owner_scope(session) as owner_id:
                append_audit(
                    session,
                    user_id=owner_id,
                    action=action,
                    request_payload=payload,
                    response_payload={"detail": detail},
                    status=ERROR,
                )
                session.commit()
        except (SQLAlchemyError, ValueError, RuntimeError):
            logger.warning("Owner UI refusal was not audited", action=action, exc_info=True)

    router = APIRouter(prefix="/api/ui", dependencies=[Depends(require_admin)], tags=["owner-ui"])

    @router.get("/overview")
    def overview() -> dict[str, Any]:
        with _owner_session() as (session, owner_id):
            return ui_queries.overview(session, owner_id, safety=safety)

    @router.get("/version")
    def version() -> dict[str, Any]:
        with session_factory() as session:
            return ui_queries.version(session, image=image)

    @router.get("/control")
    def control() -> dict[str, Any]:
        with _owner_session() as (session, owner_id):
            return ui_queries.control(session, owner_id)

    @router.post("/bindings")
    def bind(payload: BindIn) -> dict[str, Any]:
        """Bind a released strategy to one of the owner's connected accounts."""
        request = {"fields": ["mode"], "mode": payload.mode, "strategy_id": payload.strategy_id}
        try:
            with _owner_session() as (session, owner_id):
                row = binding_control.create_binding(
                    session,
                    owner_id=owner_id,
                    strategy_id=payload.strategy_id,
                    broker_account_id=payload.broker_account_id,
                    mode=payload.mode,
                )
                result = {"binding_id": int(row.binding_id), "mode": binding_control.mode_of(row)}
                session.commit()
                return result
        except BindingControlError as exc:
            _audit_refusal("binding.create", request, exc.detail)
            raise

    @router.post("/bindings/{binding_id}")
    def change_binding(binding_id: int, payload: BindingPatchIn) -> dict[str, Any]:
        """Change only the supplied fields, and only if they still hold their expected value."""
        request = {"fields": sorted(payload.changes), "binding_id": binding_id}
        try:
            with _owner_session() as (session, owner_id):
                row = binding_control.patch_binding(
                    session,
                    owner_id=owner_id,
                    binding_id=binding_id,
                    expected=payload.expected,
                    changes=payload.changes,
                )
                result = {"binding_id": int(row.binding_id), "mode": binding_control.mode_of(row)}
                session.commit()
                return result
        except BindingControlError as exc:
            _audit_refusal("binding.patch", request, exc.detail)
            raise

    @router.get("/strategies")
    def strategies() -> dict[str, Any]:
        with _owner_session() as (session, owner_id):
            return ui_queries.strategies(session, owner_id)

    @router.get("/pnl")
    def pnl(days: int = Query(default=90, ge=1, le=ui_queries.MAX_PNL_DAYS)) -> dict[str, Any]:
        with _owner_session() as (session, owner_id):
            return ui_queries.pnl(session, owner_id, days=days)

    @router.get("/fills")
    def fills(
        limit: int = Query(default=50, ge=1, le=ui_queries.MAX_FILLS_PAGE),
        before: str | None = Query(default=None, max_length=64),
    ) -> dict[str, Any]:
        try:
            cursor = ui_queries.parse_fill_cursor(before)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        with _owner_session() as (session, owner_id):
            return ui_queries.fills(session, owner_id, limit=limit, before=cursor)

    app.include_router(router)

    @app.get("/", include_in_schema=False)
    def open_ui() -> RedirectResponse:
        return RedirectResponse(url="/ui/")

    app.mount("/ui", _UiFiles(directory=UI_DIRECTORY, html=True), name="owner-ui")


__all__ = ["BUILD_INFO_PATH", "CONTENT_SECURITY_POLICY", "UI_DIRECTORY", "register_ui"]
