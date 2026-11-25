import json
from collections.abc import Iterable
from copy import copy
from datetime import datetime
from functools import cache
from typing import Self, TypedDict, Unpack
from urllib.parse import quote
from uuid import UUID

import httpx
from pydantic import TypeAdapter

from memfxxk.domain import Actor
from memfxxk.domain.context import BuildContextRequest
from memfxxk.domain.documents import document_path
from memfxxk.domain.events import IngestEvent
from memfxxk.ids import derived_id, new_id


class ClientError(Exception):
    def __init__(self, status_code: int, code: str, message: str, details: object = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.details = details


class NotFound(ClientError):
    pass


class Conflict(ClientError):
    @property
    def current_version(self) -> int | None:
        if isinstance(self.details, dict):
            for name in ("current_version", "current_revision", "head_revision", "revision"):
                version = self.details.get(name)
                if isinstance(version, int):
                    return version
        return None


class PermissionDenied(ClientError):
    pass


class ValidationError(ClientError):
    pass


class ServerError(ClientError):
    pass


def base_address(base_url: str) -> str:
    address = base_url.rstrip("/")
    return address + "/" if address.endswith("/v1") else address + "/v1/"


class ContextOverrides(TypedDict, total=False):
    user_id: str | None
    agent_id: str | None
    project_id: str | None
    actor: Actor | dict[str, object] | None


class ClientContext:
    def __init__(
        self,
        api_key: str,
        user_id: str | None,
        agent_id: str | None,
        project_id: str | None,
        actor: Actor | dict[str, object] | None,
    ) -> None:
        self._api_key = api_key
        self.user_id = user_id
        self.agent_id = agent_id
        self.project_id = project_id
        self.actor = Actor.model_validate(actor) if actor is not None else None
        self._owns_connection = True

    def with_ctx(self, **overrides: Unpack[ContextOverrides]) -> Self:
        """Share the client's connection pool with independently overridden request headers."""
        child = copy(self)
        child._owns_connection = False
        for name, value in overrides.items():
            if name not in ContextOverrides.__annotations__:
                raise TypeError(f"Unknown context field: {name}")
            if name == "actor" and value is not None:
                value = Actor.model_validate(value)
            setattr(child, name, value)
        return child

    def _headers(self) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        for name, value in (
            ("User", self.user_id),
            ("Agent", self.agent_id),
            ("Project", self.project_id),
        ):
            if value is not None:
                headers[f"X-Memfxxk-{name}"] = value
        if self.actor is not None:
            headers["X-Memfxxk-Actor"] = json.dumps(self.actor.model_dump(mode="json"))
        return headers


def segment(value: UUID | str) -> str:
    return quote(str(value), safe="")


def document_endpoint(path: str, revision: int | None = None) -> str:
    try:
        canonical = document_path(path)
    except ValueError as exc:
        raise ValidationError(422, "validation_error", str(exc)) from exc
    route = f"documents/{quote(canonical, safe='/')}"
    return route if revision is None else f"{route}/revisions/{revision}"


def context_request(
    session_id: str | BuildContextRequest | None,
    query: str | None,
    budget: dict[str, int],
) -> BuildContextRequest:
    if isinstance(session_id, BuildContextRequest):
        if query is not None or budget:
            raise TypeError("A BuildContextRequest already specifies the query and budget")
        return session_id
    return BuildContextRequest.model_validate(
        {"session_id": session_id, "query": query, "budget": budget or None}
    )


def turn_events(
    session_id: str,
    user: str,
    assistant: str,
    tool_events: Iterable[IngestEvent],
    turn_id: str | None,
    event_time: datetime | None,
) -> list[IngestEvent]:
    turn = turn_id if turn_id is not None else str(new_id())
    common: dict[str, object] = {"session_id": session_id, "turn_id": turn}
    if event_time is not None:
        common["event_time"] = event_time
    candidates = [
        IngestEvent(kind="message", role="user", content=user),
        *tool_events,
        IngestEvent(kind="message", role="assistant", content=assistant),
    ]
    return [
        IngestEvent.model_validate(
            {
                **event.model_dump(exclude_unset=True),
                **common,
                "idempotency_key": str(derived_id("sdk_turn", session_id, turn, str(index))),
            }
        )
        for index, event in enumerate(candidates)
    ]


def _http_error(response: httpx.Response) -> ClientError:
    status = response.status_code
    error_type = {
        400: ValidationError,
        401: PermissionDenied,
        403: PermissionDenied,
        404: NotFound,
        409: Conflict,
        422: ValidationError,
    }.get(status, ServerError)
    try:
        body = response.json()
        envelope = body["error"]
        return error_type(
            status, str(envelope["code"]), str(envelope["message"]), envelope.get("details")
        )
    except (ValueError, KeyError, TypeError, AttributeError):
        return error_type(status, f"http_{status}", f"HTTP {status}")


@cache
def _adapter[T](model: type[T]) -> TypeAdapter[T]:
    return TypeAdapter(model)


def decode[T](response: httpx.Response, model: type[T]) -> T:
    if not response.is_success:
        raise _http_error(response)
    if response.status_code == 204:
        return _adapter(model).validate_python(None)
    return _adapter(model).validate_json(response.content)
