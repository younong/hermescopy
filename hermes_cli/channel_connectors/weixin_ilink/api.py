"""Minimal public HTTP API for short-lived iLink enrollment attempts."""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse

_ATTEMPT_RE = re.compile(r"^enr_[0-9a-f]{32}$")

router = APIRouter()


class EnrollmentCreate(BaseModel):
    scene: str = Field(default="join", max_length=32)
    device_id: str = Field(min_length=1, max_length=128)


def _no_store(payload: dict, *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        payload,
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


@router.post("/api/public/ilink/enrollments")
async def create_enrollment(body: EnrollmentCreate, request: Request):
    service = getattr(request.app.state, "weixin_ilink_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Enrollment is unavailable")
    source = request.client.host if request.client else ""
    try:
        view = await service.enrollments.create(
            source=source,
            device_id=body.device_id,
            scene=body.scene,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        if "rate limit" in str(exc):
            raise HTTPException(status_code=429, detail="Too many enrollment attempts") from exc
        raise HTTPException(status_code=503, detail="Enrollment is unavailable") from exc
    return _no_store(
        {
            "attempt_id": view.attempt_id,
            "qr_content": view.qr_content,
            "status": view.status,
            "expires_at": view.expires_at,
        },
        status_code=201,
    )


@router.get("/api/public/ilink/enrollments/{attempt_id}")
async def get_enrollment(attempt_id: str, request: Request):
    if not _ATTEMPT_RE.fullmatch(attempt_id):
        raise HTTPException(status_code=404, detail="Enrollment not found")
    service = getattr(request.app.state, "weixin_ilink_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Enrollment is unavailable")
    view = service.enrollments.get(attempt_id)
    if view is None:
        raise HTTPException(status_code=404, detail="Enrollment not found")
    return _no_store(
        {
            "status": view.status,
            "expires_at": view.expires_at,
            "next_action": view.next_action,
        }
    )
