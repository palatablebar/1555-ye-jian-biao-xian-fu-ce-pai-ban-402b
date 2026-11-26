from dataclasses import dataclass, replace

RISK_CUTOFF = 25


@dataclass(frozen=True, slots=True)
class Policy:
    capacity: int
    group_quota: int
    trigger: float

    @classmethod
    def select(cls, capacity, mode):
        if type(capacity) is not int or not 1 <= capacity <= 60:
            raise ValueError('本班额度须在 1 至 60 之间')
        if mode == 'balanced':
            per_group = max(3, (capacity*3 + 4)//5)
            return cls(capacity, per_group, RISK_CUTOFF)
        if mode == 'focus':
            return cls(capacity, capacity, RISK_CUTOFF * .92)
        raise ValueError('未知复核策略')


@dataclass(frozen=True, slots=True)
class RuleNotice:
    rule_id: str
    meaning: str
    hard: bool = False


@dataclass(frozen=True, slots=True)
class Job:
    key: str
    cohort: str
    estimate: float
    lower: float | None
    upper: float | None
    cost: int
    state: str
    reason: str
    summary: str
    priority: float
    rule_hits: tuple[str, ...]
    latest_retest_day: int | None = None
    scheduled_day: int | None = None
    schedule_state: str | None = None
    schedule_reason: str | None = None

    def change(self, state, reason, summary):
        return replace(self, state=state, reason=reason, summary=summary)

    @property
    def due_day(self):
        return self.latest_retest_day

    @property
    def deadline_day(self):
        return self.latest_retest_day

    @property
    def actual_day(self):
        return self.scheduled_day

    @property
    def booked_day(self):
        return self.scheduled_day


@dataclass(frozen=True, slots=True)
class Plan:
    jobs: tuple[Job, ...]
    capacity: int
    balance: int
    used_by_group: tuple[tuple[str, int], ...]
    followup: object | None = None
    future_schedule: object | None = None

    def __getitem__(self, name):
        if isinstance(name, int):
            return self.jobs[name]
        if name in {'jobs', 'capacity', 'balance', 'used_by_group', 'followup', 'future_schedule'}:
            return getattr(self, name)
        for job in self.jobs:
            if job.key == name:
                return job
        raise KeyError(name)


def __getattr__(name):
    from .followup import generic_api
    return generic_api(name)
