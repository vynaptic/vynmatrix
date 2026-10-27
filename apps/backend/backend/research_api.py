"""Owner-scoped research authoring, durable jobs and immutable model registry."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from lib_application.services import (
    research_authoring,
    research_data,
    research_jobs,
    research_registry,
)
from lib_application.services.account_onboarding import owner_scope
from lib_application.services.research_authoring import ResearchWorkflowError

from .research_http import check_research_queries, read_research_body, research_response
from .research_market import register_research_market
from .research_paper import register_research_paper
from .research_templates import strategy_templates

SessionScope = Callable[[], AbstractContextManager[tuple[Any, str]]]
WriteOperation = Callable[[str, Callable[[Any, str], Any]], dict[str, Any]]


class DraftIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    source: str = Field(min_length=1, max_length=262144)
    config: dict[str, Any]


class RevisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=200)
    source: str = Field(min_length=1, max_length=262144)
    config: dict[str, Any]


class JobIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=200)
    kind: str = Field(min_length=1, max_length=30)
    inputs: dict[str, Any]


class RetryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=200)


class ModelIn(RetryIn):
    job_id: str = Field(min_length=36, max_length=36)
    name: str = Field(min_length=1, max_length=200)


def _workspace_snapshot(session: Any, owner_id: str) -> dict[str, Any]:
    datasets = research_data.list_datasets(session, owner_id)
    drafts = research_authoring.list_drafts(session, owner_id)
    jobs = research_jobs.list_jobs(session, owner_id)
    models = research_registry.list_models(session, owner_id)
    return research_response(
        datasets=datasets["datasets"],
        drafts=drafts["drafts"],
        jobs=jobs["jobs"],
        models=models["models"],
        supervisor=research_jobs.supervisor_status(session),
        next_after={
            "datasets": datasets.get("next_after"),
            "drafts": drafts.get("next_after"),
            "jobs": jobs.get("next_after"),
            "models": models.get("next_after"),
        },
        limits={"source_bytes": 262144, "active_jobs": 1, "wall_seconds": 300},
        research_only=True,
    )


def _register_model_routes(
    router: APIRouter, session_scope: SessionScope, write: WriteOperation
) -> None:
    @router.get("/models")
    def models_list(
        request: Request,
        after: str | None = Query(None, max_length=256),
        limit: int = Query(50, ge=1, le=100),
    ) -> dict[str, Any]:
        check_research_queries(request, {"after", "limit"})
        with session_scope() as (session, owner_id):
            return research_response(
                **research_registry.list_models(session, owner_id, after=after, limit=limit)
            )

    @router.post("/models")
    async def register_model(request: Request) -> dict[str, Any]:
        check_research_queries(request, set())
        payload = await read_research_body(request, ModelIn, limit=4096)
        result: dict[str, Any] = await run_in_threadpool(
            write,
            "model",
            lambda session, owner_id: research_registry.register_model(
                session, owner_id, **payload.model_dump()
            ),
        )
        return result

    @router.get("/models/{model_id}")
    def model_detail(model_id: str, request: Request) -> dict[str, Any]:
        check_research_queries(request, set())
        with session_scope() as (session, owner_id):
            return research_response(model=research_registry.get_model(session, owner_id, model_id))

    @router.get("/models/{model_id}/export")
    def model_export(model_id: str, request: Request) -> dict[str, Any]:
        check_research_queries(request, set())
        with session_scope() as (session, owner_id):
            return research_response(
                artifact=research_registry.export_model(session, owner_id, model_id)
            )


def register_research(
    app: FastAPI,
    *,
    session_factory: Callable[[], Any],
    require_admin: Callable[..., None],
    secrets_provider: Any = None,
) -> None:
    """Add data-only adapters; execution lives in the separately supervised runner."""
    register_research_market(app, session_factory=session_factory, require_admin=require_admin)
    router = APIRouter(
        prefix="/api/ui/research", dependencies=[Depends(require_admin)], tags=["owner-research"]
    )

    @contextmanager
    def session_scope() -> Iterator[tuple[Any, str]]:
        try:
            with session_factory() as session, owner_scope(session) as owner_id:
                yield session, owner_id
        except ResearchWorkflowError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    def write(label: str, operation: Callable[[Any, str], Any]) -> dict[str, Any]:
        # The session and its entire transaction belong to the worker thread.
        with session_scope() as (session, owner_id):
            result = operation(session, owner_id)
            session.commit()
            return research_response(**{label: result})

    @router.get("/workspace")
    def workspace(request: Request) -> dict[str, Any]:
        check_research_queries(request, set())
        with session_scope() as (session, owner_id):
            return _workspace_snapshot(session, owner_id)

    @router.get("/drafts")
    def drafts_list(
        request: Request,
        after: str | None = Query(None, max_length=256),
        limit: int = Query(50, ge=1, le=100),
    ) -> dict[str, Any]:
        check_research_queries(request, {"after", "limit"})
        with session_scope() as (session, owner_id):
            return research_response(
                **research_authoring.list_drafts(session, owner_id, after=after, limit=limit)
            )

    @router.get("/jobs")
    def jobs_list(
        request: Request,
        after: str | None = Query(None, max_length=256),
        limit: int = Query(50, ge=1, le=100),
    ) -> dict[str, Any]:
        check_research_queries(request, {"after", "limit"})
        with session_scope() as (session, owner_id):
            return research_response(
                **research_jobs.list_jobs(session, owner_id, after=after, limit=limit)
            )

    @router.get("/templates")
    def templates(request: Request) -> dict[str, Any]:
        check_research_queries(request, set())
        return research_response(templates=strategy_templates())

    @router.post("/drafts")
    async def save_draft(request: Request) -> dict[str, Any]:
        check_research_queries(request, set())
        payload = await read_research_body(request, DraftIn, limit=2 * 1024 * 1024)
        result: dict[str, Any] = await run_in_threadpool(
            write,
            "draft",
            lambda session, owner_id: research_authoring.create_draft(
                session, owner_id, **payload.model_dump()
            ),
        )
        return result

    @router.get("/drafts/{draft_id}")
    def draft_detail(draft_id: str, request: Request) -> dict[str, Any]:
        check_research_queries(request, set())
        with session_scope() as (session, owner_id):
            return research_response(
                draft=research_authoring.get_draft(session, owner_id, draft_id)
            )

    @router.get("/drafts/{draft_id}/export")
    def draft_export(
        draft_id: str,
        request: Request,
        revision_id: str | None = Query(None, min_length=36, max_length=36),
    ) -> dict[str, Any]:
        check_research_queries(request, {"revision_id"})
        with session_scope() as (session, owner_id):
            return research_response(
                **research_authoring.export_draft(
                    session, owner_id, draft_id, revision_id=revision_id
                )
            )

    @router.post("/drafts/{draft_id}/revisions")
    async def save_revision(draft_id: str, request: Request) -> dict[str, Any]:
        check_research_queries(request, set())
        payload = await read_research_body(request, RevisionIn, limit=2 * 1024 * 1024)
        result: dict[str, Any] = await run_in_threadpool(
            write,
            "draft",
            lambda session, owner_id: research_authoring.update_draft(
                session, owner_id, draft_id, **payload.model_dump()
            ),
        )
        return result

    @router.post("/jobs")
    async def enqueue(request: Request) -> dict[str, Any]:
        check_research_queries(request, set())
        payload = await read_research_body(request, JobIn, limit=64 * 1024)
        result: dict[str, Any] = await run_in_threadpool(
            write,
            "job",
            lambda session, owner_id: research_jobs.enqueue_job(
                session, owner_id, **payload.model_dump()
            ),
        )
        return result

    @router.get("/jobs/{job_id}")
    def job_detail(job_id: str, request: Request) -> dict[str, Any]:
        check_research_queries(request, set())
        with session_scope() as (session, owner_id):
            return research_response(job=research_jobs.get_job(session, owner_id, job_id))

    @router.post("/jobs/{job_id}/cancel")
    def job_cancel(job_id: str, request: Request) -> dict[str, Any]:
        check_research_queries(request, set())
        with session_scope() as (session, owner_id):
            result = research_jobs.cancel_job(session, owner_id, job_id)
            session.commit()
            return research_response(job=result)

    @router.post("/jobs/{job_id}/retry")
    async def job_retry(job_id: str, request: Request) -> dict[str, Any]:
        check_research_queries(request, set())
        payload = await read_research_body(request, RetryIn, limit=1024)
        result: dict[str, Any] = await run_in_threadpool(
            write,
            "job",
            lambda session, owner_id: research_jobs.retry_job(
                session, owner_id, job_id, request_id=payload.request_id
            ),
        )
        return result

    _register_model_routes(router, session_scope, write)
    app.include_router(router)

    register_research_paper(
        app,
        session_factory=session_factory,
        require_admin=require_admin,
        secrets_provider=secrets_provider,
    )
