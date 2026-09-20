"""Owner UI: static shell at ``/ui`` and its read-only JSON API at ``/api/ui``.

The shell is public because a browser navigation cannot carry the admin header;
it holds no data. Every JSON route sits behind the same admin dependency as the
rest of the config API, resolves the deployment owner on the server, and never
writes. Adding a page means one route here, one read model in
``ui_queries`` and one module under ``ui/pages``.
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
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from lib_application.services.account_onboarding import owner_scope
from lib_common.env_utils import parse_bool_env

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

    router = APIRouter(prefix="/api/ui", dependencies=[Depends(require_admin)], tags=["owner-ui"])

    @router.get("/overview")
    def overview() -> dict[str, Any]:
        with _owner_session() as (session, owner_id):
            return ui_queries.overview(session, owner_id, safety=safety)

    @router.get("/version")
    def version() -> dict[str, Any]:
        with session_factory() as session:
            return ui_queries.version(session, image=image)

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
