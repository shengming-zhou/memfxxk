import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import ColumnElement, Connection, and_, case, func, select

from memfxxk.db import records
from memfxxk.retrieval.channels import Candidate

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
MONTHS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
CHINESE_NUMBERS = "一二三四五六七八九"
MONTH_NUMBER = r"(?:1[0-2]|[1-9]|十[一二]?|[一二三四五六七八九])"


@dataclass(frozen=True, slots=True)
class TimeRange:
    start: datetime
    end: datetime


def _date(anchor: datetime, year: int, month: int, day: int) -> datetime:
    return anchor.replace(
        year=year, month=month, day=day, hour=0, minute=0, second=0, microsecond=0
    )


def _day(day: datetime) -> TimeRange:
    return TimeRange(day, day + timedelta(days=1))


def _number(text: str) -> int:
    if text.isdecimal():
        return int(text)
    if text.startswith("十"):
        return 10 + (CHINESE_NUMBERS.index(text[-1]) + 1 if len(text) > 1 else 0)
    return CHINESE_NUMBERS.index(text) + 1


def _month(anchor: datetime, year: int, month: int, phase: str = "") -> TimeRange:
    start = _date(anchor, year, month, 1)
    end = start + timedelta(days=monthrange(year, month)[1])
    if phase in ("初", "上旬", "early"):
        return TimeRange(start, start + timedelta(days=10))
    if phase in ("中旬", "mid"):
        return TimeRange(start + timedelta(days=10), start + timedelta(days=20))
    if phase in ("末", "下旬", "late"):
        return TimeRange(start + timedelta(days=20), end)
    return TimeRange(start, end)


def parse_time(query: str, anchor: datetime) -> TimeRange | None:
    """Interpret a temporal expression relative to a supplied zoned reference time."""
    value = query.casefold()
    today = _date(anchor, anchor.year, anchor.month, anchor.day)
    explicit = re.search(r"(?<!\d)(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})(?:日|号)?(?!\d)", value)
    if explicit:
        try:
            return _day(_date(anchor, *map(int, explicit.groups())))
        except ValueError:
            return None
    if re.search(r"上个?月|\blast month\b", value):
        prior = today.replace(day=1) - timedelta(days=1)
        return _month(today, prior.year, prior.month)
    if re.search(r"这个?月|本月|\bthis month\b", value):
        return _month(today, today.year, today.month)
    weekday = re.search(r"\blast (" + "|".join(WEEKDAYS) + r")\b", value)
    chinese_day = re.search(r"上(?:周|星期)([一二三四五六日天])", value)
    if weekday or chinese_day:
        requested = (
            WEEKDAYS.index(weekday[1])
            if weekday
            else min(6, "一二三四五六日天".index(chinese_day[1]))
        )
        return _day(today - timedelta(days=((today.weekday() - requested - 1) % 7) + 1))
    if re.search(r"上(?:周|星期)|\blast week\b", value):
        monday = today - timedelta(days=today.weekday() + 7)
        return TimeRange(monday, monday + timedelta(days=7))
    relative = ((r"前天", 2), (r"昨天|昨日|\byesterday\b", 1), (r"今天|今日|\btoday\b", 0))
    for expression, offset in relative:
        if re.search(expression, value):
            return _day(today - timedelta(days=offset))
    pattern = rf"(?:(\d{{4}})年)?(?<!\d)({MONTH_NUMBER})月(初|上旬|中旬|末|下旬)?"
    chinese = re.search(pattern, value)
    english = re.search(
        r"\b(?:(early|mid|late)[ -]+)?("
        + "|".join(MONTHS)
        + r")(?: +(\d{1,2})(?:st|nd|rd|th)?\b)?(?:[ ,]+(\d{4}))?\b",
        value,
    )
    day = None
    if chinese:
        year, month, phase = chinese[1], _number(chinese[2]), chinese[3] or ""
    elif english:
        year, month, phase = english[4], MONTHS.index(english[2]) + 1, english[1] or ""
        day = int(english[3]) if english[3] else None
        if english[2] == "may" and not (year or day or phase or re.search(r"\bin may\b", value)):
            return None
    else:
        return None
    resolved_year = int(year) if year else today.year - int(month > today.month)
    try:
        return (
            _day(_date(today, resolved_year, month, day))
            if day
            else _month(today, resolved_year, month, phase)
        )
    except ValueError:
        return None


def search_time(
    conn: Connection,
    query: str,
    anchor: datetime,
    filters: list[ColumnElement[bool]],
    limit: int,
) -> list[Candidate]:
    period = parse_time(query, anchor)
    if period is None:
        return []
    time = func.coalesce(records.c.valid_from, records.c.event_time)
    offset = case(
        (time < period.start, func.extract("epoch", period.start - time)),
        (time >= period.end, func.extract("epoch", time - period.end)),
        else_=0,
    )
    outside = case((and_(time >= period.start, time < period.end), 0), else_=1)
    rows = conn.execute(
        select(records.c.id, records.c.generation, offset.label("distance"))
        .where(*filters, offset <= 14 * 86400, time <= anchor)
        .order_by(outside, offset, time.desc(), records.c.id)
        .limit(limit)
    )
    return [
        Candidate(row.id, row.generation, 1.0 / (1.0 + float(row.distance) / 86400.0))
        for row in rows
    ]
