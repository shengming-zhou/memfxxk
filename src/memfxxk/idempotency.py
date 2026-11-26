import json
from hashlib import sha256

from memfxxk.domain import ProjectGrant

LEGACY_NAMESPACE = ""


def idempotency_namespace(grant: ProjectGrant | None) -> str:
    if grant is None:
        return LEGACY_NAMESPACE
    # Reissued credentials for the same identity can still retry earlier writes.
    identity = json.dumps([grant.project_id, grant.user_id, grant.agent_id])
    return sha256(identity.encode()).hexdigest()
