import json
from uuid import NAMESPACE_URL, UUID, uuid5

from uuid_utils.compat import uuid7


def new_id() -> UUID:
    return uuid7()


def derived_id(namespace: UUID | str, *parts: str) -> UUID:
    if isinstance(namespace, str):
        namespace = uuid5(NAMESPACE_URL, f"memfxxk:{namespace}")
    return uuid5(namespace, json.dumps(parts, ensure_ascii=False, separators=(",", ":")))
