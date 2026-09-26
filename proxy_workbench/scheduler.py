"""F15: schedules, quiet hours, budgets and state-change notifications.

The module is a leaf. It answers two questions — *when* a piece of work may
start and *how much* of it is allowed — and publishes the answer. It never opens
a socket, never writes DDL and never delivers anything outside the process:
external email and webhooks need a channel implementation plus a target the
user selected explicitly, because F15 forbids sending to anybody who did not
ask for it.

Time handling. The public API speaks unix seconds. Windows are local
wall-clock ranges in a named timezone, and every day of a window is resolved
once: an ambiguous wall clock (a DST fold) yields one interval that covers both
passes, and a nonexistent wall clock (a DST gap) yields the interval that ends
where the clock jumps away. A daily slot fires once per local date — the fold
does not double it and the gap does not invent a run at a time that never
happened. `dst_policy` decides whether such a slot is shifted to the transition
or skipped with a reason.

Traffic accounting. Workbench traffic (`probe`, `source`, `retry`, `judge`,
`speedtest`) and relay traffic are counted in separate counters: relay bytes
never consume a workbench budget, and workbench bytes never appear as billable
traffic. Once a limit is reached the work that already started may still land,
but only within a bounded in-flight reserve, and the snapshot says how much
that reserve is and how much of it is outstanding.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field, fields, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional, Protocol, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import i18n

UTC = timezone.utc

WEEKDAY_NAMES = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')
ALL_WEEKDAYS = WEEKDAY_NAMES

#: Notifications never scan further back than this after a very long pause; the plan is marked truncated.
MAX_LOOKBACK_DAYS = 400
#: How many sent notifications and dispatch reports a notifier keeps in memory.
NOTIFIER_HISTORY = 200
#: How many runs the in-memory store keeps per schedule; SQLite keeps the real history.
IN_MEMORY_RUN_HISTORY = 50
#: Bound on how many times the planner may jump from a closed window to the next one.
MAX_WINDOW_JUMPS = 64

#: Workbench-side payload classes. They share one budget and never mix with relay traffic.
PROBE = 'probe'
SOURCE = 'source'
RETRY = 'retry'
JUDGE = 'judge'
SPEEDTEST = 'speedtest'
#: Client traffic through the gateway. Counted apart, never charged to a workbench budget.
RELAY = 'relay'

WORKBENCH_CLASSES = (PROBE, SOURCE, RETRY, JUDGE, SPEEDTEST)
CHEAP_CLASSES = (PROBE, SOURCE, RETRY)
ALL_CLASSES = WORKBENCH_CLASSES + (RELAY,)

MIB = 1024 * 1024

DST_SKIP = 'skip'
DST_SHIFT = 'shift'
DST_POLICIES = (DST_SKIP, DST_SHIFT)

RESET_NONE = 'none'
RESET_HOURLY = 'hourly'
RESET_DAILY = 'daily'
RESET_MONTHLY = 'monthly'
RESET_RULES = (RESET_NONE, RESET_HOURLY, RESET_DAILY, RESET_MONTHLY)

KIND_INTERVAL = 'interval'
KIND_SLOTS = 'slots'
SCHEDULE_KINDS = (KIND_INTERVAL, KIND_SLOTS)

#: Why a tick did or did not produce a run. Closed set: every reason is a token the UI can translate.
REASON_DUE = 'due'
REASON_MANUAL = 'manual'
REASON_NOT_DUE = 'not_due'
REASON_DISABLED = 'disabled'
REASON_PAUSED_USER = 'paused_user'
REASON_PAUSED_QUIET = 'paused_quiet_hours'
REASON_PAUSED_BUDGET = 'paused_budget'
REASON_PAUSED_METERED = 'paused_metered'
REASON_PAUSED_BATTERY = 'paused_battery'
REASON_WINDOW_CLOSED = 'window_closed'
REASON_NO_SLOT = 'no_slot_in_window'
REASON_DST_SKIPPED = 'dst_slot_skipped'
REASON_COALESCED = 'coalesced_no_catchup'
REASON_COALESCED_SLEEP = 'coalesced_after_sleep'
REASON_BUDGET_DENIED = 'budget_denied'
REASON_STAGE_NOT_ALLOWED = 'stage_not_allowed'
DECISION_REASONS = (
    REASON_DUE, REASON_MANUAL, REASON_NOT_DUE, REASON_DISABLED, REASON_PAUSED_USER,
    REASON_PAUSED_QUIET, REASON_PAUSED_BUDGET, REASON_PAUSED_METERED, REASON_PAUSED_BATTERY,
    REASON_WINDOW_CLOSED, REASON_NO_SLOT, REASON_DST_SKIPPED, REASON_COALESCED,
    REASON_COALESCED_SLEEP, REASON_BUDGET_DENIED, REASON_STAGE_NOT_ALLOWED,
)

POWER_IGNORE = 'ignore'
POWER_PAUSE = 'pause'
POWER_BELOW_PERCENT = 'pause_below_percent'
POWER_CHEAP_ONLY = 'cheap_only'
BATTERY_POLICIES = (POWER_IGNORE, POWER_PAUSE, POWER_BELOW_PERCENT)
METERED_POLICIES = (POWER_IGNORE, POWER_PAUSE, POWER_CHEAP_ONLY)
UNKNOWN_AS_METERED = 'treat_as_metered'
UNKNOWN_AS_UNMETERED = 'treat_as_unmetered'
UNKNOWN_METERED_POLICIES = (UNKNOWN_AS_METERED, UNKNOWN_AS_UNMETERED)

#: Notification codes. `code` is machine-readable, the text comes from MESSAGES through i18n.tr.
NOTIFY_PAUSED = 'SCHEDULE_PAUSED'
NOTIFY_RESUMED = 'SCHEDULE_RESUMED'
NOTIFY_QUIET = 'SCHEDULE_QUIET_HOURS'
NOTIFY_WINDOW = 'SCHEDULE_OUTSIDE_WINDOW'
NOTIFY_BUDGET = 'BUDGET_EXHAUSTED'
NOTIFY_BUDGET_RESET = 'BUDGET_RESET'
NOTIFY_BELOW_MINIMUM = 'BELOW_MINIMUM'
NOTIFY_RECOVERED = 'RECOVERED'
NOTIFY_POWER = 'POWER_RESTRICTED'
NOTIFY_SKIPPED = 'SCHEDULE_SKIPPED'
NOTIFICATION_CODES = (
    NOTIFY_PAUSED, NOTIFY_RESUMED, NOTIFY_QUIET, NOTIFY_WINDOW, NOTIFY_BUDGET,
    NOTIFY_BUDGET_RESET, NOTIFY_BELOW_MINIMUM, NOTIFY_RECOVERED, NOTIFY_POWER, NOTIFY_SKIPPED,
)

SEVERITY_INFO = 'info'
SEVERITY_WARNING = 'warning'
SEVERITY_CRITICAL = 'critical'

CHANNEL_IN_APP = 'in_app'
CHANNEL_OS = 'os'
CHANNEL_EMAIL = 'email'
CHANNEL_WEBHOOK = 'webhook'
#: Channels that leave the machine. They are silent until a target is selected by the user.
EXTERNAL_CHANNELS = (CHANNEL_EMAIL, CHANNEL_WEBHOOK)
SKIP_NO_CHANNEL = 'channel_not_selected'
SKIP_NO_TARGET = 'target_not_configured'
SKIP_TARGET_OFF = 'target_disabled'
SKIP_NOT_CONFIRMED = 'not_user_confirmed'
SKIP_NO_IMPLEMENTATION = 'channel_not_implemented'

#: (ru, en) for every code this module emits. Translated through the existing i18n.tr, never hardcoded.
MESSAGES: Mapping[str, tuple[str, str]] = {
    'E_VALIDATION_FIELD': ('Некорректное поле «{}»: {}', 'Invalid field "{}": {}'),
    'E_VALIDATION_SCHEMA': ('Некорректная структура: {}', 'Invalid structure: {}'),
    'E_VALIDATION_UNKNOWN_FIELD': ('Неизвестные поля: {}', 'Unknown fields: {}'),
    'E_TIME_UNKNOWN': ('Непригодное время: {}', 'Unusable time: {}'),
    'E_LIMIT_BUDGET': ('Бюджет исчерпан ({}): {}', 'Budget exhausted ({}): {}'),
    # Notification codes take (subject, detail); a one-part message ignores the second argument.
    'SCHEDULE_PAUSED': ('Расписание {} остановлено: {}', 'Schedule {} paused: {}'),
    'SCHEDULE_RESUMED': ('Расписание {} снова запущено ({})', 'Schedule {} resumed ({})'),
    'SCHEDULE_QUIET_HOURS': ('Расписание {} в тихие часы до {}', 'Schedule {} inside quiet hours until {}'),
    'SCHEDULE_OUTSIDE_WINDOW': ('Расписание {} вне рабочего окна до {}',
                                'Schedule {} outside its window until {}'),
    'SCHEDULE_SKIPPED': ('Расписание {} пропущено: {}', 'Schedule {} skipped: {}'),
    'BUDGET_EXHAUSTED': ('Бюджет расписания {} исчерпан: {}', 'Budget of schedule {} exhausted: {}'),
    'BUDGET_RESET': ('Бюджет расписания {} обновлён в {}', 'Budget of schedule {} reset at {}'),
    'BELOW_MINIMUM': ('{} ниже минимума ({})', '{} is below the minimum ({})'),
    'RECOVERED': ('{} восстановился ({})', '{} recovered ({})'),
    'POWER_RESTRICTED': ('Расписание {} ограничено питанием: {}', 'Schedule {} restricted by power: {}'),
}


def message(code: str, *args: object) -> str:
    """Localized text for a machine-readable code (CONTRACTS 5.4)."""
    ru, en = MESSAGES.get(code, (code, code))
    try:
        return i18n.tr(ru, en).format(*args)
    except (IndexError, KeyError):
        return i18n.tr(ru, en)


def describe(code: str) -> tuple[str, str]:
    """(ru, en) pair for a code, for callers that keep their own catalogs."""
    return MESSAGES.get(code, (code, code))


class ScheduleError(Exception):
    """Configuration problem. `code` is a CONTRACTS 5.4 code, `field` names the offending key."""

    def __init__(self, code: str, field_name: str, detail_ru: str, detail_en: str):
        self.code = code
        self.field = field_name
        self.detail = (detail_ru, detail_en)
        super().__init__(self.text())

    def text(self) -> str:
        detail = i18n.tr(*self.detail)
        if self.code == 'E_LIMIT_BUDGET':
            return message(self.code, self.field, detail)
        if self.code in ('E_VALIDATION_UNKNOWN_FIELD', 'E_TIME_UNKNOWN', 'E_VALIDATION_SCHEMA'):
            return message(self.code, detail)
        return message(self.code, self.field, detail)


# ---------------------------------------------------------------- time windows


def _tz(name: str) -> ZoneInfo:
    if not isinstance(name, str) or not name.strip():
        raise ScheduleError('E_VALIDATION_FIELD', 'timezone', 'нужен IANA-идентификатор', 'need an IANA name')
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError, OSError) as exc:
        raise ScheduleError('E_VALIDATION_FIELD', 'timezone',
                            f'неизвестная зона {name!r}', f'unknown timezone {name!r}') from exc


def _weekdays(value: object, field_name: str = 'weekdays') -> tuple[str, ...]:
    if value is None:
        return ALL_WEEKDAYS
    if isinstance(value, str):
        value = [value]
    try:
        items = tuple(str(day).strip().lower()[:3] for day in value)
    except TypeError as exc:
        raise ScheduleError('E_VALIDATION_FIELD', field_name,
                            'нужен список дней недели', 'need a list of weekdays') from exc
    unknown = [day for day in items if day not in WEEKDAY_NAMES]
    if unknown:
        raise ScheduleError('E_VALIDATION_FIELD', field_name,
                            f'неизвестные дни недели: {", ".join(unknown)}',
                            f'unknown weekdays: {", ".join(unknown)}')
    if not items:
        raise ScheduleError('E_VALIDATION_FIELD', field_name,
                            'пустой список дней недели', 'empty weekday list')
    return items


def parse_hhmm(value: object, field_name: str = 'time') -> int:
    """'HH:MM' (or a minute count) -> minutes since local midnight. 24:00 is not accepted."""
    if isinstance(value, bool):
        raise ScheduleError('E_VALIDATION_FIELD', field_name, 'ожидалось время ЧЧ:ММ', 'expected HH:MM')
    if isinstance(value, int):
        if 0 <= value < 24 * 60:
            return int(value)
        raise ScheduleError('E_VALIDATION_FIELD', field_name,
                            f'вне диапазона 0..1439: {value}', f'outside 0..1439: {value}')
    if not isinstance(value, str):
        raise ScheduleError('E_VALIDATION_FIELD', field_name,
                            'ожидалось время ЧЧ:ММ', 'expected HH:MM')
    text = value.strip()
    parts = text.split(':')
    if len(parts) != 2 or len(parts[0]) not in (1, 2) or len(parts[1]) != 2 or not all(p.isdigit() for p in parts):
        raise ScheduleError('E_VALIDATION_FIELD', field_name,
                            f'ожидалось время ЧЧ:ММ, получено {value!r}', f'expected HH:MM, got {value!r}')
    hours, minutes = int(parts[0]), int(parts[1])
    if hours > 23 or minutes > 59:
        raise ScheduleError('E_VALIDATION_FIELD', field_name,
                            f'вне диапазона 00:00..23:59: {value!r}', f'outside 00:00..23:59: {value!r}')
    return hours * 60 + minutes


def format_hhmm(minutes: int) -> str:
    return f'{minutes // 60:02d}:{minutes % 60:02d}'


def local_instants(tz: ZoneInfo, naive: datetime) -> list[datetime]:
    """UTC instants whose local wall clock equals `naive`.

    One for an ordinary time, two inside a DST fold, none inside a DST gap.
    """
    found: list[datetime] = []
    for fold in (0, 1):
        moment = naive.replace(tzinfo=tz, fold=fold).astimezone(UTC)
        if moment.astimezone(tz).replace(tzinfo=None) == naive and moment not in found:
            found.append(moment)
    return sorted(found)


def dst_gap_transition(tz: ZoneInfo, naive: datetime) -> datetime:
    """UTC instant at which the wall clock jumps over `naive`.

    Binary search on the UTC offset: between the two fold interpretations of a
    nonexistent wall time the offset changes exactly once.
    """
    low = naive.replace(tzinfo=tz, fold=0).astimezone(UTC)
    high = naive.replace(tzinfo=tz, fold=1).astimezone(UTC)
    if low == high:
        return low
    if low > high:
        low, high = high, low
    step = timedelta(seconds=1)
    while high - low > step:
        middle = low + (high - low) / 2
        if middle.astimezone(tz).utcoffset() == low.astimezone(tz).utcoffset():
            low = middle
        else:
            high = middle
    return high


def _boundary(tz: ZoneInfo, day: date, minute: int, *, which: str) -> Optional[datetime]:
    """UTC instant of a window boundary on a local date.

    Fold: `start` takes the earlier pass so the window opens with the first one,
    `end` takes the later one so the window covers both. Gap: both take the
    transition instant, so the window begins and ends where the clock jumps.
    """
    naive = datetime(day.year, day.month, day.day) + timedelta(minutes=minute)
    found = local_instants(tz, naive)
    if len(found) == 1:
        return found[0]
    if len(found) == 2:
        return found[0] if which == 'start' else found[1]
    return dst_gap_transition(tz, naive)


def slot_instant(tz: ZoneInfo, day: date, minute: int, dst_policy: str = DST_SKIP) -> Optional[datetime]:
    """UTC instant of one daily slot, or None when the wall time does not exist and policy is skip."""
    naive = datetime(day.year, day.month, day.day) + timedelta(minutes=minute)
    found = local_instants(tz, naive)
    if found:
        return found[0]
    if dst_policy == DST_SHIFT:
        return dst_gap_transition(tz, naive)
    return None


@dataclass(frozen=True)
class Window:
    """Local wall-clock range, possibly crossing midnight.

    `start` later than `end` means the window runs over midnight into the next
    day; the weekday list then names the days the window *opens* on.
    """

    start: int
    end: int
    weekdays: tuple[str, ...] = ALL_WEEKDAYS
    label: str = ''

    def __post_init__(self):
        if not 0 <= self.start < 24 * 60 or not 0 <= self.end < 24 * 60:
            raise ScheduleError('E_VALIDATION_FIELD', 'window',
                                f'вне 00:00..23:59: {format_hhmm(self.start)}-{format_hhmm(self.end)}',
                                f'outside 00:00..23:59: {format_hhmm(self.start)}-{format_hhmm(self.end)}')
        if self.start == self.end:
            raise ScheduleError('E_VALIDATION_FIELD', 'window',
                                'пустое окно: начало равно концу', 'empty window: start equals end')

    @classmethod
    def parse(cls, text: object, weekdays: object = None, label: str = '') -> 'Window':
        """'09:00-18:00' -> Window. An open range ('09:00-') means until midnight."""
        if not isinstance(text, str) or '-' not in text:
            raise ScheduleError('E_VALIDATION_FIELD', 'window',
                                f'ожидалось "ЧЧ:ММ-ЧЧ:ММ", получено {text!r}', f'expected "HH:MM-HH:MM", got {text!r}')
        left, _, right = text.partition('-')
        start = parse_hhmm(left.strip(), 'window.start')
        if not right.strip():
            end = 24 * 60 - 1
        else:
            end = parse_hhmm(right.strip(), 'window.end')
        return cls(start=start, end=end, weekdays=_weekdays(weekdays), label=label)

    @property
    def crosses_midnight(self) -> bool:
        return self.start > self.end

    def contains(self, local: datetime) -> bool:
        """Is this *local* moment inside the window? A fold is open on both passes."""
        if local.tzinfo is None:
            raise ScheduleError('E_VALIDATION_FIELD', 'moment',
                                'нужен момент с зоной', 'need an aware moment')
        minutes = local.hour * 60 + local.minute
        today = WEEKDAY_NAMES[local.weekday()]
        yesterday = WEEKDAY_NAMES[(local.weekday() + 6) % 7]
        if not self.crosses_midnight:
            return today in self.weekdays and self.start <= minutes < self.end
        if minutes >= self.start:
            return today in self.weekdays
        return minutes < self.end and yesterday in self.weekdays

    def intervals_on(self, tz: ZoneInfo, day: date) -> list[tuple[datetime, datetime]]:
        """UTC (start, end) pairs this window is open on a local date. Empty means closed all day."""
        name = WEEKDAY_NAMES[day.weekday()]
        if name not in self.weekdays:
            return []
        if self.crosses_midnight:
            start = _boundary(tz, day, self.start, which='start')
            end = _boundary(tz, day + timedelta(days=1), self.end, which='end')
        else:
            start = _boundary(tz, day, self.start, which='start')
            end = _boundary(tz, day, self.end, which='end')
        if start is None or end is None or end <= start:
            return []
        return [(start, end)]

    def _days_around(self, at: datetime, tz: ZoneInfo) -> Iterable[date]:
        local = at.astimezone(tz)
        base = local.date()
        for offset in (-1, 0, 1, 2):
            yield base + timedelta(days=offset)

    def next_open(self, after: datetime, tz: ZoneInfo, horizon_days: int = 10) -> Optional[datetime]:
        """First open instant at or after `after`, or None when the window never opens again."""
        first = after.astimezone(tz).date() - timedelta(days=1)
        last = after.astimezone(tz).date() + timedelta(days=horizon_days)
        day = first
        while day <= last:
            for start, end in self.intervals_on(tz, day):
                if after < start:
                    return start
                if start <= after < end:
                    return after
            day += timedelta(days=1)
        return None

    def current_close(self, at: datetime, tz: ZoneInfo) -> Optional[datetime]:
        """End of the open stretch that contains `at`, or None when closed."""
        for day in self._days_around(at, tz):
            for start, end in self.intervals_on(tz, day):
                if start <= at < end:
                    return end
        return None

    def to_dict(self) -> dict:
        return {'window': f'{format_hhmm(self.start)}-{format_hhmm(self.end)}',
                'weekdays': list(self.weekdays), 'label': self.label}

    @classmethod
    def from_dict(cls, data: object, field_name: str = 'window') -> 'Window':
        if not isinstance(data, Mapping):
            raise ScheduleError('E_VALIDATION_FIELD', field_name,
                                f'ожидался объект окна, получено {type(data).__name__}',
                                f'expected a window object, got {type(data).__name__}')
        text = data.get('window') or data.get('range')
        if text is None and 'start' in data:
            text = f'{format_hhmm(parse_hhmm(data.get("start"), field_name))}-' \
                   f'{format_hhmm(parse_hhmm(data.get("end"), field_name))}'
        return cls.parse(text, data.get('weekdays'), str(data.get('label') or ''))


@dataclass(frozen=True)
class Slot:
    """One daily instant in local wall-clock time. Used by schedules of kind 'slots'."""

    at: int
    weekdays: tuple[str, ...] = ALL_WEEKDAYS
    label: str = ''

    def __post_init__(self):
        if not 0 <= self.at < 24 * 60:
            raise ScheduleError('E_VALIDATION_FIELD', 'slot',
                                f'вне 00:00..23:59: {format_hhmm(self.at)}', f'outside 00:00..23:59: {format_hhmm(self.at)}')

    @classmethod
    def parse(cls, text: object, weekdays: object = None, label: str = '') -> 'Slot':
        return cls(at=parse_hhmm(text, 'slot'), weekdays=_weekdays(weekdays), label=label)

    def instant_on(self, tz: ZoneInfo, day: date, dst_policy: str = DST_SKIP) -> Optional[datetime]:
        if WEEKDAY_NAMES[day.weekday()] not in self.weekdays:
            return None
        return slot_instant(tz, day, self.at, dst_policy)

    def to_dict(self) -> dict:
        return {'at': format_hhmm(self.at), 'weekdays': list(self.weekdays), 'label': self.label}

    @classmethod
    def from_dict(cls, data: object, field_name: str = 'slot') -> 'Slot':
        if not isinstance(data, Mapping):
            raise ScheduleError('E_VALIDATION_FIELD', field_name,
                                f'ожидался объект слота, получено {type(data).__name__}',
                                f'expected a slot object, got {type(data).__name__}')
        return cls.parse(data.get('at'), data.get('weekdays'), str(data.get('label') or ''))


def in_windows(windows: Sequence[Window], at: datetime, tz: ZoneInfo) -> bool:
    """True when no window is configured (always open) or `at` is inside one of them."""
    if not windows:
        return True
    return any(window.contains(at.astimezone(tz)) for window in windows)


def windows_next_open(windows: Sequence[Window], after: datetime, tz: ZoneInfo) -> Optional[datetime]:
    if not windows:
        return after
    candidates = [window.next_open(after, tz) for window in windows]
    return min((item for item in candidates if item is not None), default=None)


def quiet_until(quiet: Sequence[Window], at: datetime, tz: ZoneInfo) -> Optional[datetime]:
    """End of the quiet stretch containing `at`, or None when `at` is not quiet."""
    if not quiet:
        return None
    for window in quiet:
        if window.contains(at.astimezone(tz)):
            return window.current_close(at, tz)
    return None


def next_slots(tz: ZoneInfo, slots: Sequence[Slot], after: datetime, dst_policy: str = DST_SKIP,
               horizon_days: int = 14) -> tuple[Optional[datetime], int]:
    """Next slot instant strictly after `after` and how many slots were skipped as nonexistent.

    One instant per local date: the fold does not double a slot and a gap either
    shifts it to the transition or is reported as skipped.
    """
    first = after.astimezone(tz).date()
    skipped = 0
    for offset in range(horizon_days + 1):
        day = first + timedelta(days=offset)
        moments = sorted(item for item in (slot.instant_on(tz, day, dst_policy) for slot in slots)
                         if item is not None)
        missing = sum(1 for slot in slots
                      if WEEKDAY_NAMES[day.weekday()] in slot.weekdays
                      and slot.instant_on(tz, day, dst_policy) is None)
        skipped += missing
        for moment in moments:
            if moment > after:
                return moment, skipped
    return None, skipped


# ------------------------------------------------------------------- budgets


@dataclass(frozen=True)
class Budgets:
    """Hard limits for workbench traffic. `None` means no limit on that axis."""

    requests: Optional[int] = None
    bytes: Optional[int] = None
    seconds: Optional[float] = None
    concurrency: Optional[int] = None
    reset: str = RESET_DAILY
    timezone: str = 'UTC'
    #: What already-started work may still add after the limit is reached. Zero means a hard stop.
    inflight_reserve_requests: int = 1
    inflight_reserve_bytes: int = MIB
    inflight_reserve_seconds: float = 0.0
    inflight_reserve_concurrency: int = 0

    def __post_init__(self):
        for name in ('requests', 'bytes'):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ScheduleError('E_VALIDATION_FIELD', f'budgets.{name}',
                                    f'ожидалось целое >= 0, получено {value!r}',
                                    f'expected an integer >= 0, got {value!r}')
        if self.seconds is not None and (isinstance(self.seconds, bool)
                                         or not isinstance(self.seconds, (int, float)) or self.seconds < 0):
            raise ScheduleError('E_VALIDATION_FIELD', 'budgets.seconds',
                                f'ожидалось число >= 0, получено {self.seconds!r}',
                                f'expected a number >= 0, got {self.seconds!r}')
        for name in ('inflight_reserve_requests', 'inflight_reserve_bytes',
                     'inflight_reserve_concurrency'):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ScheduleError('E_VALIDATION_FIELD', f'budgets.{name}',
                                    f'ожидалось целое >= 0, получено {value!r}',
                                    f'expected an integer >= 0, got {value!r}')
        if self.concurrency is not None and (isinstance(self.concurrency, bool)
                                              or not isinstance(self.concurrency, int)
                                              or self.concurrency < 1):
            raise ScheduleError('E_VALIDATION_FIELD', 'budgets.concurrency',
                                f'ожидалось целое >= 1, получено {self.concurrency!r}',
                                f'expected an integer >= 1, got {self.concurrency!r}')
        if isinstance(self.inflight_reserve_seconds, bool) or self.inflight_reserve_seconds < 0:
            raise ScheduleError('E_VALIDATION_FIELD', 'budgets.inflight_reserve_seconds',
                                f'ожидалось число >= 0, получено {self.inflight_reserve_seconds!r}',
                                f'expected a number >= 0, got {self.inflight_reserve_seconds!r}')
        if self.reset not in RESET_RULES:
            raise ScheduleError('E_VALIDATION_FIELD', 'budgets.reset',
                                f'неизвестное правило сброса {self.reset!r}', f'unknown reset rule {self.reset!r}')

    @property
    def limited(self) -> bool:
        return any(value is not None for value in (self.requests, self.bytes, self.seconds, self.concurrency))

    def to_dict(self) -> dict:
        return {'requests': self.requests, 'bytes': self.bytes, 'seconds': self.seconds,
                'concurrency': self.concurrency, 'reset': self.reset, 'timezone': self.timezone,
                'inflight_reserve_requests': self.inflight_reserve_requests,
                'inflight_reserve_bytes': self.inflight_reserve_bytes,
                'inflight_reserve_seconds': self.inflight_reserve_seconds,
                'inflight_reserve_concurrency': self.inflight_reserve_concurrency}

    @classmethod
    def from_dict(cls, data: object, field_name: str = 'budgets') -> 'Budgets':
        if data is None:
            return cls()
        if not isinstance(data, Mapping):
            raise ScheduleError('E_VALIDATION_SCHEMA', field_name,
                                f'ожидался объект бюджета, получено {type(data).__name__}',
                                f'expected a budgets object, got {type(data).__name__}')
        known = {key: data[key] for key in
                 ('requests', 'bytes', 'seconds', 'concurrency', 'reset', 'timezone',
                  'inflight_reserve_requests', 'inflight_reserve_bytes',
                  'inflight_reserve_seconds', 'inflight_reserve_concurrency') if key in data}
        return cls(**known)


@dataclass
class BudgetCounters:
    """Persisted usage. Relay counters are cumulative and belong to the gateway."""

    requests: int = 0
    bytes: int = 0
    seconds: float = 0.0
    reserved_requests: int = 0
    reserved_bytes: int = 0
    reserved_seconds: float = 0.0
    active: int = 0
    relay_requests: int = 0
    relay_bytes: int = 0
    reset_key: str = ''
    reset_at: Optional[float] = None

    def to_dict(self) -> dict:
        return {'requests': self.requests, 'bytes': self.bytes, 'seconds': self.seconds,
                'reserved_requests': self.reserved_requests, 'reserved_bytes': self.reserved_bytes,
                'reserved_seconds': self.reserved_seconds, 'active': self.active,
                'relay_requests': self.relay_requests, 'relay_bytes': self.relay_bytes,
                'reset_key': self.reset_key, 'reset_at': self.reset_at}

    @classmethod
    def from_dict(cls, data: object) -> 'BudgetCounters':
        if not isinstance(data, Mapping):
            return cls()
        known = {key: data[key] for key in cls().to_dict() if key in data}
        return cls(**known)


@dataclass(frozen=True)
class LimitView:
    """One budget axis, including what is already promised but not yet committed."""

    limit: Optional[float]
    used: float
    reserved: float
    allowance: float

    @property
    def remaining(self) -> float:
        if self.limit is None:
            return float('inf')
        return max(0.0, self.limit - self.used - self.reserved)

    @property
    def in_flight(self) -> float:
        """How much of the outstanding work may still land past the limit.

        Bounded by the in-flight reserve by construction, and zero while the
        limit is untouched.
        """
        if self.limit is None:
            return 0.0
        return max(0.0, min(self.used + self.reserved, self.limit + self.allowance) - self.limit)

    @property
    def over_limit(self) -> bool:
        return self.limit is not None and self.used > self.limit

    @property
    def exhausted(self) -> bool:
        """Nothing more can be reserved, not even with the in-flight reserve."""
        return self.limit is not None and self.limit + self.allowance - self.used - self.reserved <= 0

    def to_dict(self) -> dict:
        return {'limit': self.limit, 'used': self.used, 'reserved': self.reserved,
                'remaining': None if self.limit is None else self.remaining,
                'allowance': self.allowance, 'over_limit': self.over_limit, 'exhausted': self.exhausted}


@dataclass(frozen=True)
class BudgetSnapshot:
    """Everything a UI needs to explain the budget state without guessing."""

    reset: str
    reset_at: Optional[float]
    requests: LimitView
    bytes: LimitView
    seconds: LimitView
    active: int
    concurrency: Optional[int]
    relay_requests: int
    relay_bytes: int
    stages: tuple[str, ...]
    exhausted: tuple[str, ...]

    def to_dict(self) -> dict:
        return {'reset': self.reset, 'reset_at': self.reset_at,
                'requests': self.requests.to_dict(), 'bytes': self.bytes.to_dict(),
                'seconds': self.seconds.to_dict(), 'active': self.active, 'concurrency': self.concurrency,
                'relay_requests': self.relay_requests, 'relay_bytes': self.relay_bytes,
                'stages': list(self.stages), 'exhausted': list(self.exhausted)}


@dataclass(frozen=True)
class BudgetDenial:
    """Why a reservation was refused, with the numbers that explain it."""

    code: str
    reason: str
    axis: str
    limit: Optional[float]
    used: float
    reserved: float
    requested: float
    remaining: float
    in_flight: float
    allowance: float
    stages: tuple[str, ...]

    def to_dict(self) -> dict:
        return {'code': self.code, 'reason': self.reason, 'axis': self.axis, 'limit': self.limit,
                'used': self.used, 'reserved': self.reserved, 'requested': self.requested,
                'remaining': self.remaining, 'in_flight': self.in_flight, 'allowance': self.allowance,
                'stages': list(self.stages)}

    def text(self) -> str:
        return message(self.code, self.axis, self.reason)


@dataclass(frozen=True)
class Reservation:
    """A promise to spend. Settle it with commit() or give it back with release()."""

    token: int
    traffic_class: str
    requests: int
    bytes: int
    seconds: float
    active: bool
    allowed: bool
    denial: Optional[BudgetDenial] = None

    @property
    def ok(self) -> bool:
        return self.allowed

    def to_dict(self) -> dict:
        return {'traffic_class': self.traffic_class, 'requests': self.requests, 'bytes': self.bytes,
                'seconds': self.seconds, 'allowed': self.allowed,
                'denial': self.denial.to_dict() if self.denial else None}


def reset_key(budgets: Budgets, at: float) -> str:
    """Bucket id of the period `at` belongs to, in the budget timezone."""
    if budgets.reset == RESET_NONE:
        return 'none'
    tz = _tz(budgets.timezone)
    moment = datetime.fromtimestamp(at, tz)
    if budgets.reset == RESET_HOURLY:
        return f'hourly:{moment.year:04d}-{moment.month:02d}-{moment.day:02d}T{moment.hour:02d}'
    if budgets.reset == RESET_DAILY:
        return f'daily:{moment.year:04d}-{moment.month:02d}-{moment.day:02d}'
    return f'monthly:{moment.year:04d}-{moment.month:02d}'


def next_reset_at(budgets: Budgets, at: float) -> Optional[float]:
    """Next moment the counters restart. A local midnight inside a DST gap shifts to the transition."""
    if budgets.reset == RESET_NONE:
        return None
    tz = _tz(budgets.timezone)
    moment = datetime.fromtimestamp(at, tz)
    if budgets.reset == RESET_HOURLY:
        start = moment.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    elif budgets.reset == RESET_DAILY:
        start = moment.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    else:
        start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        start = (start.replace(year=start.year + 1) if start.month == 12 else start.replace(month=start.month + 1))
    naive = datetime(start.year, start.month, start.day, start.hour)
    found = local_instants(tz, naive)
    instant = found[0] if found else dst_gap_transition(tz, naive)
    return instant.timestamp()


class BudgetLedger:
    """Hard limits with a bounded in-flight reserve.

    A reservation that is already open may settle even after the limit is
    reached, but only inside the reserve. Everything beyond it is refused with
    the numbers that explain the refusal, so the overrun is finite and visible.
    """

    def __init__(self, budgets: Budgets, counters: Optional[BudgetCounters] = None, now: float = 0.0,
                 allowed_classes: Optional[Iterable[str]] = None):
        self.budgets = budgets
        self.counters = counters if counters is not None else BudgetCounters()
        self.stages = tuple(allowed_classes) if allowed_classes is not None else WORKBENCH_CLASSES
        self._open: dict[int, Reservation] = {}
        self._sequence = 0
        self.maybe_reset(now)

    def maybe_reset(self, now: float) -> bool:
        """Restart the counters when the period changed. Returns True when they were reset."""
        key = reset_key(self.budgets, now)
        if self.counters.reset_key == key:
            self.counters.reset_at = next_reset_at(self.budgets, now)
            return False
        relay_requests, relay_bytes = self.counters.relay_requests, self.counters.relay_bytes
        self.counters = BudgetCounters(relay_requests=relay_requests, relay_bytes=relay_bytes,
                                       reset_key=key, reset_at=next_reset_at(self.budgets, now))
        return True

    def _limits(self) -> list[tuple[str, Optional[float], float, float, float]]:
        """(axis, limit, used, reserved, allowance) for every finite axis."""
        return [
            ('requests', self.budgets.requests, self.counters.requests, self.counters.reserved_requests,
             float(self.budgets.inflight_reserve_requests)),
            ('bytes', self.budgets.bytes, self.counters.bytes, self.counters.reserved_bytes,
             float(self.budgets.inflight_reserve_bytes)),
            ('seconds', self.budgets.seconds, self.counters.seconds, self.counters.reserved_seconds,
             float(self.budgets.inflight_reserve_seconds)),
            ('concurrency', self.budgets.concurrency, float(self.counters.active), 0.0,
             float(self.budgets.inflight_reserve_concurrency)),
        ]

    def try_reserve(self, traffic_class: str, *, requests: int = 0, bytes: int = 0, seconds: float = 0.0,
                    active: bool = True) -> Reservation:
        """Reserve budget, or explain why it is not available."""
        self._sequence += 1
        token = self._sequence
        if traffic_class not in ALL_CLASSES:
            return self._deny(token, traffic_class, 'E_VALIDATION_FIELD', 'unknown_traffic_class',
                              'traffic_class', None, 0.0, 0.0, 0.0, 0.0, 0.0)
        if traffic_class != RELAY and traffic_class not in self.stages:
            return self._deny(token, traffic_class, 'E_LIMIT_BUDGET', REASON_STAGE_NOT_ALLOWED,
                              'stages', None, 0.0, 0.0, 0.0, 0.0, 0.0)
        asked = {'requests': float(requests), 'bytes': float(bytes), 'seconds': float(seconds),
                 'concurrency': 1.0 if active else 0.0}
        # Relay traffic has its own accounting and never touches a workbench limit.
        axes = self._limits() if traffic_class != RELAY else [self._limits()[3]]
        for axis, limit, used, reserved, allowance in axes:
            if limit is None or asked[axis] <= 0:
                continue
            remaining = max(0.0, limit - used - reserved)
            if used + reserved + asked[axis] > limit + allowance:
                return self._deny(token, traffic_class, 'E_LIMIT_BUDGET', f'{axis}_exhausted', axis,
                                  limit, used, reserved, asked[axis], remaining, allowance)
        if traffic_class == RELAY:
            self.counters.relay_requests += requests
            self.counters.relay_bytes += bytes
        else:
            self.counters.reserved_requests += requests
            self.counters.reserved_bytes += bytes
            self.counters.reserved_seconds += seconds
            if active:
                self.counters.active += 1
        reservation = Reservation(token=token, traffic_class=traffic_class, requests=requests,
                                  bytes=bytes, seconds=seconds, active=active, allowed=True)
        self._open[token] = reservation
        return reservation

    def _deny(self, token: int, traffic_class: str, code: str, reason: str, axis: str,
              limit: Optional[float], used: float, reserved: float, requested: float,
              remaining: float, allowance: float) -> Reservation:
        view = LimitView(limit=limit, used=used, reserved=reserved, allowance=allowance)
        denial = BudgetDenial(code=code, reason=reason, axis=axis, limit=limit, used=used,
                              reserved=reserved, requested=requested, remaining=remaining,
                              in_flight=view.in_flight, allowance=allowance, stages=self.stages)
        return Reservation(token=token, traffic_class=traffic_class, requests=0, bytes=0, seconds=0.0,
                           active=False, allowed=False, denial=denial)

    def commit(self, reservation: Reservation, *, requests: Optional[int] = None,
               bytes: Optional[int] = None, seconds: Optional[float] = None) -> None:
        """Settle a reservation with the real cost. Overshoot above the reserve is recorded, not hidden."""
        if reservation.token not in self._open:
            raise ValueError('reservation already settled')
        if reservation.traffic_class == RELAY:
            # Relay was counted at reserve time; correct the delta only.
            self.counters.relay_requests += (requests or 0) - reservation.requests
            self.counters.relay_bytes += (bytes or 0) - reservation.bytes
        else:
            self.counters.reserved_requests -= reservation.requests
            self.counters.reserved_bytes -= reservation.bytes
            self.counters.reserved_seconds -= reservation.seconds
            self.counters.requests += reservation.requests if requests is None else requests
            self.counters.bytes += reservation.bytes if bytes is None else bytes
            self.counters.seconds += reservation.seconds if seconds is None else seconds
            if reservation.active:
                self.counters.active = max(0, self.counters.active - 1)
        del self._open[reservation.token]

    def release(self, reservation: Reservation) -> None:
        """Give back an unused reservation without spending anything."""
        if reservation.token not in self._open:
            return
        if reservation.traffic_class != RELAY:
            self.counters.reserved_requests -= reservation.requests
            self.counters.reserved_bytes -= reservation.bytes
            self.counters.reserved_seconds -= reservation.seconds
            if reservation.active:
                self.counters.active = max(0, self.counters.active - 1)
        else:
            self.counters.relay_requests -= reservation.requests
            self.counters.relay_bytes -= reservation.bytes
        del self._open[reservation.token]

    def stage_allowed(self, traffic_class: str) -> bool:
        return traffic_class == RELAY or traffic_class in self.stages

    def snapshot(self, now: Optional[float] = None) -> BudgetSnapshot:
        views = {}
        exhausted = []
        for axis, limit, used, reserved, allowance in self._limits():
            view = LimitView(limit=limit, used=used, reserved=reserved, allowance=allowance)
            views[axis] = view
            if view.exhausted:
                exhausted.append(axis)
        return BudgetSnapshot(
            reset=self.budgets.reset, reset_at=self.counters.reset_at,
            requests=views['requests'], bytes=views['bytes'], seconds=views['seconds'],
            active=self.counters.active, concurrency=self.budgets.concurrency,
            relay_requests=self.counters.relay_requests, relay_bytes=self.counters.relay_bytes,
            stages=self.stages, exhausted=tuple(exhausted))


# ------------------------------------------------------------ power / metered


@dataclass(frozen=True)
class PowerSignal:
    """What the operating system tells us about power and network cost.

    `*_supported` is the honest part: an OS that exposes nothing leaves the
    value None with the flag False, and no policy is invented from that.
    """

    on_battery: Optional[bool] = None
    battery_percent: Optional[int] = None
    metered: Optional[bool] = None
    battery_supported: bool = False
    metered_supported: bool = False
    source: str = 'none'

    def to_dict(self) -> dict:
        return {'on_battery': self.on_battery, 'battery_percent': self.battery_percent,
                'metered': self.metered, 'battery_supported': self.battery_supported,
                'metered_supported': self.metered_supported, 'source': self.source}

    @classmethod
    def unknown(cls) -> 'PowerSignal':
        return cls()


@dataclass(frozen=True)
class PowerPolicy:
    """What to do when the OS does say something. Off by default, as F15 requires."""

    on_battery: str = POWER_IGNORE
    battery_min_percent: Optional[int] = None
    metered: str = POWER_IGNORE
    unknown_metered: str = UNKNOWN_AS_METERED

    def __post_init__(self):
        if self.on_battery not in BATTERY_POLICIES:
            raise ScheduleError('E_VALIDATION_FIELD', 'power.on_battery',
                                f'неизвестная политика {self.on_battery!r}', f'unknown policy {self.on_battery!r}')
        if self.metered not in METERED_POLICIES:
            raise ScheduleError('E_VALIDATION_FIELD', 'power.metered',
                                f'неизвестная политика {self.metered!r}', f'unknown policy {self.metered!r}')
        if self.unknown_metered not in UNKNOWN_METERED_POLICIES:
            raise ScheduleError('E_VALIDATION_FIELD', 'power.unknown_metered',
                                f'неизвестная политика {self.unknown_metered!r}',
                                f'unknown policy {self.unknown_metered!r}')
        if self.battery_min_percent is not None and not 0 <= self.battery_min_percent <= 100:
            raise ScheduleError('E_VALIDATION_FIELD', 'power.battery_min_percent',
                                'ожидалось 0..100', 'expected 0..100')

    def to_dict(self) -> dict:
        return {'on_battery': self.on_battery, 'battery_min_percent': self.battery_min_percent,
                'metered': self.metered, 'unknown_metered': self.unknown_metered}

    @classmethod
    def from_dict(cls, data: object, field_name: str = 'power') -> 'PowerPolicy':
        if data is None:
            return cls()
        if not isinstance(data, Mapping):
            raise ScheduleError('E_VALIDATION_SCHEMA', field_name,
                                f'ожидался объект политики, получено {type(data).__name__}',
                                f'expected a policy object, got {type(data).__name__}')
        known = {key: data[key] for key in cls().to_dict() if key in data}
        return cls(**known)


@dataclass(frozen=True)
class PowerDecision:
    """Whether the OS signal restricts the work, and which stages may still run."""

    restricted: bool
    reason: str
    stages: tuple[str, ...]
    signal: PowerSignal

    def to_dict(self) -> dict:
        return {'restricted': self.restricted, 'reason': self.reason, 'stages': list(self.stages),
                'signal': self.signal.to_dict()}


POWER_SIGNAL_ABSENT = 'power_signal_absent'
POWER_BATTERY = 'battery_restriction'
POWER_METERED = 'metered_restriction'
POWER_METERED_UNKNOWN = 'metered_unknown_conservative'
POWER_NONE = 'power_policy_off'


def power_decision(policy: PowerPolicy, signal: PowerSignal) -> PowerDecision:
    """Apply the metered/battery policy to what the OS reported.

    No signal means no restriction: F15 applies these policies only where the
    OS provides one. A signal that exists but says "unknown" is treated as the
    conservative default, and the reason says so.
    """
    stages = WORKBENCH_CLASSES
    if policy.on_battery == POWER_IGNORE and policy.metered == POWER_IGNORE:
        return PowerDecision(False, POWER_NONE, stages, signal)

    if signal.battery_supported and signal.on_battery:
        if policy.on_battery == POWER_PAUSE:
            return PowerDecision(True, POWER_BATTERY, stages, signal)
        if (policy.on_battery == POWER_BELOW_PERCENT and policy.battery_min_percent is not None
                and signal.battery_percent is not None and signal.battery_percent < policy.battery_min_percent):
            return PowerDecision(True, POWER_BATTERY, stages, signal)

    if signal.metered_supported:
        metered = signal.metered
        reason = POWER_METERED
        if metered is None:
            if policy.unknown_metered == UNKNOWN_AS_UNMETERED:
                return PowerDecision(False, POWER_NONE, stages, signal)
            metered, reason = True, POWER_METERED_UNKNOWN
        if metered:
            if policy.metered == POWER_PAUSE:
                return PowerDecision(True, reason, stages, signal)
            if policy.metered == POWER_CHEAP_ONLY:
                return PowerDecision(False, reason, CHEAP_CLASSES, signal)
    return PowerDecision(False, POWER_SIGNAL_ABSENT, stages, signal)


def _linux_power(sysfs: Path) -> PowerSignal:
    on_battery: Optional[bool] = None
    percent: Optional[int] = None
    try:
        supplies = sorted(path for path in sysfs.iterdir() if path.is_dir())
    except OSError:
        return PowerSignal()
    for supply in supplies:
        try:
            kind = (supply / 'type').read_text().strip()
        except OSError:
            continue
        if kind == 'Mains':
            try:
                online = (supply / 'online').read_text().strip() == '1'
            except OSError:
                continue
            on_battery = not online
        elif kind == 'Battery':
            try:
                percent = int((supply / 'capacity').read_text().strip())
            except (OSError, ValueError):
                percent = None
    if on_battery is None and percent is None:
        return PowerSignal()
    # Linux exposes no portable network-cost signal; we do not invent one.
    return PowerSignal(on_battery=on_battery, battery_percent=percent, metered=None,
                       battery_supported=True, metered_supported=False, source='sysfs')


def _macos_power(runner: Callable[..., object]) -> PowerSignal:
    try:
        finished = runner(['pmset', '-g', 'batt'], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return PowerSignal()
    if getattr(finished, 'returncode', 1) != 0:
        return PowerSignal()
    on_battery: Optional[bool] = None
    percent: Optional[int] = None
    for line in (getattr(finished, 'stdout', '') or '').splitlines():
        text = line.strip()
        if text.startswith('Now drawing from'):
            on_battery = 'Battery Power' in text
        elif on_battery is not None and percent is None and '%' in text:
            try:
                percent = int(text.rsplit('%', 1)[0].strip().split()[-1])
            except ValueError:
                percent = None
    if on_battery is None:
        return PowerSignal()
    # pmset does not report network cost, so the metered policy has no signal to act on here.
    return PowerSignal(on_battery=on_battery, battery_percent=percent, metered=None,
                       battery_supported=True, metered_supported=False, source='pmset')


def read_system_signal(platform: str = sys.platform, sysfs: Path = Path('/sys/class/power_supply'),
                       runner: Callable[..., object] = subprocess.run) -> PowerSignal:
    """Ask the OS. Unknown everywhere else; never raises."""
    try:
        if platform.startswith('linux'):
            return _linux_power(sysfs)
        if platform == 'darwin':
            return _macos_power(runner)
    except Exception:  # noqa: BLE001 - an OS probe must never break the scheduler
        return PowerSignal()
    return PowerSignal()


# -------------------------------------------------------------- notifications


@dataclass(frozen=True)
class Target:
    """A delivery destination the user selected. Only a label: no address, no secret."""

    label: str
    enabled: bool = False
    user_confirmed: bool = False

    def to_dict(self) -> dict:
        return {'label': self.label, 'enabled': self.enabled, 'user_confirmed': self.user_confirmed}


@dataclass(frozen=True)
class NotificationConfig:
    """Which channels the user turned on. External channels are silent until configured."""

    in_app: bool = True
    os_notifications: bool = False
    email: Optional[Target] = None
    webhook: Optional[Target] = None

    def channel_enabled(self, channel: str) -> bool:
        if channel == CHANNEL_IN_APP:
            return self.in_app
        if channel == CHANNEL_OS:
            return self.os_notifications
        target = self.email if channel == CHANNEL_EMAIL else self.webhook if channel == CHANNEL_WEBHOOK else None
        if channel not in EXTERNAL_CHANNELS:
            return False
        return bool(target and target.enabled and target.user_confirmed)

    def skip_reason(self, channel: str) -> str:
        if channel == CHANNEL_IN_APP:
            return '' if self.in_app else SKIP_NO_CHANNEL
        if channel == CHANNEL_OS:
            return '' if self.os_notifications else SKIP_NO_CHANNEL
        if channel not in EXTERNAL_CHANNELS:
            return SKIP_NO_CHANNEL
        target = self.email if channel == CHANNEL_EMAIL else self.webhook
        if target is None:
            return SKIP_NO_TARGET
        if not target.enabled:
            return SKIP_TARGET_OFF
        if not target.user_confirmed:
            return SKIP_NOT_CONFIRMED
        return ''

    def to_dict(self) -> dict:
        return {'in_app': self.in_app, 'os_notifications': self.os_notifications,
                'email': self.email.to_dict() if self.email else None,
                'webhook': self.webhook.to_dict() if self.webhook else None}

    @classmethod
    def from_dict(cls, data: object) -> 'NotificationConfig':
        if not isinstance(data, Mapping):
            return cls()
        def target(key: str) -> Optional[Target]:
            value = data.get(key)
            if not isinstance(value, Mapping):
                return None
            return Target(label=str(value.get('label') or key),
                          enabled=bool(value.get('enabled', False)),
                          user_confirmed=bool(value.get('user_confirmed', False)))
        return cls(in_app=bool(data.get('in_app', True)),
                   os_notifications=bool(data.get('os_notifications', False)),
                   email=target('email'), webhook=target('webhook'))


@dataclass(frozen=True)
class Notification:
    """A state change worth telling the user about. `data` holds plain numbers only."""

    subject: str
    code: str
    at: float
    severity: str = SEVERITY_INFO
    data: tuple[tuple[str, object], ...] = ()

    @property
    def dedup_key(self) -> str:
        return f'{self.subject}:{self.code}'

    def get(self, key: str, default: object = None) -> object:
        return dict(self.data).get(key, default)

    def text(self) -> str:
        values = ', '.join(f'{key}={value}' for key, value in self.data)
        return message(self.code, self.subject, self.get('reason', '')) + (f' [{values}]' if values else '')

    def to_dict(self) -> dict:
        return {'subject': self.subject, 'code': self.code, 'at': self.at, 'severity': self.severity,
                'data': dict(self.data)}


@dataclass(frozen=True)
class Delivery:
    """Proof that a channel was asked to send. The body is the localized text, nothing else."""

    channel: str
    target_ref: str
    notification: Notification
    at: float

    def to_dict(self) -> dict:
        return {'channel': self.channel, 'target_ref': self.target_ref, 'at': self.at,
                'code': self.notification.code, 'subject': self.notification.subject,
                'severity': self.notification.severity, 'data': dict(self.notification.data)}


@dataclass(frozen=True)
class DispatchReport:
    deliveries: tuple[Delivery, ...] = ()
    skipped: tuple[tuple[str, str], ...] = ()

    def to_dict(self) -> dict:
        return {'deliveries': [{'channel': item.channel, 'target_ref': item.target_ref,
                                'code': item.notification.code, 'subject': item.notification.subject}
                               for item in self.deliveries],
                'skipped': [{'channel': channel, 'reason': reason} for channel, reason in self.skipped]}


class Channel(Protocol):
    """Delivery implementation. The owner of the channel holds the address and any secret."""

    name: str

    def send(self, notification: Notification) -> None: ...


class Dispatcher:
    """Hands a notification to the channels the user turned on.

    Nothing leaves the process unless a channel is enabled, has a target and the
    target was confirmed by the user; every other case is reported as skipped
    with a reason instead of being sent quietly.
    """

    def __init__(self, config: Optional[NotificationConfig] = None,
                 channels: Optional[Mapping[str, Channel]] = None):
        self.config = config or NotificationConfig()
        self.channels = dict(channels or {})

    def dispatch(self, notification: Notification) -> DispatchReport:
        delivered: list[Delivery] = []
        skipped: list[tuple[str, str]] = []
        candidates = [CHANNEL_IN_APP, CHANNEL_OS, *EXTERNAL_CHANNELS]
        for channel in candidates:
            if not self.config.channel_enabled(channel):
                skipped.append((channel, self.config.skip_reason(channel) or SKIP_NO_CHANNEL))
                continue
            implementation = self.channels.get(channel)
            if implementation is None:
                skipped.append((channel, SKIP_NO_IMPLEMENTATION))
                continue
            target = self.config.email if channel == CHANNEL_EMAIL else self.config.webhook
            implementation.send(notification)
            delivered.append(Delivery(channel=channel, target_ref=target.label if target else channel,
                                      notification=notification, at=notification.at))
        return DispatchReport(tuple(delivered), tuple(skipped))


@dataclass(frozen=True)
class NotifyPolicy:
    """Dedup and hysteresis for one watched subject.

    `enter_below` opens an alert, `exit_above` closes it: values between the two
    are the dead band that stops flapping. `min_events` requires that many
    confirming observations before the first alert, so a single noisy sample
    says nothing.
    """

    enter_below: Optional[float] = None
    exit_above: Optional[float] = None
    min_events: int = 1
    repeat_window_s: float = 1800.0
    reset_after_s: float = 21600.0
    alert_on_unknown: bool = False

    def __post_init__(self):
        if self.min_events < 1:
            raise ScheduleError('E_VALIDATION_FIELD', 'notify.min_events',
                                'ожидалось >= 1', 'expected >= 1')
        if (self.enter_below is not None and self.exit_above is not None
                and self.exit_above < self.enter_below):
            raise ScheduleError('E_VALIDATION_FIELD', 'notify.exit_above',
                                'порог восстановления ниже порога тревоги',
                                'recovery threshold is below the alert threshold')
        if self.repeat_window_s < 0 or self.reset_after_s < 0:
            raise ScheduleError('E_VALIDATION_FIELD', 'notify.repeat_window_s',
                                'ожидалось >= 0', 'expected >= 0')

    def to_dict(self) -> dict:
        return {'enter_below': self.enter_below, 'exit_above': self.exit_above, 'min_events': self.min_events,
                'repeat_window_s': self.repeat_window_s, 'reset_after_s': self.reset_after_s,
                'alert_on_unknown': self.alert_on_unknown}

    @classmethod
    def from_dict(cls, data: object, field_name: str = 'notify') -> 'NotifyPolicy':
        if data is None:
            return cls()
        if not isinstance(data, Mapping):
            raise ScheduleError('E_VALIDATION_SCHEMA', field_name,
                                f'ожидался объект уведомлений, получено {type(data).__name__}',
                                f'expected a notify object, got {type(data).__name__}')
        known = {key: data[key] for key in cls().to_dict() if key in data}
        return cls(**known)


@dataclass(frozen=True)
class Change:
    """A state transition of a watched subject, already debounced."""

    subject: str
    from_state: str
    to_state: str
    at: float
    value: Optional[float] = None

    def to_dict(self) -> dict:
        return {'subject': self.subject, 'from': self.from_state, 'to': self.to_state, 'at': self.at,
                'value': self.value}


STATE_OK = 'ok'
STATE_ALERT = 'alert'
STATE_UNKNOWN = 'unknown'


class StateWatcher:
    """Turns a stream of observations into at most two notifications per episode.

    Ten identical degraded observations produce one change, and recovery
    produces the second. A value oscillating inside the hysteresis band produces
    none at all.
    """

    def __init__(self, subject: str, policy: NotifyPolicy, state: str = STATE_OK):
        self.subject = subject
        self.policy = policy
        self.state = state
        self._pending = 0
        self._last_value: Optional[float] = None
        self.observations = 0
        #: How many state changes this watcher has produced. A count, not a history:
        #: an in-app process may tick for weeks and must not grow a list per episode.
        self.episodes = 0

    def _would_alert(self, value: Optional[float]) -> bool:
        if value is None:
            return self.policy.alert_on_unknown
        if self.state == STATE_ALERT:
            return self.policy.exit_above is None or value < self.policy.exit_above
        if self.policy.enter_below is None:
            return False
        return value < self.policy.enter_below

    def observe(self, value: Optional[float], at: float) -> Optional[Change]:
        """Feed one observation. Returns a Change only when the state actually moved."""
        self.observations += 1
        self._last_value = value
        alerting = self._would_alert(value)
        if not alerting:
            self._pending = 0
            if self.state == STATE_ALERT:
                change = Change(self.subject, STATE_ALERT, STATE_OK, at, value)
                self.state = STATE_OK
                self.episodes += 1
                return change
            if self.state == STATE_UNKNOWN and value is not None:
                self.state = STATE_OK
            return None
        if self.state == STATE_ALERT:
            self._pending = 0
            return None
        self._pending += 1
        if self._pending < self.policy.min_events:
            return None
        self._pending = 0
        self.state = STATE_ALERT
        self.episodes += 1
        return Change(self.subject, STATE_OK, STATE_ALERT, at, value)

    def pending(self) -> int:
        return self._pending

    def last_value(self) -> Optional[float]:
        return self._last_value


class Notifier:
    """Deduplicates notifications and hands them to the dispatcher.

    The same subject and code fire once per `repeat_window_s`, and a repeat is
    possible again only after `reset_after_s` of silence. Suppressed events are
    counted, so the UI can show that something happened without spamming.
    """

    def __init__(self, dispatcher: Optional[Dispatcher] = None, config: Optional[NotificationConfig] = None,
                 now: Callable[[], float] = time.time):
        self.dispatcher = dispatcher or Dispatcher(config)
        self.now = now
        #: Bounded so a long-running process does not keep a growing audit trail in memory.
        self.sent: deque[Notification] = deque(maxlen=NOTIFIER_HISTORY)
        self.suppressed: dict[str, int] = {}
        self.reports: deque[DispatchReport] = deque(maxlen=NOTIFIER_HISTORY)
        self._last_sent: dict[str, float] = {}
        self._last_seen: dict[str, float] = {}

    def wants(self, change: Change, policy: NotifyPolicy) -> bool:
        key = f'{change.subject}:{change.to_state}'
        at = self.now()
        last = self._last_sent.get(key)
        if last is not None and at - last < policy.repeat_window_s:
            return False
        seen = self._last_seen.get(key)
        if (last is not None and seen is not None and at - seen < policy.repeat_window_s
                and at - last >= policy.reset_after_s):
            return False
        return True

    def emit(self, notification: Notification, policy: Optional[NotifyPolicy] = None) -> DispatchReport:
        """Send once per window, otherwise count it as suppressed."""
        policy = policy or NotifyPolicy()
        at = self.now()
        key = notification.dedup_key
        self._last_seen[key] = at
        last = self._last_sent.get(key)
        if last is not None and at - last < policy.repeat_window_s:
            self.suppressed[key] = self.suppressed.get(key, 0) + 1
            return DispatchReport()
        self._last_sent[key] = at
        self.sent.append(notification)
        report = self.dispatcher.dispatch(notification)
        self.reports.append(report)
        return report

    def for_change(self, change: Change, policy: NotifyPolicy, severity: str = SEVERITY_WARNING) -> DispatchReport:
        """Translate a state change into the matching notification code."""
        if not self.wants(change, policy):
            self.suppressed[f'{change.subject}:{change.to_state}'] = \
                self.suppressed.get(f'{change.subject}:{change.to_state}', 0) + 1
            return DispatchReport()
        code = NOTIFY_BELOW_MINIMUM if change.to_state == STATE_ALERT else NOTIFY_RECOVERED
        notification = Notification(subject=change.subject, code=code, at=change.at, severity=severity,
                                    data=(('reason', change.to_state), ('value', change.value),
                                          ('from', change.from_state)))
        return self.emit(notification, policy)

    def suppressed_count(self, key: Optional[str] = None) -> int:
        if key is None:
            return sum(self.suppressed.values())
        return self.suppressed.get(key, 0)


# ----------------------------------------------------------------- spec/store


@dataclass(frozen=True)
class ScheduleSpec:
    """A persisted schedule: when work may run and under which limits."""

    id: str
    kind: str = KIND_INTERVAL
    interval_minutes: Optional[float] = None
    windows: tuple[Window, ...] = ()
    slots: tuple[Slot, ...] = ()
    timezone: str = 'UTC'
    quiet_hours: tuple[Window, ...] = ()
    budgets: Budgets = field(default_factory=Budgets)
    power: PowerPolicy = field(default_factory=PowerPolicy)
    notify: NotifyPolicy = field(default_factory=NotifyPolicy)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    enabled: bool = True
    catch_up: bool = False
    max_catch_up: int = 1
    catch_up_grace_s: float = 0.0
    wake_gap_s: float = 0.0
    dst_policy: str = DST_SKIP
    pool_id: Optional[str] = None

    @property
    def effective_wake_gap_s(self) -> float:
        """Gap that counts as sleep.

        Zero means "auto": four intervals for an interval schedule, an hour for
        slot schedules that have no interval to relate the tick cadence to.
        """
        if self.wake_gap_s > 0:
            return self.wake_gap_s
        if self.interval_s:
            return max(4.0 * self.interval_s, 900.0)
        return 3600.0

    def __post_init__(self):
        if not self.id or not str(self.id).strip():
            raise ScheduleError('E_VALIDATION_FIELD', 'id', 'нужен идентификатор', 'need an id')
        if self.kind not in SCHEDULE_KINDS:
            raise ScheduleError('E_VALIDATION_FIELD', 'kind',
                                f'неизвестный вид расписания {self.kind!r}', f'unknown schedule kind {self.kind!r}')
        if self.kind == KIND_INTERVAL:
            if self.interval_minutes is None or self.interval_minutes <= 0 or self.interval_minutes > 1440:
                raise ScheduleError('E_VALIDATION_FIELD', 'interval_minutes',
                                    'ожидалось 0 < интервал <= 1440 минут',
                                    'expected 0 < interval <= 1440 minutes')
        elif not self.slots:
            raise ScheduleError('E_VALIDATION_FIELD', 'slots',
                                'для вида slots нужен хотя бы один слот',
                                'kind=slots needs at least one slot')
        if not isinstance(self.max_catch_up, int) or self.max_catch_up < 1:
            raise ScheduleError('E_VALIDATION_FIELD', 'max_catch_up', 'ожидалось целое >= 1', 'expected an integer >= 1')
        if self.wake_gap_s < 0 or self.catch_up_grace_s < 0:
            raise ScheduleError('E_VALIDATION_FIELD', 'wake_gap_s', 'ожидалось >= 0', 'expected >= 0')
        if self.dst_policy not in DST_POLICIES:
            raise ScheduleError('E_VALIDATION_FIELD', 'dst_policy',
                                f'неизвестная политика {self.dst_policy!r}', f'unknown policy {self.dst_policy!r}')
        _tz(self.timezone)

    @property
    def interval_s(self) -> Optional[float]:
        return None if self.interval_minutes is None else float(self.interval_minutes) * 60.0

    def to_dict(self) -> dict:
        return {
            'id': self.id, 'kind': self.kind, 'interval_minutes': self.interval_minutes,
            'windows': [item.to_dict() for item in self.windows],
            'slots': [item.to_dict() for item in self.slots],
            'timezone': self.timezone,
            'quiet_hours': [item.to_dict() for item in self.quiet_hours],
            'budgets': self.budgets.to_dict(), 'power': self.power.to_dict(), 'notify': self.notify.to_dict(),
            'notifications': self.notifications.to_dict(), 'enabled': self.enabled,
            'catch_up': self.catch_up, 'max_catch_up': self.max_catch_up,
            'catch_up_grace_s': self.catch_up_grace_s, 'wake_gap_s': self.wake_gap_s,
            'dst_policy': self.dst_policy, 'pool_id': self.pool_id,
        }

    @classmethod
    def from_dict(cls, data: object) -> 'ScheduleSpec':
        if not isinstance(data, Mapping):
            raise ScheduleError('E_VALIDATION_SCHEMA', 'schedule',
                                f'ожидался объект расписания, получено {type(data).__name__}',
                                f'expected a schedule object, got {type(data).__name__}')
        known = {item.name for item in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ScheduleError('E_VALIDATION_UNKNOWN_FIELD', 'schedule',
                                f'неизвестные поля: {", ".join(sorted(unknown))}',
                                f'unknown fields: {", ".join(sorted(unknown))}')
        if 'id' not in data:
            raise ScheduleError('E_VALIDATION_FIELD', 'id', 'нужен идентификатор', 'need an id')
        windows = tuple(Window.from_dict(item, 'windows') for item in data.get('windows') or ())
        quiet = tuple(Window.from_dict(item, 'quiet_hours') for item in data.get('quiet_hours') or ())
        slots = tuple(Slot.from_dict(item, 'slots') for item in data.get('slots') or ())
        return cls(
            id=str(data['id']), kind=str(data.get('kind') or KIND_INTERVAL),
            interval_minutes=data.get('interval_minutes'), windows=windows, slots=slots,
            timezone=str(data.get('timezone') or 'UTC'), quiet_hours=quiet,
            budgets=Budgets.from_dict(data.get('budgets')), power=PowerPolicy.from_dict(data.get('power')),
            notify=NotifyPolicy.from_dict(data.get('notify')),
            notifications=NotificationConfig.from_dict(data.get('notifications')),
            enabled=bool(data.get('enabled', True)), catch_up=bool(data.get('catch_up', False)),
            max_catch_up=int(data.get('max_catch_up') or 1),
            catch_up_grace_s=float(data.get('catch_up_grace_s') or 0.0),
            wake_gap_s=float(data.get('wake_gap_s') or 0.0),
            dst_policy=str(data.get('dst_policy') or DST_SKIP),
            pool_id=data.get('pool_id'),
        )


@dataclass
class ScheduleState:
    """Runtime state of a schedule. Persisted through the store seam."""

    last_run_at: Optional[float] = None
    next_run_at: Optional[float] = None
    activated_at: Optional[float] = None
    paused: bool = False
    pause_reason: Optional[str] = None
    resume_at: Optional[float] = None
    runs: int = 0
    skipped: int = 0
    counters: BudgetCounters = field(default_factory=BudgetCounters)

    def to_dict(self) -> dict:
        return {'last_run_at': self.last_run_at, 'next_run_at': self.next_run_at,
                'activated_at': self.activated_at, 'paused': self.paused, 'pause_reason': self.pause_reason,
                'resume_at': self.resume_at, 'runs': self.runs, 'skipped': self.skipped,
                'counters': self.counters.to_dict()}

    @classmethod
    def from_dict(cls, data: object) -> 'ScheduleState':
        if not isinstance(data, Mapping):
            return cls()
        known = {key: data[key] for key in ('last_run_at', 'next_run_at', 'activated_at', 'paused',
                                            'pause_reason', 'resume_at', 'runs', 'skipped') if key in data}
        return cls(counters=BudgetCounters.from_dict(data.get('counters')), **known)


@dataclass(frozen=True)
class RunRequest:
    """An instruction to run something. The job layer (F11) executes it.

    `run_id` is derived from the schedule and the planned instant, so a repeated
    tick produces the same id and the caller can treat it as idempotent.
    """

    schedule_id: str
    run_id: str
    reason: str
    scheduled_for: float
    requested_at: float
    missed: int = 0
    coalesced: bool = False
    pool_id: Optional[str] = None
    stages: tuple[str, ...] = WORKBENCH_CLASSES
    budgets: Budgets = field(default_factory=Budgets)

    def to_dict(self) -> dict:
        return {'schedule_id': self.schedule_id, 'run_id': self.run_id, 'reason': self.reason,
                'scheduled_for': self.scheduled_for, 'requested_at': self.requested_at, 'missed': self.missed,
                'coalesced': self.coalesced, 'pool_id': self.pool_id, 'stages': list(self.stages),
                'budgets': self.budgets.to_dict()}


@dataclass(frozen=True)
class Decision:
    """Why a tick did or did not produce a run, with the numbers to explain it."""

    schedule_id: str
    action: str
    reason: str
    at: float
    next_run_at: Optional[float] = None
    resume_at: Optional[float] = None
    missed: int = 0
    detail: tuple[tuple[str, object], ...] = ()

    def to_dict(self) -> dict:
        return {'schedule_id': self.schedule_id, 'action': self.action, 'reason': self.reason, 'at': self.at,
                'next_run_at': self.next_run_at, 'resume_at': self.resume_at, 'missed': self.missed,
                'detail': dict(self.detail)}

    def get(self, key: str, default: object = None) -> object:
        return dict(self.detail).get(key, default)


@dataclass(frozen=True)
class TickReport:
    """Everything one tick decided. Nothing was executed by the module."""

    at: float
    decisions: tuple[Decision, ...] = ()
    run_requests: tuple[RunRequest, ...] = ()
    woke: bool = False

    def to_dict(self) -> dict:
        return {'at': self.at, 'woke': self.woke,
                'decisions': [item.to_dict() for item in self.decisions],
                'run_requests': [item.to_dict() for item in self.run_requests]}


@dataclass(frozen=True)
class Plan:
    """What the calendar says at one instant, before policy is applied.

    `moments` are the planned instants that are due, oldest first and already
    filtered by the windows. `overdue` counts every occurrence that came due
    since the last run, so the caller can report how much was merged or skipped.
    """

    reason: str
    moments: tuple[float, ...] = ()
    overdue: int = 0
    next_at: Optional[float] = None
    resume_at: Optional[float] = None
    dst_skipped: int = 0
    truncated: bool = False

    def to_dict(self) -> dict:
        return {'reason': self.reason, 'moments': list(self.moments), 'overdue': self.overdue,
                'next_at': self.next_at, 'resume_at': self.resume_at, 'dst_skipped': self.dst_skipped,
                'truncated': self.truncated}


class ScheduleStore(Protocol):
    """Persistence seam. The module writes no DDL: an implementation stores what
    `db.migrate()` already created (CONTRACTS 3.3, migration 7)."""

    def save_spec(self, spec: ScheduleSpec) -> None: ...

    def load_specs(self) -> list[ScheduleSpec]: ...

    def remove_spec(self, schedule_id: str) -> bool: ...

    def save_state(self, schedule_id: str, state: ScheduleState) -> None: ...

    def load_state(self, schedule_id: str) -> Optional[ScheduleState]: ...

    def append_run(self, schedule_id: str, run: RunRequest, finished_at: Optional[float] = None,
                   run_state: str = 'requested') -> None: ...

    def recent_runs(self, schedule_id: str, limit: int = 10) -> list[dict]: ...


class InMemoryScheduleStore:
    """Default store: survives a scheduler restart inside one process, writes nothing."""

    def __init__(self):
        self.specs: dict[str, ScheduleSpec] = {}
        self.states: dict[str, ScheduleState] = {}
        self.runs: dict[str, list[dict]] = {}

    def save_spec(self, spec: ScheduleSpec) -> None:
        self.specs[spec.id] = spec

    def load_specs(self) -> list[ScheduleSpec]:
        return [self.specs[key] for key in sorted(self.specs)]

    def remove_spec(self, schedule_id: str) -> bool:
        self.states.pop(schedule_id, None)
        self.runs.pop(schedule_id, None)
        return self.specs.pop(schedule_id, None) is not None

    def save_state(self, schedule_id: str, state: ScheduleState) -> None:
        self.states[schedule_id] = replace(state)

    def load_state(self, schedule_id: str) -> Optional[ScheduleState]:
        found = self.states.get(schedule_id)
        return replace(found) if found is not None else None

    def append_run(self, schedule_id: str, run: RunRequest, finished_at: Optional[float] = None,
                   run_state: str = 'requested') -> None:
        history = self.runs.setdefault(schedule_id, [])
        history.append({'id': run.run_id, 'schedule_id': schedule_id, 'started_at': run.scheduled_for,
                        'finished_at': finished_at, 'state': run_state, 'counters': run.to_dict()})
        del history[:-IN_MEMORY_RUN_HISTORY]

    def recent_runs(self, schedule_id: str, limit: int = 10) -> list[dict]:
        return list(reversed(self.runs.get(schedule_id, [])))[:limit]


#: Columns of migration 7 that this module needs but CONTRACTS 3.3 does not define yet.
#: `db.py` owns the DDL; the request itself lives in docs/integration/HANDOFF/scheduler.md.
REQUESTED_COLUMNS = {
    'schedules': ('last_run_at', 'paused', 'pause_reason', 'resume_at', 'dst_policy', 'catch_up',
                  'max_catch_up', 'wake_gap_s', 'power_json', 'notify_json', 'notifications_json',
                  'counters_json', 'updated_at'),
    'schedule_run': ('reason', 'missed'),
}


class SqliteScheduleStore:
    """Adapter over the tables `db.migrate()` creates (CONTRACTS 3.3, migration 7).

    It issues no DDL. Columns the contract does not define yet are used when
    present and ignored when absent, so the module works both before and after
    the migration gains them. `persists_runtime_state` and `persists_counters`
    say which case applies; until `counters_json` exists a restart restarts the
    period counters, which is stated in the handoff rather than hidden here.
    """

    def __init__(self, connection, table_prefix: str = ''):
        self.connection = connection
        self.prefix = table_prefix
        self.available = self._columns('schedules')
        self.run_columns = self._columns('schedule_run')
        self.persists_runtime_state = 'last_run_at' in self.available and 'paused' in self.available
        self.persists_counters = 'counters_json' in self.available

    def _table(self, name: str) -> str:
        return f'{self.prefix}{name}'

    def _columns(self, table: str) -> set[str]:
        try:
            rows = self.connection.execute(f'PRAGMA table_info({self._table(table)})').fetchall()
        except Exception:  # noqa: BLE001 - a missing table is a configuration state, not a crash
            return set()
        return {row[1] for row in rows}

    def save_spec(self, spec: ScheduleSpec) -> None:
        window_json = json.dumps({'windows': [item.to_dict() for item in spec.windows],
                                  'slots': [item.to_dict() for item in spec.slots]}, sort_keys=True)
        quiet_json = json.dumps([item.to_dict() for item in spec.quiet_hours], sort_keys=True)
        columns = ['id', 'pool_id', 'kind', 'interval_minutes', 'window_json', 'timezone',
                   'quiet_hours_json', 'budgets_json', 'next_run_at', 'enabled']
        values: list[object] = [spec.id, spec.pool_id, spec.kind, spec.interval_minutes, window_json,
                                spec.timezone, quiet_json, json.dumps(spec.budgets.to_dict(), sort_keys=True),
                                None, 1 if spec.enabled else 0]
        self._write_extra('schedules', columns, values, spec)

    def _write_extra(self, table: str, columns: list[str], values: list[object], spec: ScheduleSpec) -> None:
        name = self._table(table)
        if 'id' not in self.available:
            return
        if 'dst_policy' in self.available:
            columns.append('dst_policy')
            values.append(spec.dst_policy)
        if 'catch_up' in self.available:
            columns.append('catch_up')
            values.append(1 if spec.catch_up else 0)
        if 'max_catch_up' in self.available:
            columns.append('max_catch_up')
            values.append(spec.max_catch_up)
        if 'wake_gap_s' in self.available:
            columns.append('wake_gap_s')
            values.append(spec.wake_gap_s)
        if 'power_json' in self.available:
            columns.append('power_json')
            values.append(json.dumps(spec.power.to_dict(), sort_keys=True))
        if 'notify_json' in self.available:
            columns.append('notify_json')
            values.append(json.dumps(spec.notify.to_dict(), sort_keys=True))
        if 'notifications_json' in self.available:
            columns.append('notifications_json')
            values.append(json.dumps(spec.notifications.to_dict(), sort_keys=True))
        placeholders = ', '.join('?' for _ in columns)
        # `INSERT OR REPLACE` is a DELETE plus an INSERT in SQLite: every column
        # left out of the list comes back at its default.  `paused`,
        # `pause_reason`, `resume_at`, `counters_json` and `last_run_at` are not
        # in that list, so before this change a user pause and the spent daily
        # budget survived a restart and were then silently wiped by the next
        # `save_spec` -- the moment the user edited the interval.  An upsert
        # that only names the columns it writes keeps the rest.
        assignments = ', '.join(f'"{column}"=excluded."{column}"' for column in columns
                                if column != 'id')
        self.connection.execute(
            f'INSERT INTO {name} ({", ".join(columns)}) VALUES ({placeholders})'
            f' ON CONFLICT(id) DO UPDATE SET {assignments}', values)
        self.connection.commit()

    def load_specs(self) -> list[ScheduleSpec]:
        name = self._table('schedules')
        if not self._column_names('schedules'):
            return []
        rows = self.connection.execute(f'SELECT * FROM {name} ORDER BY id').fetchall()
        columns = self._column_names(name)
        return [self._spec_from_row(dict(zip(columns, row))) for row in rows]

    def _column_names(self, table: str) -> list[str]:
        rows = self.connection.execute(f'PRAGMA table_info({self._table(table)})').fetchall()
        return [row[1] for row in rows]

    def _spec_from_row(self, record: Mapping[str, object]) -> ScheduleSpec:
        window = json.loads(record.get('window_json') or '{}')
        data: dict = {
            'id': str(record['id']), 'kind': str(record.get('kind') or KIND_INTERVAL),
            'interval_minutes': record.get('interval_minutes'), 'timezone': str(record.get('timezone') or 'UTC'),
            'windows': window.get('windows') or [], 'slots': window.get('slots') or [],
            'quiet_hours': json.loads(record.get('quiet_hours_json') or '[]'),
            'budgets': json.loads(record.get('budgets_json') or '{}'),
            'enabled': bool(record.get('enabled', 1)),
            'pool_id': record.get('pool_id'),
        }
        for column in ('power_json', 'notify_json', 'notifications_json'):
            if record.get(column):
                data[column[:-5]] = json.loads(record[column])
        for column in ('dst_policy', 'max_catch_up', 'wake_gap_s'):
            if record.get(column) is not None:
                data[column] = record[column]
        if record.get('catch_up') is not None:
            data['catch_up'] = bool(record['catch_up'])
        return ScheduleSpec.from_dict(data)

    def remove_spec(self, schedule_id: str) -> bool:
        if 'id' not in self.available:
            return False
        cursor = self.connection.execute(f'DELETE FROM {self._table("schedules")} WHERE id = ?', (schedule_id,))
        self.connection.commit()
        return cursor.rowcount > 0

    def save_state(self, schedule_id: str, state: ScheduleState) -> None:
        name = self._table('schedules')
        columns: list[str] = []
        values: list[object] = []
        if 'last_run_at' in self.available:
            columns.append('last_run_at')
            values.append(state.last_run_at)
        if 'next_run_at' in self.available:
            columns.append('next_run_at')
            values.append(state.next_run_at)
        if 'paused' in self.available:
            columns.append('paused')
            values.append(1 if state.paused else 0)
        if 'pause_reason' in self.available:
            columns.append('pause_reason')
            values.append(state.pause_reason)
        if 'resume_at' in self.available:
            columns.append('resume_at')
            values.append(state.resume_at)
        if 'counters_json' in self.available:
            columns.append('counters_json')
            values.append(json.dumps(state.counters.to_dict(), sort_keys=True))
        if not columns or 'id' not in self.available:
            return
        self.connection.execute(
            f'UPDATE {name} SET {", ".join(f"{column} = ?" for column in columns)} WHERE id = ?',
            [*values, schedule_id])
        self.connection.commit()

    def load_state(self, schedule_id: str) -> Optional[ScheduleState]:
        name = self._table('schedules')
        wanted = ('last_run_at', 'next_run_at', 'paused', 'pause_reason', 'resume_at', 'counters_json')
        columns = [column for column in wanted if column in self.available]
        if not columns or 'id' not in self.available:
            return None
        row = self.connection.execute(
            f'SELECT {", ".join(columns)} FROM {name} WHERE id = ?', (schedule_id,)).fetchone()
        if row is None:
            return None
        record = dict(zip(columns, row))
        last_run_at = record.get('last_run_at')
        history = self.recent_runs(schedule_id, 1)
        if last_run_at is None and history:
            # The contract schema has no last_run_at column: the newest recorded run is the anchor,
            # which is what keeps the interval grid and the no-catch-up rule across a restart.
            last_run_at = max(item.get('started_at') or 0.0 for item in history)
        total = self.connection.execute(
            f'SELECT COUNT(*) FROM {self._table("schedule_run")} WHERE schedule_id = ?',
            (schedule_id,)).fetchone()[0]
        return ScheduleState(
            last_run_at=last_run_at, next_run_at=record.get('next_run_at'),
            paused=bool(record.get('paused') or False), pause_reason=record.get('pause_reason'),
            resume_at=record.get('resume_at'), runs=int(total),
            counters=BudgetCounters.from_dict(json.loads(record.get('counters_json') or '{}')))

    def append_run(self, schedule_id: str, run: RunRequest, finished_at: Optional[float] = None,
                   run_state: str = 'requested') -> None:
        name = self._table('schedule_run')
        if not {'id', 'schedule_id'} <= self.run_columns:
            return
        columns = ['id', 'schedule_id', 'started_at', 'finished_at', 'state', 'counters_json']
        values: list[object] = [run.run_id, schedule_id, run.scheduled_for, finished_at, run_state,
                                json.dumps(run.to_dict(), sort_keys=True)]
        if 'reason' in self.run_columns:
            columns.append('reason')
            values.append(run.reason)
        if 'missed' in self.run_columns:
            columns.append('missed')
            values.append(run.missed)
        placeholders = ', '.join('?' for _ in columns)
        self.connection.execute(
            f'INSERT OR REPLACE INTO {name} ({", ".join(columns)}) VALUES ({placeholders})', values)
        self.connection.commit()

    def recent_runs(self, schedule_id: str, limit: int = 10) -> list[dict]:
        name = self._table('schedule_run')
        if 'schedule_id' not in self.run_columns:
            return []
        rows = self.connection.execute(
            f'SELECT * FROM {name} WHERE schedule_id = ? ORDER BY started_at DESC, id DESC LIMIT ?',
            (schedule_id, limit)).fetchall()
        columns = self._column_names('schedule_run')
        return [dict(zip(columns, row)) for row in rows]


# ------------------------------------------------------------------ scheduler


class Scheduler:
    """Decides when a schedule may run and how much it may spend.

    The module never runs the work itself: a tick returns `RunRequest` objects
    for the job layer (F11) to execute. A tick is a pure function of the current
    time and the stored state, so the same instant always produces the same
    `run_id`.
    """

    def __init__(self, store: Optional[ScheduleStore] = None, clock: Callable[[], float] = time.time,
                 notifier: Optional[Notifier] = None, power_reader: Callable[[], PowerSignal] = read_system_signal):
        self.store = store or InMemoryScheduleStore()
        self.clock = clock
        self.notifier = notifier or Notifier()
        self.power_reader = power_reader
        self._state: dict[str, ScheduleState] = {}
        self._last_tick: Optional[float] = None
        self._woke_pending = False
        for spec in self.store.load_specs():
            stored = self.store.load_state(spec.id)
            if stored is not None:
                self._state[spec.id] = stored

    # -- registration -----------------------------------------------------

    def add(self, spec: ScheduleSpec | Mapping[str, object]) -> ScheduleSpec:
        """Validate, persist and activate a schedule. Invalid input raises ScheduleError."""
        spec = spec if isinstance(spec, ScheduleSpec) else ScheduleSpec.from_dict(spec)
        _tz(spec.timezone)
        _tz(spec.budgets.timezone)
        self.store.save_spec(spec)
        self._state.setdefault(spec.id, ScheduleState())
        return spec

    def get(self, schedule_id: str) -> Optional[ScheduleSpec]:
        for spec in self.store.load_specs():
            if spec.id == schedule_id:
                return spec
        return None

    def list(self) -> list[ScheduleSpec]:
        return self.store.load_specs()

    def remove(self, schedule_id: str) -> bool:
        self._state.pop(schedule_id, None)
        return self.store.remove_spec(schedule_id)

    def state(self, schedule_id: str) -> ScheduleState:
        found = self._state.get(schedule_id)
        if found is None:
            found = ScheduleState()
            self._state[schedule_id] = found
        return found

    def ledger(self, schedule_id: str, spec: Optional[ScheduleSpec] = None,
               now: Optional[float] = None) -> BudgetLedger:
        """The budget view of a schedule, with the stages the current power policy allows."""
        spec = spec or self.get(schedule_id)
        if spec is None:
            raise ScheduleError('E_VALIDATION_FIELD', 'schedule_id',
                                f'расписания {schedule_id!r} нет', f'no schedule {schedule_id!r}')
        now = self.clock() if now is None else now
        decision = power_decision(spec.power, self.power_reader())
        state = self.state(schedule_id)
        return BudgetLedger(spec.budgets, state.counters, now, decision.stages)

    # -- manual control ---------------------------------------------------

    def run_now(self, schedule_id: str, at: Optional[float] = None) -> Optional[RunRequest]:
        """Ask for a run outside the calendar, for example the "check now" action.

        A disabled schedule, a paused one, quiet hours, a closed window and a
        power restriction all still refuse: a manual run is not a way around the
        policy the user configured. The run counts as a run, so the interval grid
        restarts from now instead of firing twice.
        """
        now = self.clock() if at is None else at
        spec = self.get(schedule_id)
        if spec is None:
            raise ScheduleError('E_VALIDATION_FIELD', 'schedule_id',
                                f'расписания {schedule_id!r} нет', f'no schedule {schedule_id!r}')
        state = self.state(schedule_id)
        if self._policy_block(spec, state, now) is not None:
            return None
        power = power_decision(spec.power, self.power_reader())
        run = RunRequest(schedule_id=spec.id, run_id=make_run_id(spec.id, now), reason=REASON_MANUAL,
                         scheduled_for=now, requested_at=now, missed=0, coalesced=False,
                         pool_id=spec.pool_id, stages=power.stages, budgets=spec.budgets)
        state.last_run_at = now
        step = spec.interval_s or 0.0
        state.next_run_at = now + step if step else None
        state.runs += 1
        self.store.save_state(schedule_id, state)
        self.store.append_run(schedule_id, run)
        return run

    def blocked_by(self, schedule_id: str, at: Optional[float] = None) -> Optional[Decision]:
        """Why this schedule cannot run right now, or None when nothing blocks it."""
        now = self.clock() if at is None else at
        spec = self.get(schedule_id)
        if spec is None:
            raise ScheduleError('E_VALIDATION_FIELD', 'schedule_id',
                                f'расписания {schedule_id!r} нет', f'no schedule {schedule_id!r}')
        return self._policy_block(spec, self.state(schedule_id), now)

    def _policy_block(self, spec: ScheduleSpec, state: ScheduleState, now: float) -> Optional[Decision]:
        """The first reason this schedule may not run, or None when nothing blocks it."""
        tz = _tz(spec.timezone)
        if not spec.enabled:
            return Decision(spec.id, 'skip', REASON_DISABLED, now)
        if state.paused:
            return Decision(spec.id, 'skip', state.pause_reason or REASON_PAUSED_USER, now,
                            resume_at=state.resume_at)
        until = quiet_until(spec.quiet_hours, datetime.fromtimestamp(now, tz), tz)
        if until is not None:
            return Decision(spec.id, 'skip', REASON_PAUSED_QUIET, now, resume_at=until.timestamp())
        if not in_windows(spec.windows, datetime.fromtimestamp(now, tz), tz):
            opened = windows_next_open(spec.windows, datetime.fromtimestamp(now, tz), tz)
            return Decision(spec.id, 'skip', REASON_WINDOW_CLOSED, now,
                            resume_at=opened.timestamp() if opened else None)
        power = power_decision(spec.power, self.power_reader())
        if power.restricted:
            reason = REASON_PAUSED_BATTERY if power.reason == POWER_BATTERY else REASON_PAUSED_METERED
            return Decision(spec.id, 'skip', reason, now, detail=(('power_reason', power.reason),))
        return None

    def pause(self, schedule_id: str, reason: str = REASON_PAUSED_USER,
              at: Optional[float] = None) -> Decision:
        """Stop the schedule until it is resumed. Nothing is queued while it is paused."""
        at = self.clock() if at is None else at
        state = self.state(schedule_id)
        state.paused = True
        state.pause_reason = reason
        state.resume_at = None
        self.store.save_state(schedule_id, state)
        notification = Notification(subject=schedule_id, code=NOTIFY_PAUSED, at=at, severity=SEVERITY_WARNING,
                                    data=(('reason', reason),))
        self.notifier.emit(notification, self._policy(schedule_id))
        return Decision(schedule_id, 'skip', reason, at, next_run_at=state.next_run_at)

    def resume(self, schedule_id: str, at: Optional[float] = None) -> Decision:
        """Resume a paused schedule. Overdue occurrences are coalesced, never replayed."""
        at = self.clock() if at is None else at
        state = self.state(schedule_id)
        was_paused = state.paused
        state.paused = False
        state.pause_reason = None
        state.resume_at = None
        if was_paused:
            self.store.save_state(schedule_id, state)
            notification = Notification(subject=schedule_id, code=NOTIFY_RESUMED, at=at,
                                        data=(('reason', state.pause_reason or 'auto'),))
            self.notifier.emit(notification, self._policy(schedule_id))
        return Decision(schedule_id, 'resume', REASON_DUE if was_paused else REASON_NOT_DUE, at,
                        next_run_at=state.next_run_at)

    def mark_wake(self, at: Optional[float] = None) -> float:
        """Tell the scheduler the machine just woke up. The next tick coalesces what is overdue.

        The platform layer (F22) calls this when it sees a resume, so a wake is
        not guessed from a tick gap alone.
        """
        self._woke_pending = True
        return self.clock() if at is None else at

    # -- planning ---------------------------------------------------------

    def plan(self, spec: ScheduleSpec, state: ScheduleState, now: float) -> 'Plan':
        """Which planned instants are due at `now`, how many were missed, and why not.

        Windows are a filter on any plan: a candidate outside every window is
        skipped, and the plan reports when the next one inside the window is.
        """
        tz = _tz(spec.timezone)
        base = state.last_run_at if state.last_run_at is not None else state.activated_at
        if base is None:
            return Plan(REASON_NOT_DUE, next_at=None)
        keep = spec.max_catch_up
        if spec.kind == KIND_SLOTS:
            moments, overdue, dst_skipped, truncated = self._slot_candidates(spec, tz, base, now, keep)
        else:
            moments, overdue, dst_skipped = self._interval_candidates(spec, tz, base, now, keep)
            truncated = False
        inside = [item for item in moments if in_windows(spec.windows, datetime.fromtimestamp(item, tz), tz)]
        if inside:
            stale = bool(spec.catch_up_grace_s and now - inside[-1] > spec.catch_up_grace_s)
            return Plan(REASON_DUE if not stale else REASON_COALESCED, tuple(inside), overdue,
                        next_at=self._next_inside(spec, tz, state, inside[-1], now), dst_skipped=dst_skipped,
                        truncated=truncated)
        after = moments[-1] if moments else base
        following = self._next_inside(spec, tz, state, after, now)
        if following is None:
            return Plan(REASON_NO_SLOT, dst_skipped=dst_skipped, truncated=truncated)
        if following > now:
            reason = REASON_WINDOW_CLOSED if not in_windows(spec.windows, datetime.fromtimestamp(now, tz), tz) \
                else REASON_NOT_DUE
            return Plan(reason, next_at=following, resume_at=following, overdue=overdue,
                        dst_skipped=dst_skipped, truncated=truncated)
        return Plan(REASON_DUE, (following,), 0, next_at=following, dst_skipped=dst_skipped)

    def _interval_candidates(self, spec: ScheduleSpec, tz: ZoneInfo, base: float, now: float,
                             keep: int) -> tuple[list[float], int, int]:
        """Occurrences strictly after the anchor. Activation is the origin, not a run."""
        step = spec.interval_s or 0.0
        if step <= 0 or now <= base:
            return [], 0, 0
        count = int((now - base) // step)
        first = max(1, count - keep + 1)
        moments = [base + step * index for index in range(first, count + 1)]
        return moments, count, 0

    def _slot_candidates(self, spec: ScheduleSpec, tz: ZoneInfo, base: float, now: float,
                         keep: int) -> tuple[list[float], int, int, bool]:
        """Slots due since the last run. The enumeration is bounded, not open ended."""
        first_day = datetime.fromtimestamp(base, tz).date() - timedelta(days=1)
        last_day = datetime.fromtimestamp(now, tz).date()
        if (last_day - first_day).days > MAX_LOOKBACK_DAYS:
            first_day = last_day - timedelta(days=MAX_LOOKBACK_DAYS)
            truncated = True
        else:
            truncated = False
        moments: list[float] = []
        skipped = 0
        day = first_day
        while day <= last_day:
            allowed = False
            for slot in spec.slots:
                if WEEKDAY_NAMES[day.weekday()] not in slot.weekdays:
                    continue
                allowed = True
                moment = slot.instant_on(tz, day, spec.dst_policy)
                if moment is None:
                    skipped += 1
                    continue
                stamp = moment.timestamp()
                if base < stamp <= now:
                    moments.append(stamp)
            day += timedelta(days=1)
        moments.sort()
        return moments[-keep:], len(moments), skipped, truncated

    def _next_inside(self, spec: ScheduleSpec, tz: ZoneInfo, state: ScheduleState, after: float,
                     now: float) -> Optional[float]:
        """First planned instant after `after` that falls inside the windows. None if there is none."""
        base = state.last_run_at if state.last_run_at is not None else state.activated_at
        if base is None:
            return None
        if spec.kind == KIND_SLOTS:
            moment, _ = next_slots(tz, spec.slots, datetime.fromtimestamp(after, tz), spec.dst_policy)
            return moment.timestamp() if moment else None
        step = spec.interval_s or 0.0
        if step <= 0:
            return None
        candidate = base + (int((after - base) // step) + 1) * step
        for _ in range(MAX_WINDOW_JUMPS):
            if in_windows(spec.windows, datetime.fromtimestamp(candidate, tz), tz):
                return candidate
            opened = windows_next_open(spec.windows, datetime.fromtimestamp(candidate, tz), tz)
            if opened is None:
                return None
            stamp = opened.timestamp()
            index = -(-(stamp - base) // step)  # ceil: first grid point at or after the opening
            candidate = base + max(index, int((candidate - base) // step) + 1) * step
        return None

    # -- ticking ----------------------------------------------------------

    def tick(self, at: Optional[float] = None) -> TickReport:
        """Decide what may run now. No job is started here."""
        now = self.clock() if at is None else at
        if not isinstance(now, (int, float)) or isinstance(now, bool) or now != now:
            raise ScheduleError('E_TIME_UNKNOWN', 'at', f'время {now!r} непригодно', f'time {now!r} is not usable')
        signal = self.power_reader()
        specs = self.store.load_specs()
        woke = self._woke_pending or (self._last_tick is not None
                                      and (now - self._last_tick) >= self._wake_gap(specs))
        self._woke_pending = False
        decisions: list[Decision] = []
        requests: list[RunRequest] = []
        for spec in specs:
            decision, runs = self._tick_one(spec, now, signal=signal, woke=woke)
            decisions.append(decision)
            requests.extend(runs)
        self._last_tick = now
        return TickReport(at=now, decisions=tuple(decisions), run_requests=tuple(requests), woke=woke)

    def _wake_gap(self, specs: Sequence[ScheduleSpec]) -> float:
        gaps = [spec.effective_wake_gap_s for spec in specs]
        return min(gaps) if gaps else float('inf')

    def _policy(self, schedule_id: str) -> NotifyPolicy:
        spec = self.get(schedule_id)
        return spec.notify if spec else NotifyPolicy()

    def _tick_one(self, spec: ScheduleSpec, now: float, *, signal: PowerSignal,
                  woke: bool) -> tuple[Decision, list[RunRequest]]:
        state = self.state(spec.id)
        tz = _tz(spec.timezone)
        if state.activated_at is None:
            state.activated_at = now

        if not spec.enabled:
            return Decision(spec.id, 'skip', REASON_DISABLED, now, next_run_at=state.next_run_at), []

        # A pause that expired on its own (quiet hours, budget reset) is lifted here.
        if state.paused and state.resume_at is not None and now >= state.resume_at:
            state.paused = False
            state.pause_reason = None
            state.resume_at = None
            self.store.save_state(spec.id, state)
        if state.paused:
            reason = state.pause_reason or REASON_PAUSED_USER
            return Decision(spec.id, 'skip', reason, now, next_run_at=state.next_run_at,
                            resume_at=state.resume_at), []

        until = quiet_until(spec.quiet_hours, datetime.fromtimestamp(now, tz), tz)
        if until is not None:
            state.paused, state.pause_reason, state.resume_at = True, REASON_PAUSED_QUIET, until.timestamp()
            self.store.save_state(spec.id, state)
            self.notifier.emit(Notification(subject=spec.id, code=NOTIFY_QUIET, at=now,
                                            data=(('resume_at', state.resume_at),)), spec.notify)
            return Decision(spec.id, 'skip', REASON_PAUSED_QUIET, now, next_run_at=state.next_run_at,
                            resume_at=state.resume_at), []

        if not in_windows(spec.windows, datetime.fromtimestamp(now, tz), tz):
            opened = windows_next_open(spec.windows, datetime.fromtimestamp(now, tz), tz)
            resume_at = opened.timestamp() if opened else None
            self.notifier.emit(Notification(subject=spec.id, code=NOTIFY_WINDOW, at=now,
                                            data=(('resume_at', resume_at),)), spec.notify)
            return Decision(spec.id, 'skip', REASON_WINDOW_CLOSED, now, next_run_at=state.next_run_at,
                            resume_at=resume_at), []

        power = power_decision(spec.power, signal)
        ledger = BudgetLedger(spec.budgets, state.counters, now, power.stages)
        if power.restricted:
            reason = REASON_PAUSED_BATTERY if power.reason == POWER_BATTERY else REASON_PAUSED_METERED
            state.paused, state.pause_reason, state.resume_at = True, reason, None
            self.store.save_state(spec.id, state)
            self.notifier.emit(Notification(subject=spec.id, code=NOTIFY_POWER, at=now,
                                            data=(('reason', power.reason),)), spec.notify)
            return Decision(spec.id, 'skip', reason, now, next_run_at=state.next_run_at,
                            detail=(('power_reason', power.reason),)), []

        plan = self.plan(spec, state, now)
        if not plan.moments:
            if plan.dst_skipped and plan.reason == REASON_NO_SLOT:
                self.notifier.emit(Notification(subject=spec.id, code=NOTIFY_SKIPPED, at=now,
                                                data=(('reason', REASON_DST_SKIPPED),
                                                      ('slots', plan.dst_skipped))), spec.notify)
            return Decision(spec.id, 'skip', plan.reason, now, next_run_at=plan.next_at,
                            resume_at=plan.resume_at, detail=(('dst_skipped', plan.dst_skipped),)), []

        reason, moments, coalesced, missed = self._decide_dispatch(spec, plan, woke)
        if not moments:
            return Decision(spec.id, 'skip', reason, now, next_run_at=plan.next_at, missed=missed), []

        # A merged run is requested for the instant we decided to run it, not for the
        # occurrence it stands in for; `missed` says how many were folded into it.
        stamps = [now] * len(moments) if coalesced else list(moments)
        requests = []
        for stamp in stamps:
            requests.append(RunRequest(schedule_id=spec.id, run_id=make_run_id(spec.id, stamp),
                                       reason=reason, scheduled_for=stamp, requested_at=now,
                                       missed=missed, coalesced=coalesced, pool_id=spec.pool_id,
                                       stages=power.stages, budgets=spec.budgets))
            self.store.append_run(spec.id, requests[-1])
        step = spec.interval_s or 0.0
        state.last_run_at = stamps[-1]
        if coalesced and step:
            state.next_run_at = now + step
        else:
            state.next_run_at = plan.next_at if plan.next_at and plan.next_at > now else (now + step if step else None)
        state.runs += len(requests)
        state.skipped += max(0, plan.overdue - len(requests))
        state.counters = ledger.counters
        self.store.save_state(spec.id, state)
        decision = Decision(spec.id, 'run', reason, now, next_run_at=state.next_run_at, missed=missed,
                            detail=(('coalesced', coalesced), ('planned', len(moments)),
                                    ('stages', ','.join(power.stages)), ('power_reason', power.reason),
                                    ('truncated', plan.truncated)))
        return decision, requests

    def _decide_dispatch(self, spec: ScheduleSpec, plan: 'Plan', woke: bool) -> tuple[str, list[float], bool, int]:
        """How many of the due occurrences run now, and what becomes of the rest.

        Sleep, an expired catch-up grace and a schedule without catch-up all merge
        the overdue occurrences into one run; with catch-up at most
        `max_catch_up` of them are replayed and the rest are reported as merged.
        """
        if len(plan.moments) == 1 and plan.overdue == 1 and not woke:
            return REASON_DUE, list(plan.moments), False, 0
        if woke:
            return REASON_COALESCED_SLEEP, [plan.moments[-1]], True, plan.overdue - 1
        if plan.reason == REASON_COALESCED:
            return REASON_COALESCED, [plan.moments[-1]], True, plan.overdue - 1
        if not spec.catch_up:
            return REASON_COALESCED, [plan.moments[-1]], True, plan.overdue - 1
        return REASON_DUE, list(plan.moments), False, plan.overdue - len(plan.moments)

    # -- reporting --------------------------------------------------------

    def report(self, at: Optional[float] = None) -> dict:
        """Per-schedule status for the UI: next run, why it is stopped, budget, signals."""
        now = self.clock() if at is None else at
        signal = self.power_reader()
        items = []
        for spec in self.store.load_specs():
            state = self.state(spec.id)
            ledger = BudgetLedger(spec.budgets, state.counters, now)
            tz = _tz(spec.timezone)
            items.append({
                'id': spec.id, 'kind': spec.kind, 'enabled': spec.enabled, 'paused': state.paused,
                'pause_reason': state.pause_reason, 'resume_at': state.resume_at,
                'next_run_at': state.next_run_at, 'last_run_at': state.last_run_at,
                'runs': state.runs, 'skipped': state.skipped,
                'in_window': in_windows(spec.windows, datetime.fromtimestamp(now, tz), tz),
                'quiet': quiet_until(spec.quiet_hours, datetime.fromtimestamp(now, tz), tz) is not None,
                'power': power_decision(spec.power, signal).to_dict(),
                'budget': ledger.snapshot(now).to_dict(),
            })
        return {'at': now, 'power': signal.to_dict(), 'schedules': items,
                'notifications': [item.to_dict() for item in self.notifier.sent],
                'suppressed': self.notifier.suppressed_count(),
                'persistence': self.persistence()}

    def persistence(self) -> dict:
        """What survives a restart on the store this scheduler is talking to.

        A budget that silently resets is worse than no budget: the user reads the
        counter, trusts it, and a restart hands the whole limit back.  So the two
        runtime facts — the pause and the period counters — are reported as *lost*
        rather than quietly defaulted, and the caller can say so instead of
        claiming a limit that is not being enforced across a restart.  The module
        writes no DDL, so the fix belongs to the migrator that owns `schedules`
        (HANDOFF/scheduler.md §1.1; the columns are in :data:`REQUESTED_COLUMNS`).
        """
        runtime = bool(getattr(self.store, 'persists_runtime_state', False))
        counters = bool(getattr(self.store, 'persists_counters', False))
        lost = [name for name, kept in (('paused', runtime), ('counters', counters)) if not kept]
        return {'persists_runtime_state': runtime, 'persists_counters': counters,
                'lost_on_restart': lost,
                'requested_columns': {name: list(columns) for name, columns in REQUESTED_COLUMNS.items()}}


def make_run_id(schedule_id: str, scheduled_for: float) -> str:
    """Deterministic id: the same plan for the same instant yields the same id."""
    digest = hashlib.sha256(f'{schedule_id}|{scheduled_for:.3f}'.encode('utf-8')).hexdigest()
    return f'{schedule_id}-{digest[:16]}'
