from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http import HTTPStatus
from threading import Event as StopEvent
from threading import Thread
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException

from memfxxk.api.dependencies import get_bound_service
from memfxxk.config import Settings
from memfxxk.db import create_engine
from memfxxk.domain.events import Event, IngestEvent, IngestResult
from memfxxk.ingest import NotFound, TurnScopeConflict
from memfxxk.jobs import Worker
from memfxxk.scope import ScopeError
from memfxxk.service import BoundService, MemoryService

RequestService = Annotated[BoundService, Depends(get_bound_service)]


class IngestBatch(BaseModel):
    events: list[IngestEvent]


def _error_response(
    status: int,
    code: str,
    message: str,
    *,
    details: object = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message, "details": details}},
        status_code=status,
        headers=headers,
    )


def create_app(settings: Settings | None = None, *, engine: Engine | None = None) -> FastAPI:
    config = settings if settings is not None else Settings()
    db_engine = engine if engine is not None else create_engine(str(config.database_url))
    service = MemoryService(db_engine, config)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        stop = StopEvent()
        thread = None
        if config.worker.embedded:
            worker = Worker(service)
            thread = Thread(
                target=worker.run_forever, args=(stop,), name="memfxxk-worker", daemon=True
            )
            thread.start()
        try:
            yield
        finally:
            stop.set()
            if thread is not None:
                thread.join(timeout=2)
            if engine is None:
                db_engine.dispose()

    app = FastAPI(title="memfxxk", lifespan=lifespan)
    app.state.memory_service = service

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException) -> JSONResponse:
        status = HTTPStatus(exc.status_code)
        message = status.phrase if status == HTTPStatus.NOT_FOUND else str(exc.detail)
        return _error_response(status, status.name.lower(), message, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"location": item["loc"], "message": item["msg"], "type": item["type"]}
            for item in exc.errors()
        ]
        return _error_response(422, "validation_error", "Invalid request", details=errors)

    @app.exception_handler(ScopeError)
    async def scope_error(_request: Request, exc: ScopeError) -> JSONResponse:
        return _error_response(403, "forbidden", str(exc))

    @app.exception_handler(NotFound)
    async def not_found(_request: Request, _exc: NotFound) -> JSONResponse:
        return _error_response(404, "not_found", HTTPStatus.NOT_FOUND.phrase)

    @app.exception_handler(TurnScopeConflict)
    async def turn_scope_conflict(_request: Request, exc: TurnScopeConflict) -> JSONResponse:
        return _error_response(409, "conflict", str(exc))

    @app.exception_handler(SQLAlchemyError)
    async def database_error(_request: Request, _exc: SQLAlchemyError) -> JSONResponse:
        return _error_response(503, "database_unavailable", "Database unavailable")

    @app.get("/v1/healthz")
    def healthz(service: RequestService) -> dict[str, str]:
        service.check_database()
        return {"status": "ok"}

    @app.post("/v1/events")
    def post_events(body: IngestBatch, service: RequestService) -> list[IngestResult]:
        return service.ingest(body.events)

    @app.get("/v1/events/{event_id}")
    def read_event(event_id: UUID, service: RequestService) -> Event:
        return service.get_event(event_id)

    @app.get("/v1/sessions/{session_id}/events")
    def read_session_events(
        session_id: str,
        service: RequestService,
        from_seq: Annotated[int | None, Query(ge=1)] = None,
        to_seq: Annotated[int | None, Query(ge=1)] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    ) -> list[Event]:
        return service.list_session_events(session_id, from_seq, to_seq, limit)

    return app
