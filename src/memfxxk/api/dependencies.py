from typing import Annotated, cast

from fastapi import Depends, Header, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import ValidationError

from memfxxk.auth import AuthenticationError, authenticate
from memfxxk.domain import Actor, Principal, RequestContext
from memfxxk.service import BoundService, MemoryService

_bearer = HTTPBearer(auto_error=False)


def get_service(request: Request) -> MemoryService:
    return cast(MemoryService, request.app.state.memory_service)


def get_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    service: Annotated[MemoryService, Depends(get_service)],
) -> Principal:
    try:
        return authenticate(
            credentials.credentials.strip() if credentials else "", engine=service.engine
        )
    except AuthenticationError as exc:
        raise HTTPException(401, str(exc), headers={"WWW-Authenticate": "Bearer"}) from exc


def get_context(
    principal: Annotated[Principal, Depends(get_principal)],
    user: Annotated[str | None, Header(alias="X-Memfxxk-User")] = None,
    agent: Annotated[str | None, Header(alias="X-Memfxxk-Agent")] = None,
    project: Annotated[str | None, Header(alias="X-Memfxxk-Project")] = None,
    actor: Annotated[str | None, Header(alias="X-Memfxxk-Actor")] = None,
) -> RequestContext:
    try:
        parsed_actor = (
            Actor(type="agent", id=agent) if actor is None else Actor.model_validate_json(actor)
        )
    except ValidationError as exc:
        raise HTTPException(422, "X-Memfxxk-Actor must be a valid JSON actor object") from exc
    return RequestContext(
        org_id=principal.org_id,
        user_id=user,
        agent_id=agent,
        project_id=project,
        actor=parsed_actor,
    )


def get_bound_service(
    service: Annotated[MemoryService, Depends(get_service)],
    principal: Annotated[Principal, Depends(get_principal)],
    ctx: Annotated[RequestContext, Depends(get_context)],
) -> BoundService:
    return service.for_request(principal, ctx)


RequestService = Annotated[BoundService, Depends(get_bound_service)]
