"""Shared bounded, strict JSON and response contract for owner research routes."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, TypeVar

from fastapi import HTTPException, Request
from pydantic import BaseModel, ValidationError

from lib_data.research_dataset import MAX_IMPORT_BYTES

# A one-byte CSV control character can require six JSON bytes (\u0000).
MAX_REQUEST_BYTES = 6 * MAX_IMPORT_BYTES + 16 * 1024
ResearchBody = TypeVar("ResearchBody", bound=BaseModel)


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            msg = "Duplicate JSON fields are forbidden"
            raise ValueError(msg)
        result[name] = value
    return result


def _bad_constant(_value: str) -> None:
    msg = "Nonfinite JSON numbers are forbidden"
    raise ValueError(msg)


async def read_research_body(
    request: Request, schema: type[ResearchBody], *, limit: int | None = None
) -> ResearchBody:
    """Bound input while streaming, before JSON and Pydantic allocate objects."""
    maximum = MAX_REQUEST_BYTES if limit is None else min(limit, MAX_REQUEST_BYTES)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > maximum:
            raise HTTPException(status_code=413, detail="Research request byte limit exceeded")
        body.extend(chunk)
    try:
        decoded = json.loads(
            body.decode("utf-8"), object_pairs_hook=_object, parse_constant=_bad_constant
        )
        return schema.model_validate(decoded)
    except (ValueError, UnicodeDecodeError, ValidationError, RecursionError) as exc:
        raise HTTPException(
            status_code=422, detail="Invalid research request fields or JSON"
        ) from exc


def check_research_queries(request: Request, permitted: set[str]) -> None:
    """Reject unknown fields, including identity selectors, and duplicate keys."""
    keys = list(request.query_params.keys())
    if set(keys) - permitted or any(len(request.query_params.getlist(key)) != 1 for key in keys):
        raise HTTPException(status_code=422, detail="Unknown or duplicate research query fields")


def research_response(**values: Any) -> dict[str, Any]:
    return {"as_of": datetime.now(UTC).isoformat().replace("+00:00", "Z"), **values}


__all__ = ["MAX_REQUEST_BYTES", "check_research_queries", "read_research_body", "research_response"]
