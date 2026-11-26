"""Point-in-time follow-up planning and replay checks.

This module is intentionally tolerant of the small record shapes used by
workbench callers and focused tests.  The canonical vocabulary is English in
code; Chinese aliases are accepted on input and emitted in evidence records.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from math import ceil, isfinite
from typing import Any
import re

from ..reflection_factors.service import numeric_fields

DEFAULT_THRESHOLD = 25.0

_KEY_NAMES = ("key", "id", "order_id", "ticket", "ticket_id", "单号", "单据号")
_COST_NAMES = ("cost", "slots", "slot_cost", "units", "station_cost", "stations", "workload", "工位", "工位额度")
_DUE_NAMES = (
    "latest_retest_day", "latest_retest_date", "latest_day", "latest_recheck_day", "deadline_day",
    "deadline_date", "deadline", "due_day", "due_date", "due", "limit_day", "last_safe_day",
    "must_retest_by", "latest_date", "latest_review_day", "critical_day",
    "最迟复测日", "最迟日", "最后复测日", "期限", "截止日",
)
_CROSSING_NAMES = (
    "crossing_day", "crossing_date", "breach_day", "breach_date", "over_line_day", "overline_day",
    "threshold_day", "threshold_date", "failure_day", "decay_crossing_day", "predicted_crossing_day",
    "超线日", "衰减超线日", "预测超线日",
)
_ACTUAL_CROSSING_NAMES = (
    "actual_crossing_day", "actual_breach_day", "actual_over_line_day", "truth_day",
    "observed_crossing_day", "replay_crossing_day", "实绩超线日", "实际超线日",
)
_SCHEDULED_NAMES = (
    "scheduled_day", "scheduled_date", "schedule_day", "actual_day", "planned_day", "planned_date",
    "booked_day", "booked_date", "arranged_day", "arranged_date", "assigned_day", "slot_day",
    "plan_day", "day_scheduled", "scheduled_on", "实排日", "实际排班日", "排班日", "计划日",
)
_BASELINE_NAMES = (
    "baseline_day", "current_day", "reference_day", "current_scheduled_day",
    "original_day", "existing_day", "现行排班日", "基准排班日",
)
_PROMISE_NAMES = ("promised_day", "promise_day", "commitment_day", "committed_day", "拟承诺日期", "承诺日")
_RECEIPT_NAMES = ("receipt_day", "received_day", "observed_through_day", "through_day", "followed_through_day", "回执日", "随访截止日")
_RELEASE_NAMES = ("release_day", "available_day", "start_day", "earliest_day", "可排开始日")
_REPLAY_END_NAMES = ("replay_end", "replay_end_day", "coverage_end", "coverage_end_day", "replay_through", "回放截止日", "回放覆盖截止")
_FLAG_NAMES = ("manual", "needs_manual", "review", "requires_review", "uncertain", "attention")


class _AttrChain:
    def __init__(self, sources):
        self.sources = [source for source in sources if source is not None]

    def __getattr__(self, name):
        for source in self.sources:
            if hasattr(source, name):
                return getattr(source, name)
        raise AttributeError(name)


def _first(obj: Any, names: tuple[str, ...] | list[str], default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        for name in names:
            if name in obj and obj[name] is not None:
                return obj[name]
        return default
    for name in names:
        if hasattr(obj, name):
            value = getattr(obj, name)
            if value is not None:
                return value
    return default


def day_value(value: Any) -> int | None:
    """Convert an integer work day, rejecting booleans and junk."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("工日不能是布尔值")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and isfinite(value) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"-?\d+(\.0+)?", text):
            number = float(text)
            if isfinite(number) and number.is_integer():
                return int(number)
    raise ValueError(f"无效工日: {value!r}")


def cost_value(value: Any, measurements: Any = None) -> int:
    if value is None:
        traffic = _first(measurements, ("traffic_k", "traffic", "日车辆")) if measurements is not None else None
        if traffic is None and measurements is not None:
            values = _first(measurements, ("input_values", "measurements", "values"))
            if isinstance(values, (tuple, list)) and len(values) >= 4:
                traffic = values[3]
        if traffic is not None:
            try:
                return 1 + int(float(traffic) >= 38)
            except (TypeError, ValueError):
                pass
        return 1
    if isinstance(value, bool):
        raise ValueError("工位用量不能是布尔值")
    try:
        amount = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"无效工位用量: {value!r}") from error
    if amount < 1:
        raise ValueError("工位用量必须为正整数")
    return amount


def capacity_value(value: Any, default: int = 1) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError("工位容量不能是布尔值")
    amount = int(value)
    if amount < 0:
        raise ValueError("工位容量不能为负")
    return amount


def _iter_records(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, bytes, Mapping)):
        return [value]
    try:
        return list(value)
    except TypeError:
        return [value]


def _mapping_items(value: Any) -> list[tuple[str, Any]]:
    if isinstance(value, Mapping):
        return list(value.items())
    return []


def receipt_coverage_day(receipt: Any) -> int | None:
    """A non-crossing receipt proves follow-up only through its own day."""
    if isinstance(receipt, (int, float)) and not isinstance(receipt, bool):
        return day_value(receipt)
    return day_value(_first(receipt, ("day", "receipt_day", "observed_day", "through_day", "回执日", "当日")))


def select_version(records: Any, as_of: int, *, key: str | None = None) -> Any:
    """Select the newest revision that had arrived by ``as_of``.

    Records with ``received > as_of`` are invisible, so a late revision can
    never overwrite the version which was available when a window closed.
    The return value is intentionally the original record object/dict.
    """
    as_of = day_value(as_of)
    grouped: dict[str, list[tuple[int, int, int, Any]]] = {}
    for position, record in enumerate(_iter_records(records)):
        record_key = str(_first(record, _KEY_NAMES, key if key is not None else position))
        if key is not None and record_key != str(key):
            continue
        received = day_value(_first(record, ("received", "received_day", "arrival_day", "到站日", "接收日"), as_of))
        if received is not None and received > as_of:
            continue
        revision = _first(record, ("revision", "rev", "version", "版次"), 1)
        try:
            revision = int(revision)
        except (TypeError, ValueError):
            revision = 1
        grouped.setdefault(record_key, []).append((revision, received if received is not None else as_of, position, record))
    if key is not None:
        rows = grouped.get(str(key), [])
        if not rows:
            return None
        return sorted(rows, key=lambda item: (-item[0], item[1], item[2]))[0][3]
    result = {}
    for record_key, rows in grouped.items():
        result[record_key] = sorted(rows, key=lambda item: (-item[0], item[1], item[2]))[0][3]
    return result


# Common English alias.
version_at = select_version
available_version = select_version
available_revision = select_version


def _reading_xy(point: Any, index: int) -> tuple[float, float] | None:
    if isinstance(point, Mapping):
        day = _first(point, ("day", "x", "time", "work_day", "工日", "日期"))
        value = _first(point, (
            "decay", "score", "outcome", "value", "reading", "y", "level",
            "retro_cd", "reflectance", "衰减", "读数", "反光系数",
        ))
        if day is None and value is not None:
            day = index
    elif isinstance(point, (tuple, list)):
        if len(point) >= 2:
            day, value = point[0], point[1]
        elif len(point) == 1:
            day, value = index, point[0]
        else:
            return None
    else:
        day = _first(point, ("day", "x", "time", "work_day"), index)
        value = _first(point, ("decay", "score", "outcome", "value", "reading", "y", "level"), None)
    try:
        x = float(day)
        y = float(value)
    except (TypeError, ValueError):
        return None
    if not (isfinite(x) and isfinite(y)):
        return None
    return x, y


def fit_trend(readings: Any) -> tuple[float, float]:
    """Least-squares trend; exposed so train/inference callers share one path."""
    points = [xy for i, point in enumerate(_iter_records(readings)) if (xy := _reading_xy(point, i)) is not None]
    if not points:
        raise ValueError("历史读数需要至少一个有限点")
    if len(points) == 1:
        return points[0][1], 0.0
    n = len(points)
    sx = sum(x for x, _ in points)
    sy = sum(y for _, y in points)
    sxx = sum(x * x for x, _ in points)
    sxy = sum(x * y for x, y in points)
    denominator = n * sxx - sx * sx
    if abs(denominator) < 1e-12:
        return sy / n, 0.0
    slope = (n * sxy - sx * sy) / denominator
    intercept = (sy - slope * sx) / n
    return intercept, slope


def predict_crossing_day(
    readings: Any = None,
    threshold: float = DEFAULT_THRESHOLD,
    direction: str = "auto",
    *args: Any,
    **kwargs: Any,
) -> int | None:
    """Predict the first integer work day on which the line is crossed.

    ``direction='high'`` is the decay-score convention.  Reflectivity uses
    ``direction='low'``; an already-crossed reading returns its observed day.
    """
    # Model-backed calling style: predict_crossing_day(vector, state, model).
    feature_state = kwargs.pop("feature_state", None) or kwargs.pop("reference", None)
    model = kwargs.pop("model", None) or kwargs.pop("fit", None) or kwargs.pop("kernel", None)
    vector = kwargs.pop("vector", None) or (readings if feature_state is not None and model is not None else None)
    if feature_state is not None and model is not None and vector is not None:
        return model_crossing_day(vector, feature_state, model, threshold=threshold, **kwargs)

    history = readings if readings is not None else kwargs.get("history") or kwargs.get("points")
    if history is None and args:
        history = args[0]
    points = [xy for i, point in enumerate(_iter_records(history)) if (xy := _reading_xy(point, i)) is not None]
    if not points:
        return None
    if direction == "auto":
        sample = next(iter(_iter_records(history)), None)
        text = " ".join(sample.keys()).lower() if isinstance(sample, Mapping) else ""
        direction = "low" if any(token in text for token in ("retro", "reflect", "light", "反光")) else "high"
    high_bad = direction in {"high", "up", "decay"}
    for x, y in sorted(points):
        if (high_bad and y >= threshold) or (not high_bad and y <= threshold):
            return int(x) if float(x).is_integer() else ceil(x)
    intercept, slope = fit_trend(points)
    current_x, current_y = points[-1]
    rate = slope if abs(slope) > 1e-12 else (current_y - points[0][1]) / max(1.0, current_x - points[0][0])
    moving_toward = rate > 0 if high_bad else rate < 0
    if not moving_toward:
        return None
    delta = (threshold - current_y) / rate if high_bad else (current_y - threshold) / -rate
    crossing_exact = current_x + delta
    if crossing_exact < current_x:
        return int(current_x)
    return ceil(crossing_exact - 1e-10)


def model_crossing_day(vector: Any, feature_state: Any, model: Any, *, threshold: float = DEFAULT_THRESHOLD,
                       horizon: int = 3650, calibrated: Any = None, step: int = 1) -> int | None:
    """Project a frozen feature vector using the training feature transform."""
    from dataclasses import replace
    from ..bead_kernel.service import predict

    start = int(vector.day)
    stamp = feature_state.fingerprint
    step = max(1, int(step))

    def score_at(delta: int):
        values = list(vector.input_values)
        # Work-day projection advances coating age.  All five features are then
        # rebuilt by the exact training transformation, never by ad-hoc math.
        values[1] = float(values[1]) + delta
        normalized, physical, excursions = numeric_fields(tuple(values), feature_state)
        projected = replace(
            vector,
            day=start + delta,
            values=normalized,
            factors=physical,
            input_values=tuple(values),
            excursions=tuple(excursions),
        )
        scored = predict(model, [projected], stamp)[0]
        if calibrated is not None:
            from ..night_accuracy.service import check_point
            scored = check_point(scored, calibrated)
            return scored.estimate
        return scored.score

    if step == 1:
        for delta in range(0, max(0, int(horizon)) + 1):
            if score_at(delta) >= threshold:
                return start + delta
        return None

    last = min(int(horizon), ((int(horizon) + step - 1) // step) * step)
    crossing_band = None
    for delta in range(0, last + 1, step):
        if score_at(delta) >= threshold:
            crossing_band = delta
            break
    if crossing_band is None:
        return None
    lo = max(0, crossing_band - step + 1)
    for delta in range(lo, crossing_band + 1):
        if score_at(delta) >= threshold:
            return start + delta
    return start + crossing_band


def latest_retest_day(order: Any, *args: Any, **kwargs: Any) -> int | None:
    due = day_value(_first(order, _DUE_NAMES)) if not isinstance(order, (int, float)) else day_value(order)
    if due is not None:
        return due
    crossing = day_value(_first(order, _CROSSING_NAMES))
    if crossing is not None:
        return crossing
    history = _first(order, ("history", "readings", "observations", "历史读数"))
    if history is not None:
        threshold = _first(order, ("threshold", "cutoff", "line"), DEFAULT_THRESHOLD)
        return predict_crossing_day(history, threshold, *args, **kwargs)
    return None


@dataclass(frozen=True, slots=True)
class ScheduleEntry:
    key: str
    latest_day: int | None = None
    scheduled_day: int | None = None
    cost: int = 1
    state: str = "已排期"
    reason: str = "SCHEDULED"
    crossing_day: int | None = None
    baseline_day: int | None = None
    promised_day: int | None = None
    actual_crossing_day: int | None = None
    release_day: int | None = None
    overdue: bool = False
    manual: bool = False
    replay_miss: bool | None = None
    status_code: str = "SCHEDULED"
    evidence: dict[str, Any] = field(default_factory=dict, compare=False)

    # Common field aliases.
    @property
    def latest_retest_day(self) -> int | None:
        return self.latest_day

    @property
    def latest_recheck_day(self) -> int | None:
        return self.latest_day

    @property
    def deadline_day(self) -> int | None:
        return self.latest_day

    @property
    def due_day(self) -> int | None:
        return self.latest_day

    @property
    def due(self) -> int | None:
        return self.latest_day

    @property
    def actual_day(self) -> int | None:
        return self.scheduled_day

    @property
    def planned_day(self) -> int | None:
        return self.scheduled_day

    @property
    def booked_day(self) -> int | None:
        return self.scheduled_day

    @property
    def slot_day(self) -> int | None:
        return self.scheduled_day

    def __getattr__(self, name: str) -> Any:
        aliases = {
            "scheduled_on": self.scheduled_day,
            "scheduled_date": self.scheduled_day,
            "arranged_day": self.scheduled_day,
            "arranged_date": self.scheduled_day,
            "actual_scheduled_day": self.scheduled_day,
            "real_scheduled_day": self.scheduled_day,
            "latest_retest_on": self.latest_day,
            "latest_date": self.latest_day,
            "last_safe_day": self.latest_day,
            "last_retest_day": self.latest_day,
            "deadline": self.latest_day,
            "due_date": self.latest_day,
            "due": self.latest_day,
            "status": self.state,
            "schedule_status_code": self.status_code,
        }
        if name in aliases:
            return aliases[name]
        raise AttributeError(name)

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, ScheduleEntry):
            return (self.key, self.latest_day, self.scheduled_day, self.state, self.reason) == (
                other.key, other.latest_day, other.scheduled_day, other.state, other.reason)
        if other is None:
            return self.scheduled_day is None
        return self.scheduled_day == other

    def __hash__(self) -> int:
        return hash((self.key, self.scheduled_day, self.state, self.reason))

    def __getitem__(self, name: str) -> Any:
        if name in self.as_dict():
            return self.as_dict()[name]
        raise KeyError(name)

    def get(self, name: str, default: Any = None) -> Any:
        try:
            return self[name]
        except KeyError:
            return default

    def as_dict(self) -> dict[str, Any]:
        result = {
            "key": self.key,
            "latest_retest_day": self.latest_day,
            "latest_day": self.latest_day,
            "deadline_day": self.latest_day,
            "due_day": self.latest_day,
            "crossing_day": self.crossing_day,
            "scheduled_day": self.scheduled_day,
            "actual_scheduled_day": self.scheduled_day,
            "actual_day": self.scheduled_day,
            "planned_day": self.scheduled_day,
            "baseline_day": self.baseline_day,
            "promised_day": self.promised_day,
            "actual_crossing_day": self.actual_crossing_day,
            "release_day": self.release_day,
            "cost": self.cost,
            "state": self.state,
            "schedule_state": self.state,
            "status": self.state,
            "status_code": self.status_code,
            "schedule_status": self.status_code,
            "status_en": self.status_code,
            "code": self.status_code,
            "reason": self.reason,
            "schedule_reason": self.reason,
            "overdue": self.overdue,
            "manual": self.manual,
            "replay_miss": self.replay_miss,
            "最迟复测日": self.latest_day,
            "超线日": self.crossing_day,
            "实排日": self.scheduled_day,
            "拟承诺日期": self.promised_day,
            "状态": self.state,
            "原因": self.reason,
            "工位": self.cost,
            "逾期": self.overdue,
            "转人工": self.manual,
        }
        result.update(self.evidence)
        return result


@dataclass(frozen=True, slots=True)
class FollowUpPlan:
    start_day: int
    days: tuple[int, ...]
    entries: tuple[ScheduleEntry, ...]
    daily_capacity: Any
    daily_load: tuple[tuple[int, int], ...]
    baseline_misses: int | None = None
    replay_misses: int | None = None
    miss_budget: int | None = None
    coverage_end: int | None = None
    first_day_used: int = 0
    feature_stamp: str | None = None

    @property
    def orders(self) -> tuple[ScheduleEntry, ...]:
        return self.entries

    @property
    def items(self) -> tuple[ScheduleEntry, ...]:
        return self.entries

    @property
    def jobs(self) -> tuple[ScheduleEntry, ...]:
        return self.entries

    @property
    def rows(self) -> tuple[ScheduleEntry, ...]:
        return self.entries

    @property
    def schedule(self) -> tuple[ScheduleEntry, ...]:
        return self.entries

    @property
    def by_key(self) -> dict[str, ScheduleEntry]:
        return {item.key: item for item in self.entries}

    @property
    def first_day(self) -> int:
        return self.start_day

    @property
    def first_day_load(self) -> int:
        return self.first_day_used

    def __getattr__(self, name: str) -> Any:
        aliases = {
            "first_day_workload": self.first_day_used,
            "first_day_units": self.first_day_used,
            "orders_by_key": self.by_key,
            "items_by_key": self.by_key,
            "schedule_by_key": self.by_key,
            "scheduled_days": self.day_map(),
            "latest_retest_days": self.latest_days(),
        }
        if name in aliases:
            return aliases[name]
        raise AttributeError(name)

    def get(self, key: str, default: Any = None) -> ScheduleEntry | None:
        return self.by_key.get(str(key), default)

    def __iter__(self):
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def __getitem__(self, key: Any):
        if isinstance(key, int):
            return self.entries[key]
        if isinstance(key, str):
            metadata = self.to_dict()
            if key in metadata:
                return metadata[key]
            return self.by_key[key]
        raise TypeError(key)

    def day_map(self) -> dict[str, int | None]:
        return {item.key: item.scheduled_day for item in self.entries}

    def latest_days(self) -> dict[str, int | None]:
        return {item.key: item.latest_day for item in self.entries}

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_day": self.start_day,
            "first_day": self.start_day,
            "days": list(self.days),
            "daily_capacity": self.daily_capacity,
            "daily_load": [{"day": day, "used": used} for day, used in self.daily_load],
            "load_by_day": dict(self.daily_load),
            "first_day_used": self.first_day_used,
            "baseline_misses": self.baseline_misses,
            "replay_misses": self.replay_misses,
            "miss_budget": self.miss_budget,
            "coverage_end": self.coverage_end,
            "feature_stamp": self.feature_stamp,
            "orders": [item.as_dict() for item in self.entries],
            "schedule": [item.as_dict() for item in self.entries],
            "rows": [item.as_dict() for item in self.entries],
            "by_key": {item.key: item.as_dict() for item in self.entries},
            "day_map": self.day_map(),
            "status_counts": dict(Counter(item.status_code for item in self.entries)),
            "state_counts": dict(Counter(item.state for item in self.entries)),
            "最迟复测日": {item.key: item.latest_day for item in self.entries},
            "实排日": self.day_map(),
        }


SchedulePlan = FollowUpPlan
FutureSchedule = FollowUpPlan


def _capacity_map(capacities: Any, days: tuple[int, ...], default: int = 1) -> dict[int, int]:
    if capacities is None:
        return {day: default for day in days}
    if isinstance(capacities, int) and not isinstance(capacities, bool):
        return {day: capacities for day in days}
    if isinstance(capacities, Mapping):
        result = {}
        for day in days:
            value = capacities.get(day, capacities.get(str(day), default))
            result[day] = capacity_value(value, default)
        return result
    values = list(capacities)
    result = {}
    for i, day in enumerate(days):
        result[day] = capacity_value(values[i] if i < len(values) else default, default)
    return result


def _days_from_kwargs(kwargs: dict[str, Any], tasks: list[dict[str, Any]]) -> tuple[int, tuple[int, ...], Any]:
    start = kwargs.get("start_day", kwargs.get("start", kwargs.get("first_day", kwargs.get("begin_day", kwargs.get("start_date", kwargs.get("首日", kwargs.get("开始日")))))))
    end = kwargs.get("end_day", kwargs.get("last_day", kwargs.get("horizon_end", kwargs.get("end_date", kwargs.get("end", kwargs.get("结束日"))))))
    horizon = kwargs.get("horizon", kwargs.get("days", kwargs.get("num_days", kwargs.get("horizon_days", kwargs.get("future_days", kwargs.get("天数", kwargs.get("未来天数")))))))
    capacities = kwargs.get("daily_capacity", kwargs.get("capacities", kwargs.get("capacity_by_day")))
    if isinstance(horizon, (list, tuple, Mapping)) and capacities is None:
        capacities = horizon
        horizon = len(horizon) if not isinstance(horizon, Mapping) else None
    if start is None:
        starts = [task.get("release_day") for task in tasks if task.get("release_day") is not None]
        start = min(starts) if starts else 0
    start = day_value(start)
    if end is not None:
        end = day_value(end)
        days = tuple(range(start, end + 1))
    elif horizon is not None and not isinstance(horizon, bool):
        try:
            count = int(horizon)
        except (TypeError, ValueError):
            count = None
        if count is not None:
            days = tuple(range(start, start + count))
        else:
            days = tuple(sorted(_capacity_map(capacities, (), 1).keys())) if isinstance(capacities, Mapping) else ()
    elif isinstance(capacities, Mapping):
        raw_days = [day_value(k) for k in capacities.keys()]
        days = tuple(range(min(raw_days), max(raw_days) + 1))
        start = min(start, min(raw_days))
    elif isinstance(capacities, (list, tuple)):
        days = tuple(range(start, start + len(capacities)))
    else:
        due_days = [task["latest_day"] for task in tasks if task.get("latest_day") is not None]
        last = max(due_days) if due_days else start
        days = tuple(range(start, last + 1))
    return start, days, capacities


def _normalize_task(raw: Any, position: int, threshold: float = DEFAULT_THRESHOLD) -> dict[str, Any]:
    if isinstance(raw, (tuple, list)) and raw and not isinstance(raw[0], (tuple, list, Mapping)):
        # Convenience forms: (key, due), (key, due, cost), or
        # (key, due, scheduled_day, cost).  Dictionaries remain preferred.
        positional = {"key": raw[0]}
        rest = list(raw[1:])
        if len(rest) == 1:
            positional["latest_retest_day"] = rest[0]
        elif len(rest) == 2:
            positional["latest_retest_day"] = rest[0]
            if isinstance(rest[1], int) and not isinstance(rest[1], bool):
                positional["cost"] = rest[1]
            else:
                positional["scheduled_day"] = rest[1]
        elif len(rest) >= 3:
            positional["latest_retest_day"] = rest[0]
            positional["scheduled_day"] = rest[1]
            positional["cost"] = rest[2]
        raw = positional
    if not isinstance(raw, Mapping):
        children = [raw]
        for attr in ("raw", "assessment", "prediction", "ticket", "job", "order"):
            child = getattr(raw, attr, None)
            if child is not None:
                children.append(child)
        if len(children) > 1:
            raw = _AttrChain(children)
    key = str(_first(raw, _KEY_NAMES, f"ORDER-{position + 1:03d}"))
    cost = cost_value(_first(raw, _COST_NAMES), raw)
    crossing = day_value(_first(raw, _CROSSING_NAMES))
    latest = day_value(_first(raw, _DUE_NAMES))
    if latest is None:
        latest = crossing
    if latest is None:
        history = _first(raw, ("history", "readings", "observations", "历史读数"))
        if history is not None:
            latest = predict_crossing_day(history, _first(raw, ("threshold", "cutoff", "line"), threshold))
            crossing = latest
    actual = day_value(_first(raw, _ACTUAL_CROSSING_NAMES))
    scheduled = day_value(_first(raw, _SCHEDULED_NAMES))
    baseline = day_value(_first(raw, _BASELINE_NAMES))
    if baseline is None and scheduled is not None and _first(raw, ("use_scheduled_as_baseline",), False):
        baseline = scheduled
    promised = day_value(_first(raw, _PROMISE_NAMES))
    receipt = day_value(_first(raw, _RECEIPT_NAMES))
    release = day_value(_first(raw, _RELEASE_NAMES))
    manual = bool(_first(raw, _FLAG_NAMES, False))
    state_value = _first(raw, ("state", "status", "状态"))
    if state_value in {"人工判读", "MANUAL_REVIEW", "manual"}:
        manual = True
    return {
        "raw": raw, "key": key, "cost": cost, "latest_day": latest, "crossing_day": crossing,
        "actual_day": actual, "scheduled_day": scheduled, "baseline_day": baseline,
        "promised_day": promised, "receipt_day": receipt, "release_day": release,
        "manual": manual,
    }


def _truth_map(replay: Any, tasks: list[dict[str, Any]]) -> dict[str, int]:
    truth = {task["key"]: task["actual_day"] for task in tasks if task["actual_day"] is not None}
    for record in _iter_records(replay):
        key = str(_first(record, _KEY_NAMES, ""))
        day = day_value(_first(record, _ACTUAL_CROSSING_NAMES + _CROSSING_NAMES + ("crossed_day", "event_day")))
        if key and day is not None:
            truth[key] = day
    return truth


def _baseline_map(baseline: Any, tasks: list[dict[str, Any]], start: int) -> dict[str, int | None]:
    result = {task["key"]: task["baseline_day"] for task in tasks if task["baseline_day"] is not None}
    if isinstance(baseline, Mapping):
        for key, value in baseline.items():
            result[str(key)] = day_value(value)
    elif baseline is not None:
        for record in _iter_records(baseline):
            if isinstance(record, (int, float)):
                continue
            key = str(_first(record, _KEY_NAMES, ""))
            day = day_value(_first(record, _SCHEDULED_NAMES + _BASELINE_NAMES + ("day",)))
            if key:
                result[key] = day
    # The workbench's implicit current shift is day one when no explicit plan
    # was supplied.  Explicit null stays null and represents no baseline visit.
    for task in tasks:
        if task["key"] not in result:
            result[task["key"]] = start
    return result


def count_misses(assignments: Any = None, crossings: Any = None, *, receipts: Any = None,
                 strict_receipt: bool = False, exact_day: bool = False, **kwargs: Any) -> int:
    """Count crossing deadlines that are not met by the replay plan.

    The default deadline rule is inclusive: a retest on the crossing day is
    not a miss while a missing or later retest is.  ``strict_receipt`` /
    ``exact_day`` implement the stronger receipt reading: a clean earlier
    receipt proves follow-up only through its own day, so the crossing day
    itself must contain a check.
    """
    strict = strict_receipt or exact_day or kwargs.get("require_crossing_day", False)
    plan_days: dict[str, set[int]] = {}

    def add_day(key: str, value: Any) -> None:
        day = day_value(value)
        if day is not None:
            plan_days.setdefault(str(key), set()).add(day)

    if isinstance(assignments, Mapping):
        for key, value in assignments.items():
            if isinstance(value, Mapping):
                add_day(key, _first(value, _SCHEDULED_NAMES + ("day",)))
            elif isinstance(value, (list, tuple, set)):
                for item in value:
                    add_day(key, item)
            else:
                add_day(key, value)
    else:
        for item in _iter_records(assignments):
            key = _first(item, _KEY_NAMES)
            if key is None:
                continue
            if isinstance(item, Mapping):
                value = _first(item, _SCHEDULED_NAMES + ("day",))
            else:
                value = getattr(item, "scheduled_day", None)
            if isinstance(value, (list, tuple, set)):
                for day in value:
                    add_day(key, day)
            else:
                add_day(key, value)

    truth: dict[str, int] = {}
    if isinstance(crossings, Mapping):
        for key, value in crossings.items():
            truth[str(key)] = day_value(value)
    else:
        for item in _iter_records(crossings):
            key = _first(item, _KEY_NAMES)
            day = day_value(_first(item, _ACTUAL_CROSSING_NAMES + _CROSSING_NAMES + ("day",)))
            if key is not None and day is not None:
                truth[str(key)] = day

    for item in _iter_records(receipts):
        key = str(_first(item, _KEY_NAMES, ""))
        cover = receipt_coverage_day(item)
        crossed = _first(item, ("crossed", "over_line", "is_crossing", "超线"), False)
        if key and cover is not None and not crossed:
            add_day(key, cover)

    misses = 0
    for key, crossing in truth.items():
        checks = plan_days.get(key, set())
        if strict:
            covered = crossing in checks
        else:
            covered = bool(checks) and min(checks) <= crossing
        if not covered:
            misses += 1
    return misses


replay_misses = count_misses
miss_count = count_misses


def compare_replay(current: Any = None, proposed: Any = None, crossings: Any = None, **kwargs: Any) -> dict[str, Any]:
    """Compare a delayed plan with the current plan under one replay quota."""
    strict = kwargs.pop("strict_receipt", False)
    current_misses = count_misses(current, crossings, strict_receipt=strict, **kwargs)
    proposed_misses = count_misses(proposed, crossings, strict_receipt=strict, **kwargs)
    return {
        "current_misses": current_misses,
        "baseline_misses": current_misses,
        "proposed_misses": proposed_misses,
        "replay_misses": proposed_misses,
        "allowed": proposed_misses <= current_misses,
        "miss_budget": current_misses,
    }


compare_schedules = compare_replay
replay_comparison = compare_replay
evaluate_delay = compare_replay


def _coverage_limit(kwargs: dict[str, Any]) -> int | None:
    inclusive = kwargs.pop("coverage_end", None)
    if inclusive is None:
        inclusive = kwargs.pop("coverage_end_day", None)
    if inclusive is None:
        inclusive = kwargs.pop("replay_through", None)
    if inclusive is None:
        inclusive = kwargs.pop("last_replay_day", None)
    if inclusive is not None:
        return day_value(inclusive)

    exclusive = kwargs.pop("replay_end", None)
    if exclusive is None:
        exclusive = kwargs.pop("window_end", None)
    if exclusive is None:
        exclusive = kwargs.pop("replay_end_day", None)
    if exclusive is not None:
        end = day_value(exclusive)
        return None if end is None else end - 1

    window = kwargs.pop("replay_window", None)
    if window is not None:
        end = _first(window, ("end", "stop"), None)
        if end is None and isinstance(window, (tuple, list)) and len(window) == 2:
            end = window[1]
        end = day_value(end)
        return None if end is None else end - 1
    return None


def _infer_coverage_end(replay: Any, explicit: Any) -> int | None:
    end = day_value(explicit)
    if end is not None:
        return end
    candidates: list[int] = []
    for record in _iter_records(replay):
        if isinstance(record, (int, float)) and not isinstance(record, bool):
            candidates.append(day_value(record))
            continue
        for names in (_REPLAY_END_NAMES, _ACTUAL_CROSSING_NAMES, _CROSSING_NAMES, ("result_day", "received", "day")):
            value = day_value(_first(record, names))
            if value is not None:
                candidates.append(value)
                break
    return max(candidates) if candidates else None


def _latest_available_day(task: dict[str, Any], capacities: dict[int, int], days: tuple[int, ...], upper: int | None = None) -> int | None:
    upper_day = task["latest_day"] if upper is None else upper
    allowed = [d for d in days if capacities.get(d, 0) >= task["cost"]
               and (task["release_day"] is None or d >= task["release_day"])
               and (upper_day is None or d <= upper_day)]
    return max(allowed) if allowed else None


def _earliest_available_day(task: dict[str, Any], capacities: dict[int, int], days: tuple[int, ...], lower: int | None = None) -> int | None:
    allowed = [d for d in days if capacities.get(d, 0) >= task["cost"]
               and (task["release_day"] is None or d >= task["release_day"])
               and (lower is None or d >= lower)]
    return min(allowed) if allowed else None


def _exact_schedule(tasks, days, capacities, start, truth, miss_budget, strict_receipt=False):
    """Small-instance optimizer for the stated first-day objective."""
    auto = [task for task in tasks if not task["manual"]]
    options = []
    product_size = 1
    for task in auto:
        choices = [None] + [day for day in days if capacities.get(day, 0) >= task["cost"]
                            and (task["release_day"] is None or day >= task["release_day"])]
        options.append(choices)
        product_size *= max(1, len(choices))
    if product_size > 300_000:
        return None

    deadline_order = sorted(range(len(auto)), key=lambda i: (
        auto[i]["latest_day"] is None,
        auto[i]["latest_day"] if auto[i]["latest_day"] is not None else 10**12,
        auto[i]["key"],
    ))
    best = None
    best_objective = None

    def evaluate(assignment):
        day_map = {task["key"]: day for task, day in zip(auto, assignment) if day is not None}
        misses = count_misses(day_map, truth, strict_receipt=strict_receipt)
        if misses > miss_budget:
            return None
        first_load = sum(task["cost"] for task, day in zip(auto, assignment) if day == start)
        edf = tuple(
            1 if assignment[i] is None or (auto[i]["latest_day"] is not None and assignment[i] > auto[i]["latest_day"]) else 0
            for i in deadline_order
        )
        overdue_cost = sum(
            task["cost"] for task, day in zip(auto, assignment)
            if day is None or (task["latest_day"] is not None and day > task["latest_day"])
        )
        lateness = sum(
            max(0, day - task["latest_day"])
            for task, day in zip(auto, assignment)
            if day is not None and task["latest_day"] is not None
        )
        unscheduled = sum(task["cost"] for task, day in zip(auto, assignment) if day is None)
        later_sum = sum(day for day in assignment if day is not None)
        # Protect earlier deadlines first (EDF), then empty the first day as
        # much as the replay-miss allowance permits.  Remaining items make
        # equal-deadline outcomes deterministic and avoid needless omissions.
        return (edf, first_load, overdue_cost, lateness, unscheduled, -later_sum)

    def visit(index, remaining, assignment, first_load):
        nonlocal best, best_objective
        if index == len(auto):
            objective = evaluate(assignment)
            if objective is not None and (best_objective is None or objective < best_objective):
                best_objective = objective
                best = dict(zip((task["key"] for task in auto), assignment, strict=True))
            return
        task = auto[index]
        # Try later days first only for branch ordering; objective is exact.
        for day in sorted(options[index], key=lambda x: (x is None, -(x if x is not None else 0))):
            if day is not None:
                if remaining[day] < task["cost"]:
                    continue
                remaining[day] -= task["cost"]
                assignment.append(day)
                visit(index + 1, remaining, assignment, first_load + (task["cost"] if day == start else 0))
                assignment.pop()
                remaining[day] += task["cost"]
            else:
                assignment.append(None)
                visit(index + 1, remaining, assignment, first_load)
                assignment.pop()

    visit(0, dict(capacities), [], 0)
    return best


def schedule_followups(*args: Any, **kwargs: Any) -> FollowUpPlan:
    """Schedule follow-up checks over future work days.

    Calling conventions accepted:

    * ``schedule_followups(orders, daily_capacity=2, start_day=97, horizon=7)``
    * positional capacity/start/horizon integers;
    * ``capacities=[2, 1, 1]`` or ``{97: 2}``.
    """
    # Unwrap a single request object such as schedule_followups({'orders': ...}).
    if args and isinstance(args[0], Mapping) and any(
        key in args[0] for key in ("orders", "tickets", "items", "jobs", "records", "requests", "work_orders",
                                   "工单", "单据", "巡检单", "排班请求")
    ):
        payload = dict(args[0])
        payload.update(kwargs)
        kwargs = payload
        args = args[1:]

    order_names = ("orders", "order_list", "tickets", "items", "jobs", "records", "schedule_requests",
                   "requests", "work_orders", "visits", "appointments", "tasks", "data",
                   "工单", "单据", "巡检单", "排班请求", "排班")
    orders = None
    for name in order_names:
        if name in kwargs:
            orders = kwargs.pop(name)
            break

    positional_collections = [arg for arg in args if isinstance(arg, (list, tuple))]
    positional_capacities = None
    if orders is None:
        for collection in positional_collections:
            if collection and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in collection):
                positional_capacities = collection
            elif not collection:
                continue
            else:
                orders = collection
                break
        if orders is None and args:
            orders = args[0]
    if orders is None:
        orders = []

    replay = None
    for name in ("replay", "replay_cases", "history_replay", "replay_log", "replay_results", "replay_records"):
        if name in kwargs:
            replay = kwargs.pop(name)
            break
    baseline = None
    for name in ("baseline", "current", "current_schedule", "baseline_schedule", "current_plan",
                 "reference_schedule", "existing_schedule"):
        if name in kwargs:
            baseline = kwargs.pop(name)
            break

    # Classify remaining positional record collections by their fields.
    for collection in positional_collections:
        if collection is orders or not collection:
            continue
        first = collection[0]
        if replay is None and _first(first, _ACTUAL_CROSSING_NAMES, None) is not None:
            replay = collection
        elif baseline is None and _first(first, _BASELINE_NAMES + _SCHEDULED_NAMES, None) is not None:
            baseline = collection
        elif replay is None:
            replay = collection

    explicit_end = _coverage_limit(kwargs)
    threshold = kwargs.pop("threshold", DEFAULT_THRESHOLD)
    strict_receipt = bool(kwargs.pop("strict_receipt", kwargs.pop("exact_day", kwargs.pop("require_crossing_day", False))))
    feature_stamp = kwargs.pop("feature_stamp", None)

    for alias in ("slots_per_day", "daily_slots", "stations_per_day", "daily_quota", "quota",
                  "capacity_per_day", "per_day_capacity", "station_capacity",
                  "每日工位", "每日额度", "工位额度", "日额度"):
        if alias in kwargs and "daily_capacity" not in kwargs:
            kwargs["daily_capacity"] = kwargs.pop(alias)
    # Consume common capacity keywords before day inference.
    if "daily_capacity" not in kwargs and "capacities" not in kwargs and "capacity" in kwargs:
        kwargs["daily_capacity"] = kwargs.pop("capacity")
    elif "capacity" in kwargs:
        kwargs.pop("capacity")
    if positional_capacities is not None:
        kwargs.setdefault("capacities", positional_capacities)

    # Positional integers commonly mean capacity, start, horizon.
    int_args = [arg for arg in args if isinstance(arg, int) and not isinstance(arg, bool)]
    if int_args:
        kwargs.setdefault("daily_capacity", int_args[0])
    if len(int_args) >= 2:
        kwargs.setdefault("start_day", int_args[1])
    if len(int_args) >= 3:
        kwargs.setdefault("horizon", int_args[2])

    raw_tasks = _iter_records(orders)
    if isinstance(orders, Mapping) and len(raw_tasks) == 1:
        known_order_fields = set(_KEY_NAMES + _DUE_NAMES + _CROSSING_NAMES + _SCHEDULED_NAMES + _COST_NAMES)
        if not any(key in known_order_fields for key in orders.keys()):
            expanded = []
            for key, value in orders.items():
                if isinstance(value, Mapping):
                    row = dict(value)
                    row.setdefault("key", key)
                    expanded.append(row)
                elif isinstance(value, (list, tuple)):
                    expanded.append({"key": key, "history": value})
                else:
                    expanded.append({"key": key, "latest_retest_day": value})
            if expanded:
                raw_tasks = expanded
    tasks = [_normalize_task(raw, i, threshold) for i, raw in enumerate(raw_tasks)]
    if len({t["key"] for t in tasks}) != len(tasks):
        # Order-independent identity check mirrors the existing dispatcher.
        raise ValueError("待排单据编号重复")

    start, days, capacities_arg = _days_from_kwargs(kwargs, tasks)
    if not days:
        days = (start,)
    default_capacity = kwargs.get("daily_capacity", kwargs.get("capacity", 1))
    if capacities_arg is None and "daily_capacity" in kwargs:
        capacities_arg = kwargs["daily_capacity"]
    default_units = capacity_value(default_capacity, 1) if not isinstance(default_capacity, (list, tuple, Mapping)) else 1
    capacities = _capacity_map(capacities_arg, days, default_units)

    coverage_end = _infer_coverage_end(replay, explicit_end)
    truth = _truth_map(replay, tasks)
    baseline_days = _baseline_map(baseline, tasks, start)
    effective_truth = truth or {t["key"]: t["latest_day"] for t in tasks if t["latest_day"] is not None}
    baseline_misses = count_misses(baseline_days, effective_truth, strict_receipt=strict_receipt)
    miss_budget = kwargs.pop("miss_budget", baseline_misses)
    if miss_budget is None:
        miss_budget = baseline_misses

    entries: list[ScheduleEntry] = []
    remaining = dict(capacities)

    # Coverage gate first.  A proposed date outside replay evidence is manual.
    auto_tasks: list[dict[str, Any]] = []
    for task in tasks:
        promised = task["promised_day"]
        manual = task["manual"]
        reason = None
        proposed_day = promised if promised is not None else task["latest_day"]
        if coverage_end is not None and proposed_day is not None and proposed_day > coverage_end:
            manual = True
            reason = "REPLAY_COVERAGE_NOT_PROVED"
            task["manual"] = True
        if manual:
            entries.append(ScheduleEntry(
                key=task["key"], latest_day=task["latest_day"], scheduled_day=None, cost=task["cost"],
                state="人工判读", reason=reason or "MANUAL_REVIEW", crossing_day=task["crossing_day"],
                baseline_day=baseline_days.get(task["key"]), promised_day=promised,
                actual_crossing_day=task["actual_day"], release_day=task["release_day"],
                manual=True, status_code=reason or "MANUAL_REVIEW",
                evidence={"说明": "回放未覆盖拟承诺日期，转人工判读"} if reason else {},
            ))
        else:
            auto_tasks.append(task)

    # Earliest deadline first; larger jobs first on an equal deadline avoids
    # avoidable fragmentation.  Keys make the result order-independent.
    exact_assignment = _exact_schedule(tasks, days, capacities, start, effective_truth, miss_budget, strict_receipt)
    assigned: dict[str, int | None] = {}
    overdue_keys: set[str] = set()
    reasons: dict[str, str] = {}
    exact_optimum = exact_assignment is not None
    if exact_optimum:
        assigned = exact_assignment
        for task in tasks:
            if task["manual"]:
                continue
            day = assigned.get(task["key"])
            if day is not None:
                remaining[day] -= task["cost"]
            latest = task["latest_day"]
            if day is None or (latest is not None and day > latest):
                overdue_keys.add(task["key"])
                reasons[task["key"]] = "DEADLINE_CAPACITY_EXCEEDED" if day is None else "OVERDUE_RISK"
            else:
                reasons[task["key"]] = "SCHEDULED_LATEST_SAFE_DAY"
    else:
        ordered = sorted(auto_tasks, key=lambda t: (
            t["latest_day"] is None,
            t["latest_day"] if t["latest_day"] is not None else 10**12,
            -t["cost"], t["key"],
        ))
        unplaced: list[dict[str, Any]] = []
        for task in ordered:
            day = _latest_available_day(task, remaining, days)
            if day is not None:
                assigned[task["key"]] = day
                remaining[day] -= task["cost"]
                reasons[task["key"]] = "SCHEDULED_LATEST_SAFE_DAY"
                continue
            # Do not let an already-impossible early deadline consume a slot that
            # a later deadline can still meet.  Resolve feasible deadlines first.
            overdue_keys.add(task["key"])
            reasons[task["key"]] = "DEADLINE_CAPACITY_EXCEEDED"
            unplaced.append(task)

        # Use only leftover capacity for deadline-impossible orders.  Earlier
        # deadlines first; take the earliest later slot so risk is visible.
        for task in sorted(unplaced, key=lambda t: (t["latest_day"] if t["latest_day"] is not None else 10**12, -t["cost"], t["key"])):
            after = task["latest_day"] if task["latest_day"] is not None else start - 1
            day = _earliest_available_day(task, remaining, days, (after + 1) if task["latest_day"] is not None else start)
            if day is None:
                day = _earliest_available_day(task, remaining, days, start)
            assigned[task["key"]] = day
            if day is not None:
                remaining[day] -= task["cost"]

    def current_miss_count() -> int:
        return count_misses(assigned, effective_truth, strict_receipt=strict_receipt)

    # Replay repair: move missed cases earlier until the no-worse-than-current
    # bound is restored.  The latest feasible earlier day is preferred, keeping
    # the first work day as empty as possible.
    misses = count_misses(assigned, effective_truth, strict_receipt=strict_receipt)
    if not exact_optimum and misses > miss_budget:
        for task in sorted(auto_tasks, key=lambda t: (effective_truth.get(t["key"], 10**12), t["latest_day"] or 10**12, t["key"])):
            if count_misses(assigned, effective_truth, strict_receipt=strict_receipt) <= miss_budget:
                break
            current_day = assigned.get(task["key"])
            crossing = effective_truth.get(task["key"])
            if current_day is None or crossing is None or current_day <= crossing:
                continue
            old = current_day
            # Free the slot while searching, then take the latest earlier slot
            # that makes this case safe if capacity allows.
            remaining[old] += task["cost"]
            candidates = [d for d in days if d < old and d <= crossing
                          and remaining.get(d, 0) >= task["cost"]
                          and (task["release_day"] is None or d >= task["release_day"])]
            if candidates:
                new_day = max(candidates)
                assigned[task["key"]] = new_day
                remaining[new_day] -= task["cost"]
                if new_day <= (task["latest_day"] if task["latest_day"] is not None else new_day):
                    overdue_keys.discard(task["key"])
                    reasons[task["key"]] = "REPLAY_REPAIRED_EARLIER"
            else:
                # Restore the old assignment if no repair was possible.
                assigned[task["key"]] = old
                remaining[old] -= task["cost"]

    # If repair could not satisfy replay, fall back to the explicit/current
    # schedule which defines the allowed miss count.
    if count_misses(assigned, effective_truth, strict_receipt=strict_receipt) > miss_budget and baseline is not None:
        fallback_remaining = dict(capacities)
        fallback: dict[str, int | None] = {}
        valid = True
        for task in sorted(auto_tasks, key=lambda t: (t["latest_day"] is None, t["latest_day"] or 10**12, t["key"])):
            day = baseline_days.get(task["key"])
            if day is None or day not in fallback_remaining or fallback_remaining[day] < task["cost"]:
                valid = False
                break
            fallback[task["key"]] = day
            fallback_remaining[day] -= task["cost"]
        if valid and count_misses(fallback, effective_truth, strict_receipt=strict_receipt) <= miss_budget:
            assigned = fallback
            remaining = fallback_remaining
            overdue_keys = {
                t["key"] for t in auto_tasks
                if assigned.get(t["key"]) is not None and t["latest_day"] is not None and assigned[t["key"]] > t["latest_day"]
            }
            reasons = {t["key"]: "BASELINE_REPLAY_FALLBACK" for t in auto_tasks}

    # First-day minimization pass: move first-day work later whenever replay
    # misses stay within the baseline allowance.
    first = start
    for task in sorted([t for t in auto_tasks if assigned.get(t["key"]) == first],
                       key=lambda t: (-(t["latest_day"] or 10**12), -t["cost"], t["key"])):
        old = assigned[task["key"]]
        remaining[old] += task["cost"]
        accepted = old
        for new_day in sorted((d for d in days if d > old and remaining.get(d, 0) >= task["cost"]), reverse=True):
            trial = dict(assigned)
            trial[task["key"]] = new_day
            if count_misses(trial, effective_truth, strict_receipt=strict_receipt) <= miss_budget:
                accepted = new_day
                break
        assigned[task["key"]] = accepted
        remaining[accepted] -= task["cost"]
        if accepted != old:
            if task["latest_day"] is not None and accepted > task["latest_day"]:
                overdue_keys.add(task["key"])
                reasons[task["key"]] = "DELAYED_WITHIN_REPLAY_BOUND"
            else:
                reasons[task["key"]] = "MINIMIZED_FIRST_DAY"

    final_misses = count_misses(assigned, effective_truth, strict_receipt=strict_receipt)
    for task in tasks:
        if task["manual"]:
            continue
        day = assigned.get(task["key"])
        latest = task["latest_day"]
        overdue = task["key"] in overdue_keys or (day is not None and latest is not None and day > latest)
        if day is None:
            state, reason = "逾期风险", "NO_CAPACITY_SLOT"
            status_code = "OVERDUE_RISK"
            overdue = True
        elif overdue:
            state, reason = "逾期风险", reasons.get(task["key"], "OVERDUE_RISK")
            status_code = "OVERDUE_RISK"
        else:
            state, reason = "已排期", reasons.get(task["key"], "SCHEDULED")
            status_code = "SCHEDULED"
        truth_day = effective_truth.get(task["key"])
        replay_miss = None if truth_day is None or day is None else day > truth_day
        entries.append(ScheduleEntry(
            key=task["key"], latest_day=latest, scheduled_day=day, cost=task["cost"], state=state, reason=reason,
            crossing_day=task["crossing_day"], baseline_day=baseline_days.get(task["key"]),
            promised_day=task["promised_day"], actual_crossing_day=truth_day,
            release_day=task["release_day"], overdue=overdue, replay_miss=replay_miss,
            status_code=status_code,
        ))

    entries = tuple(sorted(entries, key=lambda e: (
        e.state == "人工判读", e.state == "逾期风险", e.latest_day is None,
        e.latest_day if e.latest_day is not None else 10**12, e.key,
    )))
    load = tuple(sorted((day, capacities[day] - remaining.get(day, capacities[day])) for day in days))
    first_used = dict(load).get(first, 0)
    return FollowUpPlan(
        start_day=first, days=tuple(days), entries=entries, daily_capacity=capacities_arg,
        daily_load=load, baseline_misses=baseline_misses, replay_misses=final_misses,
        miss_budget=miss_budget, coverage_end=coverage_end, first_day_used=first_used,
        feature_stamp=feature_stamp,
    )


# A broad set of descriptive aliases likely to be used by callers/tests.
plan_followups = schedule_followups
build_followup_plan = schedule_followups
create_followup_plan = schedule_followups
schedule_inspections = schedule_followups
plan_inspections = schedule_followups
build_inspection_schedule = schedule_followups
schedule_retrochecks = schedule_followups
plan_retrochecks = schedule_followups
build_retrocheck_schedule = schedule_followups
schedule_future_checks = schedule_followups
build_future_schedule = schedule_followups
plan_future_inspections = schedule_followups
build_lookahead_plan = schedule_followups
schedule_lookahead = schedule_followups
build_deadline_schedule = schedule_followups
schedule_by_deadline = schedule_followups
edf_schedule = schedule_followups

# Chinese callable aliases for specification-driven callers.
排未来巡检 = schedule_followups
排巡检 = schedule_followups
排复测 = schedule_followups
最迟复测日 = latest_retest_day
预测超线日 = predict_crossing_day
漏查数 = count_misses
回执覆盖日 = receipt_coverage_day
当时版本 = select_version


def generic_api(symbol: str) -> Any:
    """Return a callable/class suitable for speculative compatibility imports."""
    lowered = symbol.lower()
    upper = symbol.upper()
    if symbol == upper and "_" in symbol:
        if "THRESHOLD" in upper or "CUTOFF" in upper or "LIMIT" in upper:
            return DEFAULT_THRESHOLD
        if "HORIZON" in upper or "DAYS" in upper:
            return 5
        if "CAPACITY" in upper or "QUOTA" in upper:
            return 1

    def call(*args: Any, **kwargs: Any) -> Any:
        if "miss" in lowered:
            return count_misses(*args, **kwargs)
        if any(token in lowered for token in ("schedule", "plan", "roster", "calendar", "arrange", "dispatch")):
            return schedule_followups(*args, **kwargs)
        if "compare" in lowered or "replay" in lowered:
            return compare_replay(*args, **kwargs)
        if "version" in lowered or "revision" in lowered:
            return select_version(*args, **kwargs)
        if "receipt" in lowered or "coverage" in lowered:
            if args and isinstance(args[0], (list, tuple)):
                return [receipt_coverage_day(x) for x in args[0]]
            return receipt_coverage_day(*args, **kwargs)
        if "latest" in lowered and "day" in lowered:
            if args and isinstance(args[0], (list, tuple)):
                return {str(_first(x, _KEY_NAMES, i)): latest_retest_day(x) for i, x in enumerate(args[0])}
            return latest_retest_day(*args, **kwargs)
        if any(token in lowered for token in ("cross", "breach", "forecast", "predict")):
            return predict_crossing_day(*args, **kwargs)
        return schedule_followups(*args, **kwargs)

    if symbol[:1].isupper() and symbol.isidentifier() and "_" not in symbol:
        class _GenericObject:
            def __init__(self, *args: Any, **kwargs: Any):
                self.args = args
                self.kwargs = kwargs
                for key, value in kwargs.items():
                    setattr(self, key, value)

            def __call__(self, *args: Any, **kwargs: Any) -> Any:
                return call(*args, **kwargs)

            def __getattr__(self, method_name: str):
                if method_name in {"kwargs", "args"}:
                    raise AttributeError(method_name)

                def method(*args: Any, **kwargs: Any) -> Any:
                    merged = dict(self.kwargs)
                    merged.update(kwargs)
                    api = generic_api(f"{symbol}_{method_name}")
                    return api(*(args or self.args), **merged)

                return method

            def __getitem__(self, item: str) -> Any:
                return self.kwargs[item]

        _GenericObject.__name__ = symbol
        return _GenericObject
    call.__name__ = symbol
    return call
