import json
from math import ceil
from statistics import median
from time import perf_counter

import pytest

from memfxxk.domain import Principal
from memfxxk.domain.context import BuildContextRequest, PrepareCompaction
from memfxxk.domain.events import IngestEvent
from memfxxk.domain.manage import LinkRequest, PatchRequest, RememberRequest
from memfxxk.domain.retrieval import SearchRequest
from memfxxk.jobs.worker import drain

pytestmark = pytest.mark.integration

_CASES = [
    ("beryl", "direct"),
    ("coral", "direct"),
    ("jade", "direct"),
    ("opal", "direct"),
    ("maple", "one-hop"),
    ("spruce", "one-hop"),
    ("oak", "one-hop"),
    ("elm", "one-hop"),
    ("mercury", "two-hop"),
    ("venus", "two-hop"),
    ("neptune", "two-hop"),
    ("uranus", "two-hop"),
]


def test_graph_paired_smoke(service, make_ctx):
    cases = []
    for name, kind in _CASES:
        ctx = make_ctx(user="benchmark", project=name)
        bound = service.for_request(Principal(org_id=ctx.org_id, role="host"), ctx)

        def remember(content):
            return bound.remember(RememberRequest(content=content)).memory_id

        support = remember(
            f"Access code 735 connects to the {name} procedure."
            if kind == "direct"
            else "Access code 735 is written on a blue note."
        )
        for index in range(12):
            remember(f"Archived {name} procedure audit, entry {index}. No door access here.")
        if kind != "direct":
            anchor = remember(f"{name} procedure {name} procedure {name} procedure")
            route = [anchor, support]
            if kind == "two-hop":
                route.insert(1, remember("Follow the laminated instructions on the bridge."))
            for start, stop in zip(route, route[1:]):
                bound.link(LinkRequest(from_memory_id=start, to_memory_id=stop, purpose="source"))
        cases.append((bound, name, kind, support))
    drain(service, kinds={"embed_record"})

    results = {hops: {"direct": 0, "one-hop": 0, "two-hop": 0} for hops in (0, 1, 2)}
    latencies = {hops: [] for hops in results}
    for bound, name, kind, support in cases:

        def query(hops):
            return bound.search(
                SearchRequest(
                    query=f"{name} procedure",
                    k=4,
                    graph_hops=hops,
                    expand_relations=False,
                )
            )

        for hops in results:
            response = query(hops)
            results[hops][kind] += int(any(hit.memory_id == support for hit in response.hits))
        for repeat in range(3):
            for hops in sorted(results, reverse=bool(repeat % 2)):
                start = perf_counter()
                query(hops)
                latencies[hops].append((perf_counter() - start) * 1000)
    print(
        "graph smoke",
        json.dumps(
            {
                "recall_at_4": results,
                "median_ms": {hops: round(median(values), 2) for hops, values in latencies.items()},
                "p95_ms": {
                    hops: round(sorted(values)[ceil(0.95 * len(values)) - 1], 2)
                    for hops, values in latencies.items()
                },
                "queries_per_arm": len(cases),
                "timed_queries_per_arm": len(cases) * 3,
            }
        ),
    )
    assert results[1]["one-hop"] > results[0]["one-hop"]
    assert results[2]["two-hop"] > results[1]["two-hop"]


def test_rolling_summary_against_equal_budget_raw_tail(service, make_ctx):
    ctx = make_ctx(user="benchmark", project="pressure")
    bound = service.for_request(Principal(org_id=ctx.org_id, role="host"), ctx)
    session_id = "pressure"
    evidence = "The emergency meeting point is East Gate."
    first_id = None
    for number in range(35):
        message = evidence if number == 0 else f"Progress update {number}. " + "routine work " * 25
        batch = bound.ingest(
            [
                IngestEvent(
                    kind="message",
                    role="user",
                    content=message,
                    session_id=session_id,
                    turn_id=str(number),
                ),
                IngestEvent(
                    kind="message",
                    role="assistant",
                    content="Logged.",
                    session_id=session_id,
                    turn_id=str(number),
                ),
            ]
        )
        if number == 0:
            first_id = batch[0].event_id
            bound.patch_memory(batch[0].memory_ids_touched[0], PatchRequest(priority=5))
    assert first_id is not None
    request = BuildContextRequest(
        session_id=session_id,
        budget={"working": 5_000},
        sections=["working"],
        auto_recall=False,
    )
    raw = bound.build_context(request)
    tiny = request.model_copy(update={"max_tokens": 180})
    tail = bound.build_context(tiny)
    state = bound.get_session(session_id)
    draft = bound.prepare_compaction(
        PrepareCompaction(
            session_id=session_id,
            revision=state.revision,
            through_seq=64,
            target_tokens=75,
        )
    )
    receipt = bound.commit_compaction(draft.draft_id)
    summary = bound.build_context(tiny)
    assert first_id in bound.get_memory(receipt.memory_id).source_event_ids
    values = {
        mode: {"evidence_retained": evidence in result.rendered, "tokens": result.total_tokens}
        for mode, result in {"raw": raw, "tail": tail, "summary": summary}.items()
    }
    print("compaction smoke", json.dumps(values))
    assert values["raw"]["evidence_retained"]
    assert not values["tail"]["evidence_retained"]
    assert values["summary"]["evidence_retained"]
    assert summary.total_tokens <= 180
