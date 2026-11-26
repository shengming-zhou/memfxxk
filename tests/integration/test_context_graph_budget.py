from collections.abc import Callable

import pytest

from memfxxk.domain import Principal, RequestContext
from memfxxk.domain.context import BuildContextRequest, SearchFilters
from memfxxk.domain.documents import PutDocumentRequest
from memfxxk.domain.manage import LinkRequest, PatchRequest, RememberRequest
from memfxxk.service import MemoryService
from memfxxk.text import SimpleCounter

pytestmark = pytest.mark.integration


def test_graph_evidence_stays_within_evidence_and_bundle_caps(
    service: MemoryService, make_ctx: Callable[..., RequestContext]
) -> None:
    ctx = make_ctx(user="keeper", project="atlas")
    bound = service.for_request(Principal(org_id=ctx.org_id, role="host"), ctx)
    anchor = bound.remember(RememberRequest(content="Calibration check " + "detail " * 200))
    support = bound.remember(RememberRequest(content="The approved value is seven."))
    bound.link(
        LinkRequest(
            from_memory_id=anchor.memory_id,
            to_memory_id=support.memory_id,
            purpose="verification",
        )
    )
    bound.put_document("project.md", PutDocumentRequest(content="Follow the project conventions."))
    request = BuildContextRequest(
        query="calibration",
        sections=["documents", "evidence"],
        budget={"evidence": 111},
        max_tokens=105,
        search_filters=SearchFilters(channels=["lexical"], graph_hops=1),
    )

    baseline = bound.build_context(request)
    evidence = baseline.sections[-1]
    assert [item.ref_id for item in evidence.items] == [str(anchor.memory_id)]
    assert str(support.memory_id) in [item.ref_id for item in evidence.dropped]
    assert "The approved value is seven." not in evidence.rendered

    bundle = bound.build_context(request.model_copy(update={"graph_evidence_tokens": 42}))
    evidence = bundle.sections[-1]
    assert [item.ref_id for item in evidence.items] == [
        str(anchor.memory_id),
        str(support.memory_id),
    ]
    assert evidence.items[-1].reason.startswith("graph(path=")
    assert not evidence.items[-1].truncated
    assert "The approved value is seven." in evidence.rendered
    graph_text = evidence.rendered[evidence.rendered.index("## Graph-related memory") :]
    assert SimpleCounter().count(graph_text) <= 42
    assert evidence.used_tokens <= evidence.budget
    assert bundle.total_tokens <= 105
    assert bound.get_bundle(bundle.bundle_id).sections[-1].rendered == ""

    bound.patch_memory(
        support.memory_id,
        PatchRequest(
            base_version=support.version,
            content="The approved value is seven. " + "detail " * 200,
        ),
    )
    clipped = bound.build_context(request.model_copy(update={"graph_evidence_tokens": 42}))
    evidence = clipped.sections[-1]
    assert evidence.items[-1].ref_id == str(support.memory_id)
    assert evidence.items[-1].truncated
    assert "The approved value is seven." in evidence.rendered
    assert clipped.total_tokens <= 105


def test_graph_budget_releases_unused_capacity_and_respects_scope(
    service: MemoryService, make_ctx: Callable[..., RequestContext]
) -> None:
    ctx = make_ctx(user="keeper", project="atlas")
    principal = Principal(org_id=ctx.org_id, role="host")
    owner = service.for_request(principal, ctx)
    anchor = owner.remember(
        RememberRequest(
            content="Calibration check " + "detail " * 120,
            scope_drop={"project_id", "user_id"},
        )
    )
    request = BuildContextRequest(
        query="calibration",
        sections=["evidence"],
        budget={"evidence": 75},
        max_tokens=75,
        search_filters=SearchFilters(channels=["lexical"], graph_hops=1),
    )
    regular = owner.build_context(request).sections[0]
    reserved = request.model_copy(update={"graph_evidence_tokens": 60})
    assert owner.build_context(reserved).sections[0] == regular

    private = owner.remember(RememberRequest(content="Private approval is seven."))
    owner.link(
        LinkRequest(
            from_memory_id=anchor.memory_id,
            to_memory_id=private.memory_id,
            purpose="verification",
        )
    )
    small = owner.build_context(request.model_copy(update={"graph_evidence_tokens": 1})).sections[0]
    ordinary = owner.build_context(request).sections[0]
    assert small.rendered == ordinary.rendered
    assert small.items == ordinary.items
    assert [item.ref_id for item in small.dropped] == [str(private.memory_id)]
    for other in (
        make_ctx(user="another", project="atlas"),
        make_ctx(user="keeper", project="other"),
    ):
        outsider = service.for_request(principal, other)
        evidence = outsider.build_context(reserved).sections[0]
        assert [item.ref_id for item in evidence.items] == [str(anchor.memory_id)]
        assert evidence.dropped == []
        assert "Private approval" not in evidence.rendered
