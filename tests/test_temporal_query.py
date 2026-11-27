from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from memfxxk.retrieval.temporal import parse_time


@pytest.mark.parametrize(
    ("query", "start", "end"),
    [
        ("上个月订了什么", "2025-03-01", "2025-04-01"),
        ("三月初见过谁", "2025-03-01", "2025-03-11"),
        ("What happened last Tuesday?", "2025-04-08", "2025-04-09"),
        ("上周二下了什么订单", "2025-04-08", "2025-04-09"),
        ("What happened on 2024-02-29?", "2024-02-29", "2024-03-01"),
        ("And on March 5, 2024?", "2024-03-05", "2024-03-06"),
    ],
)
def test_time_expressions_keep_the_callers_zone(query, start, end):
    zone = ZoneInfo("Asia/Shanghai")
    result = parse_time(query, datetime(2025, 4, 10, 14, tzinfo=zone))
    assert result.start == datetime.fromisoformat(start).replace(tzinfo=zone)
    assert result.end == datetime.fromisoformat(end).replace(tzinfo=zone)


def test_invalid_dates_and_verb_may_do_not_activate_temporal_search():
    anchor = datetime.fromisoformat("2025-04-10T12:00:00+00:00")
    assert parse_time("We may visit in a few days.", anchor) is None
    assert parse_time("meeting 2025-02-29", anchor) is None
