"""Authenticated research market imports and immutable dataset HTTP adapters."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from lib_application.db.models.research_data import ResearchBar
from lib_application.services import research_data
from lib_application.services.account_onboarding import owner_scope
from lib_application.services.research_admission import ResearchWorkflowError

from .research_http import (
    check_research_queries,
    read_research_body,
    research_response,
)


class ImportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metadata: dict[str, Any]
    csv: str


class FreezeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    import_id: str = Field(min_length=36, max_length=36)
    expected_hash: str = Field(pattern="^[0-9a-f]{64}$")
    name: str = Field(min_length=1, max_length=200)


class SnapshotIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metadata: dict[str, Any]
    name: str = Field(min_length=1, max_length=200)


def register_research_market(
    app: FastAPI, *, session_factory: Callable[[], Any], require_admin: Callable[..., None]
) -> None:
    """Register owner-resolved routes without importing or executing editor code."""
    router = APIRouter(
        prefix="/api/ui/research",
        dependencies=[Depends(require_admin)],
        tags=["owner-research-data"],
    )

    @contextmanager
    def owner_session() -> Iterator[tuple[Any, str]]:
        try:
            with session_factory() as session, owner_scope(session) as owner_id:
                yield session, owner_id
        except (research_data.ResearchDataError, ResearchWorkflowError) as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    @router.get("/market")
    def market(
        request: Request,
        instrument_id: int | None = Query(None, gt=0),
        source: str | None = Query(None, max_length=50),
        timeframe: str | None = Query(None, max_length=20),
        before: str | None = Query(None, max_length=256),
        limit: int = Query(50, ge=1, le=1000),
        search: str | None = Query(None, max_length=100),
    ) -> dict[str, Any]:
        check_research_queries(
            request, {"instrument_id", "source", "timeframe", "before", "limit", "search"}
        )
        with owner_session() as (session, owner_id):
            return research_response(
                **research_data.market_data(
                    session,
                    owner_id,
                    instrument_id=instrument_id,
                    source=source,
                    timeframe=timeframe,
                    before=before,
                    limit=limit,
                    search=search,
                )
            )

    @router.post("/imports")
    async def upload(request: Request) -> dict[str, Any]:
        check_research_queries(request, set())
        payload = await read_research_body(request, ImportIn)
        try:
            content = payload.csv.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise HTTPException(status_code=422, detail="CSV must be valid UTF-8") from exc

        def transaction() -> dict[str, Any]:
            with owner_session() as (session, owner_id):
                result = research_data.import_dataset(session, owner_id, payload.metadata, content)
                session.commit()
                return research_response(**{"import": result})

        result: dict[str, Any] = await run_in_threadpool(transaction)
        return result

    @router.post("/datasets")
    async def freeze(request: Request) -> dict[str, Any]:
        check_research_queries(request, set())
        payload = await read_research_body(request, FreezeIn, limit=16 * 1024)

        def transaction() -> dict[str, Any]:
            with owner_session() as (session, owner_id):
                result = research_data.freeze_dataset(
                    session, owner_id, payload.import_id, payload.expected_hash, payload.name
                )
                session.commit()
                return research_response(dataset=result)

        result: dict[str, Any] = await run_in_threadpool(transaction)
        return result

    @router.post("/datasets/snapshot")
    async def snapshot(request: Request) -> dict[str, Any]:
        check_research_queries(request, set())
        payload = await read_research_body(request, SnapshotIn, limit=16 * 1024)
        if "name" in payload.metadata:
            raise HTTPException(
                status_code=422, detail="Snapshot name belongs in the request envelope"
            )

        def transaction() -> dict[str, Any]:
            with owner_session() as (session, owner_id):
                result = research_data.snapshot_dataset(
                    session, owner_id, {**payload.metadata, "name": payload.name}
                )
                session.commit()
                if result.get("dataset", "frozen") is None:
                    return research_response(**result)
                return research_response(dataset=result)

        result: dict[str, Any] = await run_in_threadpool(transaction)
        return result

    @router.get("/datasets")
    def datasets(
        request: Request,
        after: str | None = Query(None, max_length=256),
        limit: int = Query(50, ge=1, le=1000),
    ) -> dict[str, Any]:
        check_research_queries(request, {"after", "limit"})
        with owner_session() as (session, owner_id):
            return research_response(**research_data.list_datasets(session, owner_id, after, limit))

    @router.get("/datasets/{dataset_id}")
    def dataset(
        request: Request,
        dataset_id: str,
        include_bars: bool = False,
        after: int = Query(-1, ge=-1, le=99999),
        limit: int = Query(500, ge=1, le=1000),
    ) -> dict[str, Any]:
        check_research_queries(request, {"include_bars", "after", "limit"})
        with owner_session() as (session, owner_id):
            result = research_data.get_dataset(session, owner_id, dataset_id)
            values: dict[str, Any] = {"dataset": result}
            if include_bars:
                rows = session.execute(
                    select(ResearchBar.ordinal, ResearchBar.bar_json)
                    .where(
                        ResearchBar.owner_id == owner_id,
                        ResearchBar.dataset_id == dataset_id,
                        ResearchBar.ordinal > after,
                    )
                    .order_by(ResearchBar.ordinal)
                    .limit(limit + 1)
                ).all()
                values.update(
                    bars=[row.bar_json for row in rows[:limit]],
                    next_after=rows[limit - 1].ordinal if len(rows) > limit else None,
                )
            return research_response(**values)

    @router.get("/datasets/{dataset_id}/export")
    def export(request: Request, dataset_id: str) -> dict[str, Any]:
        check_research_queries(request, set())
        with owner_session() as (session, owner_id):
            return research_response(**research_data.export_dataset(session, owner_id, dataset_id))

    app.include_router(router)


__all__ = ["register_research_market"]
