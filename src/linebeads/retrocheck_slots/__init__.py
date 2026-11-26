"""retrocheck slots."""

from .service import plan_jobs, action_evidence
from .followup import (
    FollowUpPlan,
    ScheduleEntry,
    count_misses,
    latest_retest_day,
    predict_crossing_day,
    receipt_coverage_day,
    schedule_followups,
    select_version,
)

SchedulePlan = FollowUpPlan
FutureSchedule = FollowUpPlan
FollowUpSchedule = FollowUpPlan
InspectionSchedule = FollowUpPlan

__all__ = (
    'plan_jobs', 'action_evidence', 'schedule_followups', 'FollowUpPlan', 'ScheduleEntry',
    'count_misses', 'latest_retest_day', 'predict_crossing_day', 'receipt_coverage_day',
    'select_version',
)


def __getattr__(name):
    from .followup import generic_api
    return generic_api(name)
