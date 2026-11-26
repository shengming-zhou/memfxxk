from hashlib import sha256

from pydantic import ValidationError
from sqlalchemy import Engine, select

from memfxxk.db import api_keys, project_key_grants
from memfxxk.domain import Principal


class AuthenticationError(ValueError):
    """The supplied API key could not be authenticated."""


def hash_key(raw_key: str) -> str:
    return sha256(raw_key.encode("utf-8")).hexdigest()


def authenticate(raw_key: str, *, engine: Engine) -> Principal:
    if not raw_key:
        raise AuthenticationError("A valid API key is required")

    query = (
        select(
            api_keys.c.org_id,
            api_keys.c.role,
            project_key_grants.c.project_id,
            project_key_grants.c.user_id,
            project_key_grants.c.agent_id,
            project_key_grants.c.access,
        )
        .select_from(
            api_keys.outerjoin(project_key_grants, project_key_grants.c.key_id == api_keys.c.id)
        )
        .where(
            api_keys.c.key_hash == hash_key(raw_key),
            api_keys.c.revoked_at.is_(None),
        )
    )
    with engine.connect() as connection:
        key = connection.execute(query).mappings().one_or_none()
    if key is None:
        raise AuthenticationError("A valid API key is required")
    try:
        return Principal.model_validate(
            {
                "org_id": key.org_id,
                "role": key.role,
                "project_grant": dict(key) if key.project_id is not None else None,
            }
        )
    except ValidationError as exc:
        raise AuthenticationError("A valid API key is required") from exc
