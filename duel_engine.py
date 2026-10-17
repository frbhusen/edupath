"""Duel engine: matchmaking limits, timers, scoring and XP settlement."""

import json
import random
import secrets
from datetime import datetime, timedelta
from bson import ObjectId
from mongoengine.errors import DoesNotExist
from .models import (
    User,
    Subject,
    Section,
    Lesson,
    Test,
    Question,
    XPEvent,
    Duel,
    DuelAnswer,
    DuelStats,
)
from .xp import _calculate_level, _get_or_create_gamification_profile, _update_badges


DUEL_INVITE_COOLDOWN_SECONDS = 60
DUEL_SAME_OPPONENT_COOLDOWN_SECONDS = 180
DUEL_PENDING_LIMIT_PER_CHALLENGER = 3
DUEL_WAITING_JOIN_TIMEOUT_SECONDS = 300
DUEL_PAIR_RECENT_LOCK_SECONDS = 45


def _duel_generate_token(length: int = 32):
    # URL-safe one-time invitation token.
    token = secrets.token_urlsafe(max(24, length))
    return token[:64]


def _duel_get_or_create_stats(student_id):
    stats = DuelStats.objects(student_id=student_id).first()
    if not stats:
        stats = DuelStats(student_id=student_id)
        stats.save()
    return stats


def _duel_get_scope_info(scope_type: str, scope_id: str):
    scope_type = (scope_type or "").strip().lower()
    scope_id = (scope_id or "").strip()
    if not ObjectId.is_valid(scope_id):
        return None, None, None

    if scope_type == "lesson":
        lesson = Lesson.objects(id=scope_id).first()
        if not lesson:
            return None, None, None
        return "lesson", lesson.id, lesson.title

    if scope_type == "section":
        section = Section.objects(id=scope_id).first()
        if not section:
            return None, None, None
        return "section", section.id, section.title

    if scope_type == "subject":
        subject = Subject.objects(id=scope_id).first()
        if not subject:
            return None, None, None
        return "subject", subject.id, subject.name

    return None, None, None


def _duel_pick_questions(scope_type: str, scope_id, question_count: int = 15):
    test_ids = []
    if scope_type == "lesson":
        test_ids = [t.id for t in Test.objects(lesson_id=scope_id).only("id").all()]
    elif scope_type == "section":
        test_ids = [t.id for t in Test.objects(section_id=scope_id).only("id").all()]
    elif scope_type == "subject":
        sections = Section.objects(subject_id=scope_id).only("id").all()
        section_ids = [s.id for s in sections]
        if section_ids:
            test_ids = [t.id for t in Test.objects(section_id__in=section_ids).only("id").all()]

    if not test_ids:
        return []

    questions = list(Question.objects(test_id__in=test_ids).all())
    if len(questions) < question_count:
        return []

    random.shuffle(questions)
    picked = questions[:question_count]
    return [str(q.id) for q in picked]


def _duel_time_left_seconds(duel, player_slot: str, now=None):
    now = now or datetime.utcnow()
    if not duel.started_at:
        return int(duel.timer_seconds or 540)

    elapsed = max(0, int((now - duel.started_at).total_seconds()))
    penalty = int(duel.challenger_penalty_seconds or 0) if player_slot == "challenger" else int(duel.opponent_penalty_seconds or 0)
    left = int(duel.timer_seconds or 540) - elapsed - penalty
    return max(0, left)


def _duel_safe_user(user_ref):
    try:
        return user_ref
    except (DoesNotExist, AttributeError):
        return None


def _duel_safe_user_id(user_ref):
    user = _duel_safe_user(user_ref)
    return getattr(user, "id", None) if user else None


def _duel_safe_user_name(user_ref, fallback="لاعب"):
    user = _duel_safe_user(user_ref)
    return user.full_name if user else fallback


def _duel_player_slot(duel, user_id):
    if _duel_safe_user_id(getattr(duel, "challenger_id", None)) == user_id:
        return "challenger"
    if _duel_safe_user_id(getattr(duel, "opponent_id", None)) == user_id:
        return "opponent"
    return None


def _duel_slot_has_joined(duel, slot: str):
    if slot == "challenger":
        return bool(getattr(duel, "challenger_joined_at", None))
    if slot == "opponent":
        return bool(getattr(duel, "opponent_joined_at", None))
    return False


def _duel_slot_submitted(duel, slot: str):
    if slot == "challenger":
        return bool(getattr(duel, "challenger_submitted", False))
    if slot == "opponent":
        return bool(getattr(duel, "opponent_submitted", False))
    return False


def _duel_slot_finished_at(duel, slot: str):
    if slot == "challenger":
        return getattr(duel, "challenger_finished_at", None)
    if slot == "opponent":
        return getattr(duel, "opponent_finished_at", None)
    return None


def _duel_compute_phase(duel, slot: str):
    slot_joined = _duel_slot_has_joined(duel, slot)
    both_joined = _duel_slot_has_joined(duel, "challenger") and _duel_slot_has_joined(duel, "opponent")
    my_submitted = _duel_slot_submitted(duel, slot)
    opp_slot = "opponent" if slot == "challenger" else "challenger"
    opp_submitted = _duel_slot_submitted(duel, opp_slot)

    if duel.status == "completed":
        return "completed"
    if duel.status == "accepted_waiting" or not slot_joined or not both_joined:
        return "waiting"
    if duel.status == "live" and my_submitted and not opp_submitted:
        return "submitted_waiting"
    if duel.status == "live":
        return "live"
    return "waiting"


def _duel_build_play_state(duel, slot: str):
    opp_slot = "opponent" if slot == "challenger" else "challenger"
    my_submitted = _duel_slot_submitted(duel, slot)
    opp_submitted = _duel_slot_submitted(duel, opp_slot)

    my_now = _duel_slot_finished_at(duel, slot) if my_submitted else None
    opp_now = _duel_slot_finished_at(duel, opp_slot) if opp_submitted else None

    return {
        "phase": _duel_compute_phase(duel, slot),
        "slot_joined": _duel_slot_has_joined(duel, slot),
        "both_joined": _duel_slot_has_joined(duel, "challenger") and _duel_slot_has_joined(duel, "opponent"),
        "my_submitted": my_submitted,
        "opp_submitted": opp_submitted,
        "my_left": _duel_time_left_seconds(duel, slot, now=my_now),
        "opp_left": _duel_time_left_seconds(duel, opp_slot, now=opp_now),
    }


def _duel_compute_live_scores(duel):
    challenger_id = _duel_safe_user_id(getattr(duel, "challenger_id", None))
    opponent_id = _duel_safe_user_id(getattr(duel, "opponent_id", None))
    if not challenger_id or not opponent_id:
        return int(duel.challenger_score or 0), int(duel.opponent_score or 0)

    answers = list(DuelAnswer.objects(duel_id=duel.id).only("player_id", "is_correct").all())
    challenger_score = 0
    opponent_score = 0
    for answer in answers:
        if not getattr(answer, "is_correct", False):
            continue
        pid = _duel_safe_user_id(getattr(answer, "player_id", None))
        if pid == challenger_id:
            challenger_score += 1
        elif pid == opponent_id:
            opponent_score += 1
    return challenger_score, opponent_score


def _duel_compute_settlement_plan(
    challenger_score: int,
    opponent_score: int,
    challenger_finished_at,
    opponent_finished_at,
    challenger_streak_before: int,
    opponent_streak_before: int,
):
    challenger_score = int(challenger_score or 0)
    opponent_score = int(opponent_score or 0)
    challenger_streak_before = int(challenger_streak_before or 0)
    opponent_streak_before = int(opponent_streak_before or 0)

    if challenger_score > opponent_score:
        winner_slot = "challenger"
    elif opponent_score > challenger_score:
        winner_slot = "opponent"
    else:
        cf = challenger_finished_at or datetime.max
        of = opponent_finished_at or datetime.max
        winner_slot = "challenger" if cf <= of else "opponent"

    loser_slot = "opponent" if winner_slot == "challenger" else "challenger"
    winner_streak_before = challenger_streak_before if winner_slot == "challenger" else opponent_streak_before
    loser_streak_before = opponent_streak_before if loser_slot == "opponent" else challenger_streak_before

    winner_streak_after = winner_streak_before + 1
    base_win_xp = 35 if winner_streak_before >= 10 else 30

    streak_bonus = 0
    if winner_streak_after == 5:
        streak_bonus = 30
    elif winner_streak_after == 7:
        streak_bonus = 50
    elif winner_streak_after == 10:
        streak_bonus = 75

    loser_penalty = -5 if loser_streak_before >= 10 else 0
    return {
        "winner_slot": winner_slot,
        "loser_slot": loser_slot,
        "base_win_xp": base_win_xp,
        "streak_bonus": streak_bonus,
        "loser_penalty": loser_penalty,
        "winner_streak_after": winner_streak_after,
    }


def _duel_invite_throttle_decision(
    now,
    pending_count: int,
    latest_any_created_at,
    latest_same_created_at,
):
    pending_count = int(pending_count or 0)
    if pending_count >= DUEL_PENDING_LIMIT_PER_CHALLENGER:
        return {
            "allowed": False,
            "reason": "pending_limit",
            "remaining": None,
        }

    if latest_any_created_at:
        since_last = (now - latest_any_created_at).total_seconds()
        if since_last < DUEL_INVITE_COOLDOWN_SECONDS:
            return {
                "allowed": False,
                "reason": "global_cooldown",
                "remaining": max(0, int(DUEL_INVITE_COOLDOWN_SECONDS - since_last)),
            }

    if latest_same_created_at:
        since_pair = (now - latest_same_created_at).total_seconds()
        if since_pair < DUEL_SAME_OPPONENT_COOLDOWN_SECONDS:
            return {
                "allowed": False,
                "reason": "same_opponent_cooldown",
                "remaining": max(0, int(DUEL_SAME_OPPONENT_COOLDOWN_SECONDS - since_pair)),
            }

    return {
        "allowed": True,
        "reason": None,
        "remaining": None,
    }


def _duel_should_apply_finish_penalty(other_time_left_seconds: int):
    # Rule: deduct 15 seconds only when remaining time is >= 20 seconds.
    return int(other_time_left_seconds or 0) >= 20


def _duel_pair_lock_remaining_from_latest(now, latest_status, latest_baseline_dt):
    if latest_status in {"pending", "accepted_waiting", "live"}:
        return DUEL_PAIR_RECENT_LOCK_SECONDS
    if not latest_baseline_dt:
        return 0

    elapsed = int((now - latest_baseline_dt).total_seconds())
    remaining = DUEL_PAIR_RECENT_LOCK_SECONDS - elapsed
    return max(0, remaining)


def _duel_apply_xp_delta_once(student_id, event_type: str, source_id: str, delta_xp: int):
    exists = XPEvent.objects(student_id=student_id, event_type=event_type, source_id=source_id).first()
    if exists:
        return 0

    delta_xp = int(delta_xp or 0)
    XPEvent(
        student_id=student_id,
        event_type=event_type,
        source_id=source_id,
        xp=delta_xp,
    ).save()

    profile = _get_or_create_gamification_profile(student_id)
    profile.xp_total = int(profile.xp_total or 0) + delta_xp
    profile.level = _calculate_level(profile.xp_total)
    _update_badges(profile)
    profile.updated_at = datetime.utcnow()
    profile.save()
    return delta_xp


def _duel_get_xp_change_summary(duel, student_id):
    source_prefix = f"{duel.id}:"
    events = list(
        XPEvent.objects(student_id=student_id, source_id__startswith=source_prefix)
        .order_by("created_at")
        .all()
    )

    labels = {
        "duel_entry_fee": "رسوم دخول التحدي",
        "duel_win": "مكافأة الفوز",
        "duel_loss": "الجواهر نتيجة الخسارة",
        "duel_streak_bonus": "مكافأة سلسلة الانتصارات",
        "duel_first_perfect_bonus": "مكافأة 100% (المنهي الأول)",
        "duel_second_perfect_refund": "استرجاع رسوم الدخول",
        "duel_second_perfect_bonus": "مكافأة 100% (المنهي الثاني)",
    }

    items = []
    total_delta = 0
    for row in events:
        xp = int(getattr(row, "xp", 0) or 0)
        total_delta += xp
        items.append(
            {
                "label": labels.get(getattr(row, "event_type", ""), getattr(row, "event_type", "جواهر")),
                "xp": xp,
            }
        )

    profile = _get_or_create_gamification_profile(student_id)
    after_total = int(profile.xp_total or 0)
    before_total = after_total - total_delta

    return {
        "has_any": bool(items),
        "total": total_delta,
        "before_total": before_total,
        "after_total": after_total,
        "items": items,
    }


def _duel_try_settle(duel):
    if duel.settled or duel.status != "completed":
        return

    challenger_id = duel.challenger_id.id if duel.challenger_id else None
    opponent_id = duel.opponent_id.id if duel.opponent_id else None
    if not challenger_id or not opponent_id:
        return

    challenger_stats = _duel_get_or_create_stats(challenger_id)
    opponent_stats = _duel_get_or_create_stats(opponent_id)
    challenger_before = int(challenger_stats.current_win_streak or 0)
    opponent_before = int(opponent_stats.current_win_streak or 0)

    plan = _duel_compute_settlement_plan(
        challenger_score=int(duel.challenger_score or 0),
        opponent_score=int(duel.opponent_score or 0),
        challenger_finished_at=duel.challenger_finished_at,
        opponent_finished_at=duel.opponent_finished_at,
        challenger_streak_before=challenger_before,
        opponent_streak_before=opponent_before,
    )
    winner_slot = plan["winner_slot"]
    loser_slot = plan["loser_slot"]
    winner_id = challenger_id if winner_slot == "challenger" else opponent_id
    loser_id = opponent_id if winner_slot == "challenger" else challenger_id

    winner_stats = challenger_stats if winner_slot == "challenger" else opponent_stats
    loser_stats = opponent_stats if loser_slot == "opponent" else challenger_stats
    base_win_xp = int(plan["base_win_xp"])
    winner_stats.current_win_streak = int(plan["winner_streak_after"])
    winner_stats.best_win_streak = max(int(winner_stats.best_win_streak or 0), int(winner_stats.current_win_streak or 0))
    winner_stats.wins = int(winner_stats.wins or 0) + 1
    winner_stats.total_duels = int(winner_stats.total_duels or 0) + 1
    winner_stats.updated_at = datetime.utcnow()

    streak_bonus = int(plan["streak_bonus"])

    loser_stats.losses = int(loser_stats.losses or 0) + 1
    loser_stats.total_duels = int(loser_stats.total_duels or 0) + 1
    loser_stats.current_win_streak = 0
    loser_stats.updated_at = datetime.utcnow()

    loser_penalty = int(plan["loser_penalty"])

    source_base = str(duel.id)
    _duel_apply_xp_delta_once(winner_id, "duel_win", f"{source_base}:winner", base_win_xp)
    _duel_apply_xp_delta_once(loser_id, "duel_loss", f"{source_base}:loser", 5 + loser_penalty)
    if streak_bonus > 0:
        _duel_apply_xp_delta_once(winner_id, "duel_streak_bonus", f"{source_base}:streak", streak_bonus)

    first_slot = getattr(duel, "first_submitter_slot", None)
    first_perfect = bool(getattr(duel, "first_submitter_perfect", False))
    second_perfect = bool(getattr(duel, "second_submitter_perfect", False))
    if first_perfect and first_slot in {"challenger", "opponent"}:
        first_id = challenger_id if first_slot == "challenger" else opponent_id
        _duel_apply_xp_delta_once(first_id, "duel_first_perfect_bonus", f"{source_base}:first_perfect", 5)

    if first_perfect and second_perfect and first_slot in {"challenger", "opponent"}:
        second_slot = "opponent" if first_slot == "challenger" else "challenger"
        second_id = challenger_id if second_slot == "challenger" else opponent_id
        fee_refund = int(duel.entry_fee_xp or 20)
        _duel_apply_xp_delta_once(second_id, "duel_second_perfect_refund", f"{source_base}:second_refund", fee_refund)
        _duel_apply_xp_delta_once(second_id, "duel_second_perfect_bonus", f"{source_base}:second_bonus", 3)

    winner_stats.save()
    loser_stats.save()

    winner_user = User.objects(id=winner_id).first()
    duel.winner_id = winner_user
    duel.ended_at = duel.ended_at or datetime.utcnow()
    duel.settled = True
    duel.settlement_json = json.dumps(
        {
            "winner_id": str(winner_id),
            "base_win_xp": base_win_xp,
            "loser_xp": 5,
            "loser_penalty": loser_penalty,
            "streak_bonus": streak_bonus,
            "first_submitter_slot": first_slot,
            "first_submitter_perfect": first_perfect,
            "second_submitter_perfect": second_perfect,
        },
        ensure_ascii=False,
    )
    duel.save()


def _duel_expire_if_needed(duel):
    if duel.status == "pending" and duel.expires_at and duel.expires_at < datetime.utcnow():
        duel.status = "expired"
        duel.ended_at = datetime.utcnow()
        duel.save()
        return True
    return False


def _duel_autosubmit_timeout(duel):
    if duel.status != "live":
        return

    changed = False
    now = datetime.utcnow()
    if not duel.challenger_submitted and _duel_time_left_seconds(duel, "challenger", now=now) <= 0:
        duel.challenger_submitted = True
        duel.challenger_finished_at = now
        duel.challenger_score = int(duel.challenger_score or 0)
        changed = True

    if not duel.opponent_submitted and _duel_time_left_seconds(duel, "opponent", now=now) <= 0:
        duel.opponent_submitted = True
        duel.opponent_finished_at = now
        duel.opponent_score = int(duel.opponent_score or 0)
        changed = True

    if duel.challenger_submitted and duel.opponent_submitted:
        duel.status = "completed"
        duel.ended_at = now
        changed = True

    if changed:
        duel.save()
        if duel.status == "completed":
            _duel_try_settle(duel)


def _duel_refund_entry_if_needed(duel):
    if not duel or not duel.fee_applied:
        return

    fee = int(duel.entry_fee_xp or 20)
    _duel_apply_xp_delta_once(duel.challenger_id.id, "duel_entry_refund", f"{duel.id}:challenger_refund", fee)
    _duel_apply_xp_delta_once(duel.opponent_id.id, "duel_entry_refund", f"{duel.id}:opponent_refund", fee)


def _duel_expire_waiting_if_needed(duel):
    if duel.status != "accepted_waiting":
        return False

    baseline = duel.started_at or duel.opponent_joined_at or duel.challenger_joined_at or duel.created_at
    if not baseline:
        return False

    deadline = baseline + timedelta(seconds=DUEL_WAITING_JOIN_TIMEOUT_SECONDS)
    if datetime.utcnow() < deadline:
        return False

    duel.status = "expired"
    duel.ended_at = datetime.utcnow()
    duel.save()
    _duel_refund_entry_if_needed(duel)
    return True


def _duel_maintenance_tick_for_student(student_id):
    if not student_id:
        return

    active_rows = list(
        Duel.objects(
            __raw__={
                "$or": [
                    {"challenger_id": student_id},
                    {"opponent_id": student_id},
                ],
                "status": {"$in": ["pending", "accepted_waiting", "live", "completed"]},
            }
        )
        .order_by("-created_at")
        .limit(20)
        .all()
    )

    for duel in active_rows:
        if duel.status == "pending":
            _duel_expire_if_needed(duel)
            continue
        if duel.status == "accepted_waiting":
            _duel_expire_waiting_if_needed(duel)
            continue
        if duel.status == "live":
            _duel_autosubmit_timeout(duel)
            continue
        if duel.status == "completed":
            _duel_try_settle(duel)


def _duel_pair_recent_lock_remaining(challenger_id, opponent_id, now=None):
    now = now or datetime.utcnow()
    latest_pair = (
        Duel.objects(
            __raw__={
                "$or": [
                    {"challenger_id": challenger_id, "opponent_id": opponent_id},
                    {"challenger_id": opponent_id, "opponent_id": challenger_id},
                ]
            }
        )
        .order_by("-created_at")
        .first()
    )
    if not latest_pair:
        return 0

    baseline = latest_pair.ended_at or latest_pair.created_at
    return _duel_pair_lock_remaining_from_latest(
        now=now,
        latest_status=latest_pair.status,
        latest_baseline_dt=baseline,
    )
