"""XP, levels, streaks and badges for students.

All XP changes go through these helpers; each is idempotent per
(student, event_type, source_id) via XPEvent.
"""

from datetime import datetime
from .models import Attempt, CustomTestAttempt, StudentGamification, XPEvent, LessonCompletion


# How many submits of the same test / course per day still earn XP. Later
# submits are graded as usual but give 0 XP, so repeating one test can't be
# used to climb the leaderboard.
DAILY_XP_SUBMITS_PER_ITEM = 3


def _calculate_level(xp_total: int) -> int:
    if xp_total < 0:
        xp_total = 0
    return (xp_total // 150) + 1


def _get_or_create_gamification_profile(student_id):
    profile = StudentGamification.objects(student_id=student_id).first()
    if not profile:
        profile = StudentGamification(student_id=student_id, xp_total=0, level=1)
        profile.save()
    return profile


def _update_streak(profile):
    now = datetime.utcnow()
    today = now.date()
    last_activity = getattr(profile, "last_activity_date", None)

    if last_activity:
        last_date = last_activity.date()
        if last_date == today:
            return
        if (today - last_date).days == 1:
            profile.current_streak = int(profile.current_streak or 0) + 1
        else:
            profile.current_streak = 1
    else:
        profile.current_streak = 1

    profile.best_streak = max(int(profile.best_streak or 0), int(profile.current_streak or 0))
    profile.last_activity_date = now


def _update_badges(profile):
    badges = set(getattr(profile, "badges", []) or [])

    xp_total = int(profile.xp_total or 0)
    best_streak = int(profile.best_streak or 0)

    if xp_total >= 1:
        badges.add("xp_starter")
    if xp_total >= 100:
        badges.add("xp_100")
    if xp_total >= 500:
        badges.add("xp_500")
    if best_streak >= 3:
        badges.add("streak_3")
    if best_streak >= 7:
        badges.add("streak_7")

    lesson_count = LessonCompletion.objects(student_id=profile.student_id.id).count()
    if lesson_count >= 5:
        badges.add("lesson_5")

    test_count = Attempt.objects(student_id=profile.student_id.id).count() + CustomTestAttempt.objects(student_id=profile.student_id.id, status="submitted").count()
    if test_count >= 5:
        badges.add("test_5")

    profile.badges = sorted(list(badges))


def _daily_xp_limit_reached(model_cls, time_field: str, **filters) -> bool:
    """True when the student already earned XP DAILY_XP_SUBMITS_PER_ITEM times today for this item."""
    day_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    rewarded_today = model_cls.objects(
        xp_earned__gt=0,
        **{f"{time_field}__gte": day_start},
        **filters,
    ).count()
    return rewarded_today >= DAILY_XP_SUBMITS_PER_ITEM


def _award_xp_for_attempt(student_id, event_type: str, source_id: str, score: int, total: int, is_retake: bool = False):
    existing_event = XPEvent.objects(
        student_id=student_id,
        event_type=event_type,
        source_id=source_id,
    ).first()
    profile = _get_or_create_gamification_profile(student_id)
    if existing_event:
        return 0, profile

    base_xp = 20 if event_type == "custom_test_submit" else 15
    pct = (score / total * 100) if total else 0
    bonus_xp = 0
    if pct >= 90:
        bonus_xp = 20
    elif pct >= 75:
        bonus_xp = 10
    elif pct >= 50:
        bonus_xp = 5

    earned_xp = base_xp + bonus_xp
    if is_retake:
        # Keep retakes rewarding but slightly lower to reduce exploitability.
        earned_xp = max(5, int(earned_xp * 0.7))

    XPEvent(
        student_id=student_id,
        event_type=event_type,
        source_id=source_id,
        xp=earned_xp,
    ).save()

    profile.xp_total = (profile.xp_total or 0) + earned_xp
    profile.level = _calculate_level(profile.xp_total)
    _update_streak(profile)
    _update_badges(profile)
    profile.updated_at = datetime.utcnow()
    profile.save()
    return earned_xp, profile


def _award_flat_xp_once(student_id, event_type: str, source_id: str, amount: int):
    existing_event = XPEvent.objects(
        student_id=student_id,
        event_type=event_type,
        source_id=source_id,
    ).first()
    profile = _get_or_create_gamification_profile(student_id)
    if existing_event:
        return 0, profile

    earned_xp = max(0, int(amount or 0))
    XPEvent(
        student_id=student_id,
        event_type=event_type,
        source_id=source_id,
        xp=earned_xp,
    ).save()

    profile.xp_total = (profile.xp_total or 0) + earned_xp
    profile.level = _calculate_level(profile.xp_total)
    _update_streak(profile)
    _update_badges(profile)
    profile.updated_at = datetime.utcnow()
    profile.save()
    return earned_xp, profile


def _apply_xp_penalty_once(student_id, event_type: str, source_id: str, penalty_amount: int):
    existing_event = XPEvent.objects(
        student_id=student_id,
        event_type=event_type,
        source_id=source_id,
    ).first()
    profile = _get_or_create_gamification_profile(student_id)
    if existing_event:
        return 0, profile

    penalty = max(0, int(penalty_amount or 0))
    current_xp = int(profile.xp_total or 0)
    applied_penalty = min(penalty, current_xp)
    delta_xp = -applied_penalty

    XPEvent(
        student_id=student_id,
        event_type=event_type,
        source_id=source_id,
        xp=delta_xp,
    ).save()

    profile.xp_total = max(0, current_xp + delta_xp)
    profile.level = _calculate_level(profile.xp_total)
    _update_badges(profile)
    profile.updated_at = datetime.utcnow()
    profile.save()
    return applied_penalty, profile
