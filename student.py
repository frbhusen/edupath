from flask import Blueprint, render_template, redirect, url_for, flash, request, session, jsonify, Response
import json
import random
import math
import secrets
from urllib.parse import quote
from datetime import datetime, timedelta
import time
from flask_login import login_required, current_user
from bson import ObjectId
from mongoengine.errors import DoesNotExist

from .models import User, Subject, Section, Lesson, Test, Question, Choice, Attempt, AttemptAnswer, AttemptTextAnswer, TestTextQuestion, ActivationCode, SectionActivation, LessonActivationCode, LessonActivation, SubjectActivation, SubjectActivationCode, CustomTestAttempt, CustomTestAnswer, StudentGamification, XPEvent, LessonCompletion, Assignment, AssignmentSubmission, AssignmentAttempt, StudyPlan, StudyPlanItem, DiscussionQuestion, DiscussionAnswer, Certificate, Duel, DuelAnswer, DuelStats, CourseSet, CourseQuestion, CourseAttempt, CourseAnswer
from .forms import ActivationForm, LessonActivationForm
from .activation_utils import cascade_subject_activation, cascade_section_activation, cascade_lesson_activation
from .extensions import cache

student_bp = Blueprint("student", __name__, template_folder="templates")

DUEL_INVITE_COOLDOWN_SECONDS = 60
DUEL_SAME_OPPONENT_COOLDOWN_SECONDS = 180
DUEL_PENDING_LIMIT_PER_CHALLENGER = 3
DUEL_WAITING_JOIN_TIMEOUT_SECONDS = 300
DUEL_PAIR_RECENT_LOCK_SECONDS = 45


class AccessContext:
    """Per-student access computation for a section with three-level hierarchy: Subject → Section → Lesson."""

    def __init__(self, section: Section, student_id: int):
        self.section = section
        self.student_id = student_id
        self.subject = section.subject
        
        # Check if entire subject is activated
        self.subject_requires_code = getattr(self.subject, "requires_code", False)
        self.subject_active = bool(
            SubjectActivation.objects(subject_id=self.subject.id, student_id=student_id, active=True).first()
        )
        self.subject_open = self.subject_active or not self.subject_requires_code
        
        # Check if section is activated
        self.section_requires_code = section.requires_code
        self.section_active = bool(
            SectionActivation.objects(section_id=section.id, student_id=student_id, active=True).first()
        )
        
        # Section is "open" if:
        # - Subject is activated OR
        # - Subject is not locked and section doesn't require code OR is activated
        # - Subject is locked but section is activated
        if self.subject_active:
            self.section_open = True
        elif self.subject_requires_code:
            self.section_open = self.section_active
        else:
            self.section_open = self.section_active or not self.section_requires_code

        # Precompute active lesson activations for faster access checks
        lesson_ids = [l.id for l in section.lessons]

        if lesson_ids:
            self.lesson_activation_ids = set()
            for la in LessonActivation.objects(
                lesson_id__in=lesson_ids,
                student_id=student_id,
                active=True,
            ).all():
                try:
                    if la.lesson_id and la.lesson_id.id:
                        self.lesson_activation_ids.add(la.lesson_id.id)
                except (DoesNotExist, AttributeError):
                    continue
        else:
            self.lesson_activation_ids = set()

        self.first_lesson_id = None
        self.first_section_wide_test_id = None

    def lesson_open(self, lesson: Lesson) -> bool:
        """Check if student can access this lesson."""
        # If subject is activated, everything is open
        if self.subject_active:
            return True

        # If subject is locked, allow only section-activated or lesson-activated
        if self.subject_requires_code:
            if self.section_active:
                return True
            return lesson.id in self.lesson_activation_ids

        # Subject is open; section rules apply
        if self.section_open:
            return True

        # Section locked but lesson activated
        return lesson.id in self.lesson_activation_ids

    def test_open(self, test: Test) -> bool:
        """Check if student can access this test.
        
        Logic:
        - Subject activated → all tests accessible
        - Section activated → all tests in section accessible (lesson + section-wide)
        - Lesson activated → only tests linked to that lesson accessible
        - Section-wide tests: require section or subject activation
        """
        # If subject is activated, all tests are accessible
        if self.subject_active:
            return True

        # Lesson-linked tests follow lesson access
        if test.lesson_id:
            lesson = test.lesson
            return bool(lesson and self.lesson_open(lesson))

        # Section-wide tests
        if self.subject_requires_code:
            return self.section_active

        return self.section_open


def _to_int(value, default=0):
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _load_attempt_settings(attempt):
    raw = getattr(attempt, "selection_settings_json", None)
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _extract_attempt_question_ids(attempt):
    raw = getattr(attempt, "question_order_json", None)
    if raw:
        try:
            qids = json.loads(raw)
            if isinstance(qids, list):
                return [str(qid) for qid in qids if ObjectId.is_valid(str(qid))]
        except Exception:
            pass

    # Fallback for old attempts created before question order was persisted.
    ordered_answers = AttemptAnswer.objects(attempt_id=attempt.id).order_by("id").all()
    return [str(ans.question_id.id) for ans in ordered_answers if ans.question_id]


def _calculate_level(xp_total: int) -> int:
    if xp_total < 0:
        xp_total = 0
    return (xp_total // 200) + 1


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


def _avatar_text_for_user(user):
    first = (getattr(user, "first_name", "") or "").strip()
    last = (getattr(user, "last_name", "") or "").strip()
    if first or last:
        return (first[:1] + last[:1]).upper()
    username = (getattr(user, "username", "") or "").strip()
    return (username[:2] or "U").upper()


def _certificate_counts_for_students(student_ids):
    if not student_ids:
        return {}

    pipeline = [
        {"$match": {"student_id": {"$in": list(student_ids)}, "is_verified": True}},
        {"$group": {"_id": "$student_id", "count": {"$sum": 1}}},
    ]
    rows = list(Certificate._get_collection().aggregate(pipeline))
    return {row.get("_id"): int(row.get("count", 0) or 0) for row in rows}


def _certificate_count_for_student(student_id):
    if not student_id:
        return 0
    return int(Certificate.objects(student_id=student_id, is_verified=True).count())


def _serialize_leaderboard_entry(profile, user, rank, xp_override=None, student_id_override=None, certificates_count=0):
    display_name = getattr(user, "full_name", None) if user else None
    username = (display_name or getattr(user, "username", "مستخدم")) if user else "مستخدم"
    student_obj_id = None
    if profile and getattr(profile, "student_id", None):
        student_obj_id = profile.student_id.id
    elif student_id_override is not None:
        student_obj_id = student_id_override

    xp_value = int(xp_override if xp_override is not None else (getattr(profile, "xp_total", 0) or 0))
    level_value = int(getattr(profile, "level", _calculate_level(xp_value)) or _calculate_level(xp_value))
    return {
        "rank": rank,
        "username": username,
        "xp": xp_value,
        "level": level_value,
        "badges": list(getattr(profile, "badges", []) or []),
        "avatar": _avatar_text_for_user(user) if user else "U",
        "is_top_3": rank <= 3,
        "student_id": str(student_obj_id) if student_obj_id else None,
        "certificates_count": int(certificates_count or 0),
    }


def _normalize_leaderboard_scope(scope_value):
    allowed = {"all", "weekly", "monthly", "seasonal"}
    scope = (scope_value or "all").strip().lower()
    return scope if scope in allowed else "all"


def _leaderboard_page_cache_key(scope, page, per_page):
    return f"leaderboard_page_{scope}_{page}_{per_page}"


def _scope_start_datetime(scope: str):
    now = datetime.utcnow()
    if scope == "weekly":
        return now - timedelta(days=7)
    if scope == "monthly":
        return now - timedelta(days=30)
    if scope == "seasonal":
        return now - timedelta(days=90)
    return None


def _aggregate_scope_rankings(scope: str, page: int, per_page: int):
    start_dt = _scope_start_datetime(scope)
    if start_dt is None:
        return [], 0

    events_coll = XPEvent._get_collection()
    match_stage = {"created_at": {"$gte": start_dt}}

    count_pipeline = [
        {"$match": match_stage},
        {"$group": {"_id": "$student_id", "xp_total": {"$sum": "$xp"}}},
        {"$count": "total"},
    ]
    count_result = list(events_coll.aggregate(count_pipeline))
    total_users = int(count_result[0]["total"]) if count_result else 0

    rows_pipeline = [
        {"$match": match_stage},
        {"$group": {"_id": "$student_id", "xp_total": {"$sum": "$xp"}}},
        {"$sort": {"xp_total": -1, "_id": 1}},
        {"$skip": max(0, (page - 1) * per_page)},
        {"$limit": per_page},
    ]
    rows = list(events_coll.aggregate(rows_pipeline))
    return rows, total_users


def _student_scope_xp(student_id, scope: str):
    start_dt = _scope_start_datetime(scope)
    if start_dt is None:
        profile = StudentGamification.objects(student_id=student_id).first()
        return int(profile.xp_total or 0) if profile else 0

    events_coll = XPEvent._get_collection()
    pipeline = [
        {"$match": {"student_id": student_id, "created_at": {"$gte": start_dt}}},
        {"$group": {"_id": "$student_id", "xp_total": {"$sum": "$xp"}}},
    ]
    result = list(events_coll.aggregate(pipeline))
    if not result:
        return 0
    return int(result[0].get("xp_total", 0) or 0)


def _leaderboard_payload_for_user(student_id, scope, page, per_page):
    board = _build_leaderboard_page(page=page, per_page=per_page, scope=scope)
    current_rank = None
    current_xp = 0
    current_certificates_count = 0
    if student_id is not None:
        current_rank = _calculate_student_rank(student_id, scope=scope)
        current_xp = _student_scope_xp(student_id=student_id, scope=scope)
        current_certificates_count = _certificate_count_for_student(student_id)
    return {
        "ok": True,
        "leaderboard": board,
        "current_rank": current_rank,
        "current_xp": current_xp,
        "current_certificates_count": current_certificates_count,
    }


def _leaderboard_payload_signature(payload):
    board = payload.get("leaderboard", {})
    entries = board.get("entries", [])
    signature_rows = tuple(
        (
            e.get("student_id"),
            int(e.get("rank", 0)),
            int(e.get("xp", 0)),
            int(e.get("certificates_count", 0) or 0),
        )
        for e in entries
    )
    return (
        board.get("scope"),
        int(board.get("page", 1)),
        int(board.get("per_page", 20)),
        tuple(signature_rows),
        payload.get("current_rank"),
        int(payload.get("current_xp", 0) or 0),
        int(payload.get("current_certificates_count", 0) or 0),
    )


def _build_leaderboard_page(page: int, per_page: int, scope: str = "all"):
    scope = _normalize_leaderboard_scope(scope)
    page = max(1, int(page or 1))
    per_page = max(10, min(int(per_page or 20), 50))
    cache_key = _leaderboard_page_cache_key(scope, page, per_page)
    cached = cache.get(cache_key)
    if cached:
        return cached

    if scope == "all":
        total_users = StudentGamification.objects.count()
        total_pages = max(1, math.ceil(total_users / per_page)) if total_users else 1
        if page > total_pages:
            page = total_pages

        start_rank = ((page - 1) * per_page) + 1
        profiles = list(
            StudentGamification.objects
            .order_by("-xp_total", "student_id")
            .skip((page - 1) * per_page)
            .limit(per_page)
            .all()
        )

        student_ids = [p.student_id.id for p in profiles if p.student_id]
        users = User.objects(id__in=student_ids).only("id", "username", "first_name", "last_name").all() if student_ids else []
        users_by_id = {u.id: u for u in users}
        cert_counts = _certificate_counts_for_students(student_ids)

        entries = []
        for i, profile in enumerate(profiles):
            user = users_by_id.get(profile.student_id.id) if profile.student_id else None
            sid = profile.student_id.id if profile.student_id else None
            entries.append(
                _serialize_leaderboard_entry(
                    profile,
                    user,
                    start_rank + i,
                    certificates_count=cert_counts.get(sid, 0),
                )
            )
    else:
        rows, total_users = _aggregate_scope_rankings(scope=scope, page=page, per_page=per_page)
        total_pages = max(1, math.ceil(total_users / per_page)) if total_users else 1
        if page > total_pages:
            page = total_pages
            rows, total_users = _aggregate_scope_rankings(scope=scope, page=page, per_page=per_page)

        start_rank = ((page - 1) * per_page) + 1
        student_ids = [row.get("_id") for row in rows if row.get("_id")]
        users = User.objects(id__in=student_ids).only("id", "username", "first_name", "last_name").all() if student_ids else []
        users_by_id = {u.id: u for u in users}
        profiles = StudentGamification.objects(student_id__in=student_ids).only("student_id", "level", "badges").all() if student_ids else []
        profiles_by_student_id = {p.student_id.id: p for p in profiles if p.student_id}
        cert_counts = _certificate_counts_for_students(student_ids)

        entries = []
        for i, row in enumerate(rows):
            sid = row.get("_id")
            xp_value = int(row.get("xp_total", 0) or 0)
            user = users_by_id.get(sid)
            profile = profiles_by_student_id.get(sid)
            entries.append(
                _serialize_leaderboard_entry(
                    profile=profile,
                    user=user,
                    rank=start_rank + i,
                    xp_override=xp_value,
                    student_id_override=sid,
                    certificates_count=cert_counts.get(sid, 0),
                )
            )

    payload = {
        "entries": entries,
        "page": page,
        "per_page": per_page,
        "scope": scope,
        "total_users": total_users,
        "total_pages": total_pages,
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }
    cache.set(cache_key, payload, timeout=15)
    return payload


def _calculate_student_rank(student_id, scope: str = "all"):
    if not student_id:
        return None
    scope = _normalize_leaderboard_scope(scope)

    if scope == "all":
        profile = StudentGamification.objects(student_id=student_id).first()
        if not profile:
            return None

        higher_count = StudentGamification.objects(
            __raw__={
                "$or": [
                    {"xp_total": {"$gt": int(profile.xp_total or 0)}},
                    {
                        "xp_total": int(profile.xp_total or 0),
                        "student_id": {"$lt": profile.student_id.id},
                    },
                ]
            }
        ).count()
        return higher_count + 1

    current_xp = _student_scope_xp(student_id=student_id, scope=scope)
    if current_xp <= 0:
        return None

    start_dt = _scope_start_datetime(scope)
    events_coll = XPEvent._get_collection()
    pipeline = [
        {"$match": {"created_at": {"$gte": start_dt}}},
        {"$group": {"_id": "$student_id", "xp_total": {"$sum": "$xp"}}},
        {
            "$match": {
                "$or": [
                    {"xp_total": {"$gt": current_xp}},
                    {
                        "$and": [
                            {"xp_total": current_xp},
                            {"_id": {"$lt": student_id}},
                        ]
                    },
                ]
            }
        },
        {"$count": "higher"},
    ]
    higher_result = list(events_coll.aggregate(pipeline))
    higher_count = int(higher_result[0]["higher"]) if higher_result else 0
    return higher_count + 1


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


def _duel_player_slot(duel, user_id):
    if duel.challenger_id and duel.challenger_id.id == user_id:
        return "challenger"
    if duel.opponent_id and duel.opponent_id.id == user_id:
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
        "duel_loss": "XP نتيجة الخسارة",
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
                "label": labels.get(getattr(row, "event_type", ""), getattr(row, "event_type", "XP")),
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


def get_unlocked_lessons(student_id: int):
    """Collect all lessons the student can access across all sections.
    Optimized to avoid N+1 queries by bulk loading data.
    """
    # Bulk load all sections
    sections = list(Section.objects().all())
    if not sections:
        return []
    
    # Bulk load all lessons for these sections
    section_ids = [s.id for s in sections]
    all_lessons = list(Lesson.objects(section_id__in=section_ids).all())
    
    # Group lessons by section for faster access
    lessons_by_section = {}
    for lesson in all_lessons:
        section_id = lesson.section_id.id
        if section_id not in lessons_by_section:
            lessons_by_section[section_id] = []
        lessons_by_section[section_id].append(lesson)
    
    # Bulk load activations
    lesson_ids = [l.id for l in all_lessons]
    lesson_activations = set(
        la.lesson_id for la in LessonActivation.objects(
            lesson_id__in=lesson_ids, 
            student_id=student_id, 
            active=True
        ).all()
    )
    
    unlocked = []
    for section in sections:
        section_lessons = lessons_by_section.get(section.id, [])
        if not section_lessons:
            continue
        
        access = AccessContext(section, student_id)
        for lesson in section_lessons:
            if access.lesson_open(lesson):
                unlocked.append(lesson)
    
    return unlocked


@student_bp.before_request
def student_duel_maintenance():
    if not current_user.is_authenticated:
        return None
    if (current_user.role or "").lower() != "student":
        return None

    try:
        _duel_maintenance_tick_for_student(current_user.id)
    except Exception:
        # Maintenance should never block user flows.
        return None
    return None

@student_bp.route("/subjects")
@login_required
@cache.cached(timeout=60, key_prefix=lambda: f"subjects_{current_user.id}_{current_user.role}")
def subjects():
    subs = list(Subject.objects().order_by('created_at').all())
    
    # Bulk load sections to avoid N+1 in template
    if subs:
        subject_ids = [s.id for s in subs]
        sections = list(Section.objects(subject_id__in=subject_ids).all())
        
        # Count sections per subject
        sections_count = {}
        for section in sections:
            subject_id = section.subject_id.id
            sections_count[subject_id] = sections_count.get(subject_id, 0) + 1
        
        # Attach count to subjects
        for subject in subs:
            subject._sections_count = sections_count.get(subject.id, 0)
    
    # Get activation status for each subject if student
    subject_activations = {}
    if current_user.role == "student":
        activations = SubjectActivation.objects(
            student_id=current_user.id, 
            active=True
        ).all()
        subject_activations = {sa.subject_id: sa for sa in activations}
    
    return render_template("student/subjects.html", subjects=subs, subject_activations=subject_activations)

# Redirect '/student' to subjects for Up navigation
@student_bp.route("/student")
@login_required
def student_root():
    return redirect(url_for("student.subjects"))

@student_bp.route("/subjects/<subject_id>")
@login_required
@cache.cached(
    timeout=60,
    key_prefix=lambda: f"subject_detail_{request.view_args.get('subject_id', '')}_{current_user.id}",
)
def subject_detail(subject_id):
    subject = Subject.objects(id=subject_id).first()
    if not subject:
        return "404", 404
    
    # Bulk load sections for this subject
    sections = list(Section.objects(subject_id=subject.id).order_by('created_at').all())
    
    # Bulk load lesson counts to avoid N+1 in template
    if sections:
        section_ids = [s.id for s in sections]
        lessons = list(Lesson.objects(section_id__in=section_ids).all())
        lesson_counts = {}
        for lesson in lessons:
            section_id = lesson.section_id.id
            lesson_counts[section_id] = lesson_counts.get(section_id, 0) + 1
    else:
        lesson_counts = {}
    
    # Check if subject is activated for this student
    subject_activation = None
    sections_data = []
    subject_requires_code = getattr(subject, "requires_code", False)
    subject_open = True
    
    if current_user.role == "student":
        subject_activation = SubjectActivation.objects(
            subject_id=subject.id,
            student_id=current_user.id,
            active=True,
        ).first()
        subject_open = bool(subject_activation) or not subject_requires_code
        
        # Bulk load section activations to avoid N+1
        section_ids = [s.id for s in sections]
        section_activations = {
            sa.section_id: sa for sa in SectionActivation.objects(
                section_id__in=section_ids,
                student_id=current_user.id,
                active=True
            ).all()
        } if section_ids else {}
        
        for section in sections:
            # Create AccessContext with preloaded activation data
            access = AccessContext(section, current_user.id)
            sections_data.append({
                "section": section,
                "is_open": access.section_open,
                "requires_code": access.section_requires_code,
                "lesson_count": lesson_counts.get(section.id, 0)
            })
    else:
        for section in sections:
            sections_data.append({
                "section": section,
                "is_open": True,
                "requires_code": False,
                "lesson_count": lesson_counts.get(section.id, 0)
            })

    return render_template(
        "student/subject_detail.html",
        subject=subject,
        subject_activation=subject_activation,
        subject_requires_code=subject_requires_code,
        subject_open=subject_open,
        sections_data=sections_data,
    )


def _course_set_open_for_student(course_set, student_id):
    """Check if a student can access a course set based on subject/section/lesson access."""
    if not course_set:
        return False
    if not course_set.is_active:
        return False

    subject_requires_code = bool(getattr(course_set.subject_id, "requires_code", False)) if course_set.subject_id else False
    subject_active = bool(
        SubjectActivation.objects(subject_id=course_set.subject_id.id, student_id=student_id, active=True).first()
    ) if course_set.subject_id else False
    if (not subject_active) and subject_requires_code:
        return False

    if course_set.lesson_id:
        lesson = course_set.lesson_id
        if not lesson or not lesson.section_id:
            return False
        access = AccessContext(lesson.section_id, student_id)
        return access.lesson_open(lesson)

    if course_set.section_id:
        section = course_set.section_id
        access = AccessContext(section, student_id)
        return access.section_open

    return True


@student_bp.route("/subjects/<subject_id>/courses")
@login_required
def subject_courses(subject_id):
    subject = Subject.objects(id=subject_id).first()
    if not subject:
        return "404", 404

    all_sets = list(CourseSet.objects(subject_id=subject.id).order_by("created_at").all())

    sets_data = []
    for item in all_sets:
        if current_user.role == "student":
            is_open = _course_set_open_for_student(item, current_user.id)
            if not is_open:
                continue
        else:
            is_open = True

        q_count = CourseQuestion.objects(course_set_id=item.id).count()
        sets_data.append(
            {
                "course_set": item,
                "question_count": q_count,
                "is_open": is_open,
            }
        )

    return render_template(
        "student/course_sets.html",
        subject=subject,
        sets_data=sets_data,
    )


@student_bp.route("/courses/<course_set_id>/take", methods=["GET"])
@login_required
def course_take(course_set_id):
    course_set = CourseSet.objects(id=course_set_id).first()
    if not course_set:
        return "404", 404

    if current_user.role == "student":
        if not _course_set_open_for_student(course_set, current_user.id):
            flash("غير مسموح.", "error")
            return redirect(url_for("student.subject_courses", subject_id=course_set.subject_id.id))

    questions = list(CourseQuestion.objects(course_set_id=course_set.id).order_by("created_at").all())
    if not questions:
        flash("لا توجد أسئلة في هذه الدورة حالياً.", "warning")
        return redirect(url_for("student.subject_courses", subject_id=course_set.subject_id.id))

    return render_template("student/course_take.html", course_set=course_set, questions=questions)


@student_bp.route("/courses/<course_set_id>/submit", methods=["POST"])
@login_required
def course_submit(course_set_id):
    course_set = CourseSet.objects(id=course_set_id).first()
    if not course_set:
        return "404", 404

    if current_user.role == "student":
        if not _course_set_open_for_student(course_set, current_user.id):
            flash("غير مسموح.", "error")
            return redirect(url_for("student.subject_courses", subject_id=course_set.subject_id.id))

    questions = list(CourseQuestion.objects(course_set_id=course_set.id).order_by("created_at").all())
    if not questions:
        flash("لا توجد أسئلة في هذه الدورة.", "error")
        return redirect(url_for("student.subject_courses", subject_id=course_set.subject_id.id))

    score = 0
    total = len(questions)

    attempt = CourseAttempt(
        course_set_id=course_set.id,
        student_id=current_user.id,
        status="submitted",
        total=total,
        score=0,
        xp_earned=0,
    )
    attempt.save()

    for q in questions:
        raw = (request.form.get(f"question_{q.id}") or "").strip().lower()
        selected_value = raw == "true"
        # Self-evaluation mode: student marks if their own solution was correct.
        is_correct = bool(selected_value)
        if is_correct:
            score += 1

        CourseAnswer(
            attempt_id=attempt.id,
            question_id=q.id,
            selected_value=selected_value,
            is_correct=is_correct,
        ).save()

    attempt.score = score
    attempt.total = total
    xp_per_question = max(1, int(getattr(course_set, "xp_per_question", 1) or 1))
    earned_xp = score * xp_per_question
    if total > 0 and score == total:
        earned_xp *= 2
    attempt.xp_earned = earned_xp
    attempt.save()

    _award_flat_xp_once(
        student_id=current_user.id,
        event_type="interactive_course_submit",
        source_id=str(attempt.id),
        amount=earned_xp,
    )

    return redirect(url_for("student.course_result", attempt_id=attempt.id))


@student_bp.route("/courses/attempts/<attempt_id>/result", methods=["GET"])
@login_required
def course_result(attempt_id):
    attempt = CourseAttempt.objects(id=attempt_id).first()
    if not attempt:
        return "404", 404

    if str(attempt.student_id.id) != str(current_user.id) and (current_user.role or "").lower() not in {"teacher", "admin", "question_editor"}:
        flash("غير مسموح.", "error")
        return redirect(url_for("student.subjects"))

    course_set = attempt.course_set_id
    answers = list(CourseAnswer.objects(attempt_id=attempt.id).all())
    answer_by_qid = {str(a.question_id.id): a for a in answers if a.question_id}
    questions = list(CourseQuestion.objects(course_set_id=course_set.id).order_by("created_at").all()) if course_set else []

    review = []
    for q in questions:
        ans = answer_by_qid.get(str(q.id))
        review.append(
            {
                "question": q,
                "answer": ans,
            }
        )
    gamification = StudentGamification.objects(student_id=attempt.student_id.id).first()

    return render_template("student/course_result.html", attempt=attempt, course_set=course_set, review=review, gamification=gamification)

@student_bp.route("/sections/<section_id>")
@login_required
@cache.cached(
    timeout=60,
    key_prefix=lambda: f"section_detail_{request.view_args.get('section_id', '')}_{current_user.id}",
)
def section_detail(section_id):
    section = Section.objects(id=section_id).first()
    if not section:
        return "404", 404
    
    # Bulk load lessons and tests for this section
    lessons = list(Lesson.objects(section_id=section.id).order_by('created_at').all())
    tests = list(Test.objects(section_id=section.id, lesson_id=None).order_by('created_at').all())
    
    # Bulk load question counts for tests
    if tests:
        test_ids = [t.id for t in tests]
        questions = list(Question.objects(test_id__in=test_ids).only('test_id').all())
        question_counts = {}
        for q in questions:
            test_id = q.test_id.id
            question_counts[test_id] = question_counts.get(test_id, 0) + 1
        
        # Attach counts to tests
        for test in tests:
            test._question_count = question_counts.get(test.id, 0)
    
    if current_user.role == "student":
        access = AccessContext(section, current_user.id)
        completed_lesson_ids = set(
            lc.lesson_id.id
            for lc in LessonCompletion.objects(
                lesson_id__in=[l.id for l in lessons],
                student_id=current_user.id,
            ).only("lesson_id").all()
            if lc.lesson_id
        )
        lessons_data = [
            {
                "lesson": lesson,
                "is_open": access.lesson_open(lesson),
                "is_completed": lesson.id in completed_lesson_ids,
            }
            for lesson in lessons
        ]
        tests_data = [
            {"test": test, "is_open": access.test_open(test)}
            for test in tests
        ]
        return render_template(
            "student/section_detail.html",
            section=section,
            subject_requires_code=access.subject_requires_code,
            subject_open=access.subject_open,
            section_active=access.section_active,
            section_requires_code=access.section_requires_code,
            section_open=access.section_open,
            lessons_data=lessons_data,
            tests_data=tests_data,
        )
    # Teachers/admins can view everything unlocked
    lessons_data = [{"lesson": l, "is_open": True, "is_completed": False} for l in lessons]
    tests_data = [{"test": t, "is_open": True} for t in tests]
    return render_template(
        "student/section_detail.html",
        section=section,
        subject_requires_code=False,
        subject_open=True,
        section_active=True,
        section_requires_code=False,
        section_open=True,
        lessons_data=lessons_data,
        tests_data=tests_data,
    )

@student_bp.route("/lessons/<lesson_id>")
@login_required
def lesson_detail(lesson_id):
    lesson = Lesson.objects(id=lesson_id).first()
    if not lesson:
        return "404", 404
    section = lesson.section
    if current_user.role == "student":
        access = AccessContext(section, current_user.id)
        if not access.lesson_open(lesson):
            if access.subject_requires_code and not access.section_active:
                flash("قم بتفعيل الدرس للوصول إليه.", "warning")
                return redirect(url_for("student.activate_lesson", lesson_id=lesson.id))
            flash("قم بتفعيل الدرس للوصول إليه.", "warning")
            return redirect(url_for("student.activate_lesson", lesson_id=lesson.id))
    def infer_resource_type(resource):
        if resource.resource_type:
            return resource.resource_type.lower()
        url = (resource.url or "").lower()
        if "youtube.com" in url or "youtu.be" in url:
            return "video"
        if url.endswith(".json"):
            return "flashcards"
        if url.endswith(".html") or url.endswith(".htm"):
            return "mindmap"
        if url.endswith(".mp3") or url.endswith(".wav") or "audio" in url:
            return "audio"
        if "drive.google.com" in url or url.endswith(".pdf"):
            return "pdf"
        return "link"

    def extract_drive_file_id(url):
        """Extract Google Drive file ID from various Google Drive URL formats."""
        if "/file/d/" in url:
            return url.split("/file/d/")[-1].split("/")[0]
        if "id=" in url:
            return url.split("id=")[-1].split("&")[0]
        return None

    def normalize_drive_url(url):
        """Convert Google Drive sharing links to direct download links."""
        lowered = url.lower()
        if "drive.google.com" in lowered:
            file_id = extract_drive_file_id(url)
            if file_id:
                # Return direct download link for JSON files, preview for PDFs
                if url.lower().endswith(".json"):
                    return f"https://drive.google.com/uc?export=download&id={file_id}"
                else:
                    # For PDFs and other files, use preview
                    return f"https://drive.google.com/file/d/{file_id}/preview"
        return url

    def to_embed_url(resource):
        url = resource.url or ""
        lowered = url.lower()
        if "youtube.com" in lowered or "youtu.be" in lowered:
            if "youtu.be/" in url:
                video_id = url.split("youtu.be/")[-1].split("?")[0]
            else:
                if "v=" in url:
                    video_id = url.split("v=")[-1].split("&")[0]
                else:
                    video_id = ""
            return f"https://www.youtube.com/embed/{video_id}" if video_id else url
        if "drive.google.com" in lowered:
            return normalize_drive_url(url)
        return url

    resources = []
    for res in lesson.resources:
        res_type = infer_resource_type(res)
        resources.append({
            "id": res.id,
            "label": res.label,
            "url": res.url,
            "resource_type": res_type,
            "embed_url": to_embed_url(res),
        })

    # Bulk load tests for this lesson
    tests = list(Test.objects(lesson_id=lesson.id).order_by('created_at').all())
    
    # Bulk load question counts
    if tests:
        test_ids = [t.id for t in tests]
        questions = list(Question.objects(test_id__in=test_ids).only('test_id').all())
        question_counts = {}
        for q in questions:
            test_id = q.test_id.id
            question_counts[test_id] = question_counts.get(test_id, 0) + 1
        
        for test in tests:
            test._question_count = question_counts.get(test.id, 0)
    
    is_completed = False
    lesson_completion_xp = max(0, int(getattr(lesson, "xp_reward", 10) or 10))

    if current_user.role == "student":
        access = AccessContext(section, current_user.id)
        tests_data = [
            {"test": test, "is_open": access.test_open(test)}
            for test in tests
        ]
        is_completed = bool(
            LessonCompletion.objects(
                lesson_id=lesson.id,
                student_id=current_user.id,
            ).first()
        )
    else:
        tests_data = [{"test": t, "is_open": True} for t in tests]

    certificate = None
    if current_user.role == "student":
        certificate = Certificate.objects(student_id=current_user.id, lesson_id=lesson.id).first()

    return render_template(
        "student/lesson_detail.html",
        lesson=lesson,
        section=section,
        resources=resources,
        tests_data=tests_data,
        is_completed=is_completed,
        lesson_completion_xp=lesson_completion_xp,
        certificate=certificate,
    )


@student_bp.route("/lessons/<lesson_id>/discussion", methods=["GET", "POST"])
@login_required
def lesson_discussion(lesson_id):
    lesson = Lesson.objects(id=lesson_id).first()
    if not lesson:
        return "404", 404

    if current_user.role == "student":
        access = AccessContext(lesson.section, current_user.id)
        if not access.lesson_open(lesson):
            flash("قم بتفعيل الدرس للوصول إلى المناقشة.", "warning")
            return redirect(url_for("student.activate_lesson", lesson_id=lesson.id))

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()

        if action == "ask_question":
            title = (request.form.get("title") or "").strip()
            body = (request.form.get("body") or "").strip()
            if not title or not body:
                flash("عنوان السؤال ومحتواه مطلوبان.", "error")
                return redirect(url_for("student.lesson_discussion", lesson_id=lesson.id))
            DiscussionQuestion(
                lesson_id=lesson.id,
                author_id=current_user.id,
                title=title,
                body=body,
                is_resolved=False,
            ).save()
            flash("تم إضافة السؤال بنجاح.", "success")
            return redirect(url_for("student.lesson_discussion", lesson_id=lesson.id))

        if action == "add_answer":
            question_id = (request.form.get("question_id") or "").strip()
            body = (request.form.get("body") or "").strip()
            question = DiscussionQuestion.objects(id=question_id, lesson_id=lesson.id).first() if ObjectId.is_valid(question_id) else None
            if not question or not body:
                flash("تعذر إضافة الرد.", "error")
                return redirect(url_for("student.lesson_discussion", lesson_id=lesson.id))

            DiscussionAnswer(
                question_id=question.id,
                author_id=current_user.id,
                body=body,
            ).save()
            flash("تمت إضافة الرد.", "success")
            return redirect(url_for("student.lesson_discussion", lesson_id=lesson.id))

        if action == "toggle_resolved":
            question_id = (request.form.get("question_id") or "").strip()
            question = DiscussionQuestion.objects(id=question_id, lesson_id=lesson.id).first() if ObjectId.is_valid(question_id) else None
            if not question:
                flash("السؤال غير موجود.", "error")
                return redirect(url_for("student.lesson_discussion", lesson_id=lesson.id))

            is_owner = str(question.author_id.id) == str(current_user.id) if question.author_id else False
            is_staff = (current_user.role or "").lower() in {"teacher", "admin"}
            if not (is_owner or is_staff):
                flash("غير مسموح لك بتغيير حالة السؤال.", "error")
                return redirect(url_for("student.lesson_discussion", lesson_id=lesson.id))

            question.is_resolved = not bool(question.is_resolved)
            question.save()
            flash("تم تحديث حالة السؤال.", "success")
            return redirect(url_for("student.lesson_discussion", lesson_id=lesson.id))

    questions = list(DiscussionQuestion.objects(lesson_id=lesson.id).order_by("-is_pinned", "is_resolved", "-created_at").all())
    answers = list(DiscussionAnswer.objects(question_id__in=[q.id for q in questions]).order_by("created_at").all()) if questions else []

    answers_by_question = {}
    author_ids = set()
    for q in questions:
        if q.author_id:
            author_ids.add(q.author_id.id)
    for a in answers:
        qid = a.question_id.id if a.question_id else None
        if qid is None:
            continue
        answers_by_question.setdefault(qid, []).append(a)
        if a.author_id:
            author_ids.add(a.author_id.id)

    users_by_id = {u.id: u for u in User.objects(id__in=list(author_ids)).all()} if author_ids else {}

    return render_template(
        "student/lesson_discussion.html",
        lesson=lesson,
        section=lesson.section,
        questions=questions,
        answers_by_question=answers_by_question,
        users_by_id=users_by_id,
    )


@student_bp.route("/discussions/pinned", methods=["GET"])
@login_required
def pinned_discussions():
    all_pinned = list(DiscussionQuestion.objects(is_pinned=True).order_by("-created_at").all())

    visible_questions = []
    access_cache = {}
    for question in all_pinned:
        lesson = question.lesson_id
        if not lesson:
            continue

        if (current_user.role or "").lower() in {"teacher", "admin"}:
            visible_questions.append(question)
            continue

        section = lesson.section
        cache_key = str(section.id) if section else ""
        if cache_key not in access_cache and section:
            access_cache[cache_key] = AccessContext(section, current_user.id)
        context = access_cache.get(cache_key)
        if context and context.lesson_open(lesson):
            visible_questions.append(question)

    answers = (
        list(
            DiscussionAnswer.objects(
                question_id__in=[q.id for q in visible_questions]
            )
            .order_by("created_at")
            .all()
        )
        if visible_questions
        else []
    )

    answers_by_question = {}
    lesson_ids = set()
    user_ids = set()

    for question in visible_questions:
        if question.lesson_id:
            lesson_ids.add(question.lesson_id.id)
        if question.author_id:
            user_ids.add(question.author_id.id)

    for answer in answers:
        qid = answer.question_id.id if answer.question_id else None
        if qid is None:
            continue
        answers_by_question.setdefault(qid, []).append(answer)
        if answer.author_id:
            user_ids.add(answer.author_id.id)

    lessons = list(Lesson.objects(id__in=list(lesson_ids)).all()) if lesson_ids else []
    lessons_by_id = {lesson.id: lesson for lesson in lessons}
    users_by_id = {user.id: user for user in User.objects(id__in=list(user_ids)).all()} if user_ids else {}

    return render_template(
        "student/pinned_discussions.html",
        questions=visible_questions,
        answers_by_question=answers_by_question,
        lessons_by_id=lessons_by_id,
        users_by_id=users_by_id,
    )


@student_bp.route("/certificates", methods=["GET"])
@login_required
def certificates():
    if current_user.role != "student":
        flash("هذه الصفحة للطلاب فقط.", "warning")
        return redirect(url_for("index"))

    certificates = list(Certificate.objects(student_id=current_user.id).order_by("-issued_at").all())
    lesson_ids = [c.lesson_id.id for c in certificates if c.lesson_id]
    lessons = Lesson.objects(id__in=lesson_ids).all() if lesson_ids else []
    lessons_by_id = {l.id: l for l in lessons}

    return render_template(
        "student/certificates.html",
        certificates=certificates,
        lessons_by_id=lessons_by_id,
    )


@student_bp.route("/certificates/<certificate_id>/download", methods=["GET"])
@login_required
def download_certificate(certificate_id):
    if current_user.role != "student":
        flash("هذه الصفحة للطلاب فقط.", "warning")
        return redirect(url_for("index"))

    cert = Certificate.objects(id=certificate_id, student_id=current_user.id).first()
    if not cert:
        flash("الشهادة غير موجودة.", "error")
        return redirect(url_for("student.certificates"))

    cert_url = (getattr(cert, "certificate_url", "") or "").strip()
    if not cert_url:
        flash("لم يتم رفع رابط الشهادة بعد. تواصل مع المعلم.", "warning")
        return redirect(url_for("student.certificates"))
    return redirect(cert_url)


@student_bp.route("/lessons/<lesson_id>/complete", methods=["POST"])
@login_required
def complete_lesson(lesson_id):
    if current_user.role != "student":
        flash("هذه الخاصية متاحة للطلاب فقط.", "warning")
        return redirect(url_for("student.lesson_detail", lesson_id=lesson_id))

    lesson = Lesson.objects(id=lesson_id).first()
    if not lesson:
        return "404", 404

    access = AccessContext(lesson.section, current_user.id)
    if not access.lesson_open(lesson):
        flash("قم بتفعيل الدرس أولاً.", "warning")
        return redirect(url_for("student.activate_lesson", lesson_id=lesson.id))

    existing_completion = LessonCompletion.objects(
        lesson_id=lesson.id,
        student_id=current_user.id,
    ).first()

    if existing_completion:
        flash("تم إنهاء هذا الدرس مسبقاً.", "info")
        return redirect(url_for("student.lesson_detail", lesson_id=lesson.id))

    LessonCompletion(
        lesson_id=lesson.id,
        student_id=current_user.id,
    ).save()

    cache.delete(f"section_detail_{lesson.section.id}_{current_user.id}")

    earned_xp, _ = _award_flat_xp_once(
        student_id=current_user.id,
        event_type="lesson_complete",
        source_id=str(lesson.id),
        amount=max(0, int(getattr(lesson, "xp_reward", 10) or 10)),
    )

    flash(f"أحسنت! تم إنهاء الدرس وربحت {earned_xp} XP.", "success")
    return redirect(url_for("student.lesson_detail", lesson_id=lesson.id))


@student_bp.route("/leaderboard")
@login_required
def leaderboard():
    page = _to_int(request.args.get("page"), 1)
    per_page = _to_int(request.args.get("per_page"), 20)
    scope = _normalize_leaderboard_scope(request.args.get("scope"))
    board = _build_leaderboard_page(page=page, per_page=per_page, scope=scope)

    current_rank = None
    current_profile = None
    current_scope_xp = 0
    current_certificates_count = 0
    if current_user.role == "student":
        current_rank = _calculate_student_rank(current_user.id, scope=scope)
        current_profile = StudentGamification.objects(student_id=current_user.id).first()
        current_scope_xp = _student_scope_xp(student_id=current_user.id, scope=scope)
        current_certificates_count = _certificate_count_for_student(current_user.id)

    return render_template(
        "student/leaderboard.html",
        leaderboard=board,
        scope=scope,
        current_rank=current_rank,
        current_profile=current_profile,
        current_scope_xp=current_scope_xp,
        current_certificates_count=current_certificates_count,
    )


@student_bp.route("/leaderboard/students/<student_id>/certificates", methods=["GET"])
@login_required
def leaderboard_student_certificates(student_id):
    student = User.objects(id=student_id, role="student").first() if ObjectId.is_valid(student_id) else None
    if not student:
        return "404", 404

    certificates = list(Certificate.objects(student_id=student.id, is_verified=True).order_by("-issued_at").all())
    lesson_ids = [c.lesson_id.id for c in certificates if c.lesson_id]
    lessons = list(Lesson.objects(id__in=lesson_ids).all()) if lesson_ids else []
    lessons_by_id = {l.id: l for l in lessons}

    return render_template(
        "student/leaderboard_student_certificates.html",
        student=student,
        certificates=certificates,
        lessons_by_id=lessons_by_id,
    )


@student_bp.route("/leaderboard/data")
@login_required
def leaderboard_data():
    page = _to_int(request.args.get("page"), 1)
    per_page = _to_int(request.args.get("per_page"), 20)
    scope = _normalize_leaderboard_scope(request.args.get("scope"))
    student_id = current_user.id if current_user.role == "student" else None
    return jsonify(_leaderboard_payload_for_user(student_id, scope, page, per_page))


@student_bp.route("/leaderboard/stream")
@login_required
def leaderboard_stream():
    page = _to_int(request.args.get("page"), 1)
    per_page = _to_int(request.args.get("per_page"), 20)
    scope = _normalize_leaderboard_scope(request.args.get("scope"))
    student_id = current_user.id if current_user.role == "student" else None

    def event_stream():
        last_sig = None
        yield ": leaderboard-stream-open\n\n"
        while True:
            payload = _leaderboard_payload_for_user(student_id, scope, page, per_page)
            sig = _leaderboard_payload_signature(payload)
            if sig != last_sig:
                last_sig = sig
                data = json.dumps(payload, ensure_ascii=False)
                yield f"event: leaderboard\n"
                yield f"data: {data}\n\n"
            else:
                yield ": keepalive\n\n"
            time.sleep(5)

    response = Response(event_stream(), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    return response


@student_bp.route("/duels", methods=["GET", "POST"])
@login_required
def duels_home():
    if (current_user.role or "").lower() != "student":
        flash("صفحة التحديات متاحة للطلاب فقط.", "error")
        return redirect(url_for("index"))

    created_duel = None
    share_link = None
    whatsapp_share = None
    telegram_share = None

    unlocked_lessons = get_unlocked_lessons(current_user.id)
    section_ids = list({l.section_id.id for l in unlocked_lessons if l.section_id})
    subject_ids = []
    if section_ids:
        sections = list(Section.objects(id__in=section_ids).only("id", "subject_id", "title").all())
        subject_ids = list({s.subject_id.id for s in sections if s.subject_id})
    else:
        sections = []
    subjects = list(Subject.objects(id__in=subject_ids).only("id", "name").all()) if subject_ids else []

    if request.method == "POST":
        opponent_username = (request.form.get("opponent_username") or "").strip()
        scope_type = (request.form.get("scope_type") or "").strip().lower()
        scope_id = (request.form.get("scope_id") or "").strip()

        now = datetime.utcnow()
        pending_count = Duel.objects(challenger_id=current_user.id, status="pending").count()
        latest_any = (
            Duel.objects(challenger_id=current_user.id)
            .order_by("-created_at")
            .only("created_at")
            .first()
        )

        opponent = User.objects(username=opponent_username, role="student").first() if opponent_username else None
        if not opponent:
            flash("اسم المستخدم غير موجود.", "error")
            return redirect(url_for("student.duels_home"))
        if opponent.id == current_user.id:
            flash("لا يمكنك تحدي نفسك.", "error")
            return redirect(url_for("student.duels_home"))

        latest_same_opponent = (
            Duel.objects(challenger_id=current_user.id, opponent_id=opponent.id)
            .order_by("-created_at")
            .only("created_at")
            .first()
        )

        throttle = _duel_invite_throttle_decision(
            now=now,
            pending_count=int(pending_count or 0),
            latest_any_created_at=(latest_any.created_at if latest_any else None),
            latest_same_created_at=(latest_same_opponent.created_at if latest_same_opponent else None),
        )
        if not throttle.get("allowed"):
            reason = throttle.get("reason")
            remaining = throttle.get("remaining")
            if reason == "pending_limit":
                flash("لديك عدد كبير من الدعوات المعلقة. انتظر الرد أو الإلغاء أولاً.", "warning")
            elif reason == "global_cooldown":
                flash(f"انتظر {int(remaining or 0)} ثانية قبل إرسال دعوة جديدة.", "warning")
            elif reason == "same_opponent_cooldown":
                flash(f"يمكنك تحدي نفس الخصم بعد {int(remaining or 0)} ثانية.", "warning")
            else:
                flash("تعذر إنشاء الدعوة الآن. حاول لاحقاً.", "warning")
            return redirect(url_for("student.duels_home"))

        pair_lock_remaining = _duel_pair_recent_lock_remaining(current_user.id, opponent.id, now=now)
        if pair_lock_remaining > 0:
            flash(f"هذا الثنائي في فترة تهدئة. حاول بعد {pair_lock_remaining} ثانية.", "warning")
            return redirect(url_for("student.duels_home"))

        norm_scope_type, norm_scope_id, scope_title = _duel_get_scope_info(scope_type, scope_id)
        if not norm_scope_type:
            flash("نطاق التحدي غير صالح.", "error")
            return redirect(url_for("student.duels_home"))

        question_ids = _duel_pick_questions(norm_scope_type, norm_scope_id, question_count=15)
        if len(question_ids) < 15:
            flash("يجب توفر 15 سؤالاً على الأقل في هذا النطاق.", "error")
            return redirect(url_for("student.duels_home"))

        challenger_profile = _get_or_create_gamification_profile(current_user.id)
        opponent_profile = _get_or_create_gamification_profile(opponent.id)
        if int(challenger_profile.xp_total or 0) < 20 or int(opponent_profile.xp_total or 0) < 20:
            flash("يلزم أن يمتلك كل لاعب 20 XP على الأقل لبدء التحدي.", "error")
            return redirect(url_for("student.duels_home"))

        active_existing = Duel.objects(
            __raw__={
                "$or": [
                    {"challenger_id": current_user.id, "opponent_id": opponent.id},
                    {"challenger_id": opponent.id, "opponent_id": current_user.id},
                ],
                "status": {"$in": ["pending", "accepted_waiting", "live"]},
            }
        ).first()
        if active_existing:
            flash("يوجد تحدي نشط بالفعل بينكما.", "warning")
            return redirect(url_for("student.duel_play", duel_id=str(active_existing.id)))

        token = _duel_generate_token()
        while Duel.objects(invite_token=token).first():
            token = _duel_generate_token()

        created_duel = Duel(
            challenger_id=current_user.id,
            opponent_id=opponent.id,
            opponent_username_snapshot=opponent.username,
            scope_type=norm_scope_type,
            scope_id=norm_scope_id,
            scope_title=scope_title,
            invite_token=token,
            question_ids_json=json.dumps(question_ids, ensure_ascii=False),
            question_count=15,
            timer_seconds=540,
            entry_fee_xp=20,
            expires_at=datetime.utcnow() + timedelta(minutes=15),
        )
        created_duel.save()

        share_link = url_for("student.duel_invite", token=token, _external=True)
        share_message = f"تحدي ودي على EduPath: {share_link}"
        whatsapp_share = f"https://wa.me/?text={quote(share_message)}"
        telegram_share = f"https://t.me/share/url?url={quote(share_link)}&text={quote('انضم للتحدي الودي على EduPath')}"
        flash("تم إنشاء دعوة التحدي بنجاح.", "success")

    incoming = list(Duel.objects(opponent_id=current_user.id).order_by("-created_at").limit(25).all())
    outgoing = list(Duel.objects(challenger_id=current_user.id).order_by("-created_at").limit(25).all())

    for duel in incoming + outgoing:
        _duel_expire_if_needed(duel)

    stats_top = list(DuelStats.objects().order_by("-wins", "-current_win_streak", "student_id").limit(10).all())
    top_ids = [s.student_id.id for s in stats_top if s.student_id]
    top_users = User.objects(id__in=top_ids).only("id", "username", "first_name", "last_name").all() if top_ids else []
    top_users_by_id = {u.id: u for u in top_users}

    scope_choices = {
        "lessons": [{"id": str(l.id), "label": l.title} for l in unlocked_lessons],
        "sections": [{"id": str(s.id), "label": s.title} for s in sections],
        "subjects": [{"id": str(s.id), "label": s.name} for s in subjects],
    }

    return render_template(
        "student/duels.html",
        incoming=incoming,
        outgoing=outgoing,
        scope_choices=scope_choices,
        created_duel=created_duel,
        share_link=share_link,
        whatsapp_share=whatsapp_share,
        telegram_share=telegram_share,
        top_stats=stats_top,
        top_users_by_id=top_users_by_id,
        users_by_id=top_users_by_id,
    )


@student_bp.route("/duels/pending-popup", methods=["GET"])
@login_required
def duels_pending_popup():
    if (current_user.role or "").lower() != "student":
        return jsonify({"ok": True, "invites": []})

    rows = list(Duel.objects(opponent_id=current_user.id, status="pending").order_by("-created_at").limit(3).all())
    payload = []
    for duel in rows:
        if _duel_expire_if_needed(duel):
            continue
        payload.append(
            {
                "id": str(duel.id),
                "token": duel.invite_token,
                "from": (duel.challenger_id.full_name if duel.challenger_id else "لاعب"),
                "scope": duel.scope_title,
                "expires_at": duel.expires_at.isoformat() + "Z" if duel.expires_at else None,
                "url": url_for("student.duel_invite", token=duel.invite_token),
            }
        )
    return jsonify({"ok": True, "invites": payload})


@student_bp.route("/duels/invite/<token>", methods=["GET"])
@login_required
def duel_invite(token):
    duel = Duel.objects(invite_token=token).first()
    if not duel:
        return "404", 404

    slot = _duel_player_slot(duel, current_user.id)
    if not slot:
        flash("هذه الدعوة ليست موجهة لحسابك.", "error")
        return redirect(url_for("student.duels_home"))

    _duel_expire_if_needed(duel)
    challenger_name = duel.challenger_id.full_name if duel.challenger_id else "لاعب"
    return render_template("student/duel_invite.html", duel=duel, slot=slot, challenger_name=challenger_name)


@student_bp.route("/duels/<duel_id>/respond", methods=["POST"])
@login_required
def duel_respond(duel_id):
    duel = Duel.objects(id=duel_id).first() if ObjectId.is_valid(duel_id) else None
    if not duel:
        return "404", 404

    slot = _duel_player_slot(duel, current_user.id)
    if slot != "opponent":
        flash("فقط اللاعب المستلم يمكنه الرد على الدعوة.", "error")
        return redirect(url_for("student.duels_home"))

    if _duel_expire_if_needed(duel):
        flash("انتهت صلاحية الدعوة.", "warning")
        return redirect(url_for("student.duels_home"))

    action = (request.form.get("action") or "").strip().lower()
    if duel.status != "pending":
        flash("لا يمكن الرد على هذه الدعوة الآن.", "warning")
        return redirect(url_for("student.duel_play", duel_id=str(duel.id)))

    if action == "decline":
        duel.status = "declined"
        duel.ended_at = datetime.utcnow()
        duel.save()
        flash("تم رفض الدعوة بدون أي خصم XP.", "info")
        return redirect(url_for("student.duels_home"))

    if action != "accept":
        flash("إجراء غير صالح.", "error")
        return redirect(url_for("student.duel_invite", token=duel.invite_token))

    challenger_profile = _get_or_create_gamification_profile(duel.challenger_id.id)
    opponent_profile = _get_or_create_gamification_profile(duel.opponent_id.id)
    fee = int(duel.entry_fee_xp or 20)
    if int(challenger_profile.xp_total or 0) < fee or int(opponent_profile.xp_total or 0) < fee:
        flash("لا يمكن قبول الدعوة: أحد اللاعبين لا يملك XP كافٍ.", "error")
        return redirect(url_for("student.duels_home"))

    _duel_apply_xp_delta_once(duel.challenger_id.id, "duel_entry_fee", f"{duel.id}:challenger_fee", -fee)
    _duel_apply_xp_delta_once(duel.opponent_id.id, "duel_entry_fee", f"{duel.id}:opponent_fee", -fee)

    duel.fee_applied = True
    duel.invite_consumed = True
    # Reset any stale runtime fields before waiting room starts.
    duel.challenger_joined_at = None
    duel.opponent_joined_at = None
    duel.started_at = None
    duel.challenger_submitted = False
    duel.opponent_submitted = False
    duel.challenger_finished_at = None
    duel.opponent_finished_at = None
    duel.challenger_score = 0
    duel.opponent_score = 0
    duel.challenger_penalty_seconds = 0
    duel.opponent_penalty_seconds = 0
    duel.status = "accepted_waiting"
    duel.save()

    flash("تم قبول الدعوة. انتظر حتى ينضم اللاعبان لبدء المباراة.", "success")
    return redirect(url_for("student.duel_play", duel_id=str(duel.id)))


@student_bp.route("/duels/<duel_id>/cancel", methods=["POST"])
@login_required
def duel_cancel(duel_id):
    duel = Duel.objects(id=duel_id).first() if ObjectId.is_valid(duel_id) else None
    if not duel:
        return "404", 404

    if not duel.challenger_id or duel.challenger_id.id != current_user.id:
        flash("فقط مرسل الدعوة يمكنه إلغاءها.", "error")
        return redirect(url_for("student.duels_home"))

    if duel.status != "pending":
        flash("لا يمكن إلغاء دعوة غير معلقة.", "warning")
        return redirect(url_for("student.duel_play", duel_id=str(duel.id)))

    duel.status = "canceled"
    duel.ended_at = datetime.utcnow()
    duel.save()
    flash("تم إلغاء الدعوة.", "success")
    return redirect(url_for("student.duels_home"))


@student_bp.route("/duels/<duel_id>/join", methods=["POST"])
@login_required
def duel_join(duel_id):
    duel = Duel.objects(id=duel_id).first() if ObjectId.is_valid(duel_id) else None
    if not duel:
        return "404", 404

    slot = _duel_player_slot(duel, current_user.id)
    if not slot:
        flash("غير مصرح لك بالانضمام لهذا التحدي.", "error")
        return redirect(url_for("student.duels_home"))

    if duel.status not in {"accepted_waiting", "live"}:
        flash("لا يمكن الانضمام في هذه الحالة.", "warning")
        return redirect(url_for("student.duel_play", duel_id=str(duel.id)))

    now = datetime.utcnow()
    if slot == "challenger" and not duel.challenger_joined_at:
        duel.challenger_joined_at = now
    if slot == "opponent" and not duel.opponent_joined_at:
        duel.opponent_joined_at = now

    if duel.status == "accepted_waiting" and duel.challenger_joined_at and duel.opponent_joined_at:
        duel.status = "live"
        duel.started_at = now

    duel.save()
    return redirect(url_for("student.duel_play", duel_id=str(duel.id)))


@student_bp.route("/duels/<duel_id>", methods=["GET"])
@login_required
def duel_play(duel_id):
    duel = Duel.objects(id=duel_id).first() if ObjectId.is_valid(duel_id) else None
    if not duel:
        return "404", 404

    slot = _duel_player_slot(duel, current_user.id)
    if not slot:
        flash("غير مصرح لك بدخول هذا التحدي.", "error")
        return redirect(url_for("student.duels_home"))

    _duel_expire_if_needed(duel)
    _duel_autosubmit_timeout(duel)

    if duel.status in {"pending", "declined", "expired", "canceled"}:
        return render_template("student/duel_invite.html", duel=duel, slot=slot, challenger_name=(duel.challenger_id.full_name if duel.challenger_id else "لاعب"))

    question_ids = []
    try:
        question_ids = json.loads(duel.question_ids_json or "[]")
    except Exception:
        question_ids = []
    questions = list(Question.objects(id__in=[qid for qid in question_ids if ObjectId.is_valid(str(qid))]).all()) if question_ids else []
    qmap = {str(q.id): q for q in questions}
    ordered_questions = [qmap[qid] for qid in question_ids if qid in qmap]

    player_answers = list(DuelAnswer.objects(duel_id=duel.id, player_id=current_user.id).all())
    answers_by_qid = {str(a.question_id.id): str(a.choice_id) if a.choice_id else "" for a in player_answers if a.question_id}

    state = _duel_build_play_state(duel, slot)
    my_left = int(state["my_left"])
    opp_left = int(state["opp_left"])
    opp_slot = "opponent" if slot == "challenger" else "challenger"
    opponent_user = duel.opponent_id if slot == "challenger" else duel.challenger_id
    phase = state["phase"]
    xp_summary = _duel_get_xp_change_summary(duel, current_user.id) if phase == "completed" else None

    return render_template(
        "student/duel_play.html",
        duel=duel,
        phase=phase,
        slot=slot,
        opponent_user=opponent_user,
        questions=ordered_questions,
        answers_by_qid=answers_by_qid,
        my_left=my_left,
        opp_left=opp_left,
        my_submitted=state["my_submitted"],
        opp_submitted=state["opp_submitted"],
        xp_summary=xp_summary,
    )


@student_bp.route("/duels/<duel_id>/state", methods=["GET"])
@login_required
def duel_state(duel_id):
    duel = Duel.objects(id=duel_id).first() if ObjectId.is_valid(duel_id) else None
    if not duel:
        return jsonify({"ok": False, "error": "not_found"}), 404

    slot = _duel_player_slot(duel, current_user.id)
    if not slot:
        return jsonify({"ok": False, "error": "forbidden"}), 403

    _duel_expire_if_needed(duel)
    _duel_autosubmit_timeout(duel)
    state = _duel_build_play_state(duel, slot)
    opp_slot = "opponent" if slot == "challenger" else "challenger"
    opponent_perfect_first_warning = (
        bool(getattr(duel, "first_submitter_perfect", False))
        and getattr(duel, "first_submitter_slot", None) == opp_slot
        and not bool(state["my_submitted"])
        and bool(state["opp_submitted"])
    )

    return jsonify(
        {
            "ok": True,
            "status": duel.status,
            "phase": state["phase"],
            "my_left": int(state["my_left"]),
            "opp_left": int(state["opp_left"]),
            "my_submitted": bool(state["my_submitted"]),
            "opp_submitted": bool(state["opp_submitted"]),
            "slot_joined": bool(state["slot_joined"]),
            "both_joined": bool(state["both_joined"]),
            "challenger_score": int(duel.challenger_score or 0),
            "opponent_score": int(duel.opponent_score or 0),
            "opponent_perfect_first_warning": opponent_perfect_first_warning,
            "opponent_time_deduction_seconds": 60 if opponent_perfect_first_warning else 0,
        }
    )


@student_bp.route("/duels/<duel_id>/review", methods=["GET"])
@login_required
def duel_review(duel_id):
    duel = Duel.objects(id=duel_id).first() if ObjectId.is_valid(duel_id) else None
    if not duel:
        return "404", 404

    slot = _duel_player_slot(duel, current_user.id)
    if not slot:
        flash("غير مصرح لك بمراجعة هذا التحدي.", "error")
        return redirect(url_for("student.duels_home"))

    if duel.status != "completed":
        flash("المراجعة متاحة فقط بعد انتهاء التحدي.", "warning")
        return redirect(url_for("student.duel_play", duel_id=str(duel.id)))

    question_ids = []
    try:
        question_ids = json.loads(duel.question_ids_json or "[]")
    except Exception:
        question_ids = []

    valid_qids = [qid for qid in question_ids if ObjectId.is_valid(str(qid))]
    questions = list(Question.objects(id__in=valid_qids).all()) if valid_qids else []
    qmap = {str(q.id): q for q in questions}

    all_answers = list(DuelAnswer.objects(duel_id=duel.id).all())
    answers_by_key = {}
    for a in all_answers:
        if not a.question_id or not a.player_id:
            continue
        answers_by_key[(str(a.player_id.id), str(a.question_id.id))] = a

    my_user = duel.challenger_id if slot == "challenger" else duel.opponent_id
    opponent_user = duel.opponent_id if slot == "challenger" else duel.challenger_id
    my_user_id = str(my_user.id) if my_user else ""
    opponent_user_id = str(opponent_user.id) if opponent_user else ""

    review = []
    for qid in question_ids:
        q = qmap.get(str(qid))
        if not q:
            continue

        choices_by_id = {str(c.choice_id): c for c in q.choices}
        correct_choice = None
        if q.correct_choice_id:
            correct_choice = choices_by_id.get(str(q.correct_choice_id))
        if not correct_choice:
            correct_choice = next((c for c in q.choices if c.is_correct), None)

        my_ans = answers_by_key.get((my_user_id, str(q.id)))
        opp_ans = answers_by_key.get((opponent_user_id, str(q.id)))
        my_selected = choices_by_id.get(str(my_ans.choice_id)) if my_ans and my_ans.choice_id else None
        opp_selected = choices_by_id.get(str(opp_ans.choice_id)) if opp_ans and opp_ans.choice_id else None

        review.append(
            {
                "question": q,
                "my_selected": my_selected,
                "my_is_correct": bool(my_ans.is_correct) if my_ans else False,
                "opp_selected": opp_selected,
                "opp_is_correct": bool(opp_ans.is_correct) if opp_ans else False,
                "correct_choice": correct_choice,
            }
        )

    return render_template(
        "student/duel_review.html",
        duel=duel,
        my_user=my_user,
        opponent_user=opponent_user,
        review=review,
    )


@student_bp.route("/duels/<duel_id>/submit", methods=["POST"])
@login_required
def duel_submit(duel_id):
    duel = Duel.objects(id=duel_id).first() if ObjectId.is_valid(duel_id) else None
    if not duel:
        return "404", 404

    slot = _duel_player_slot(duel, current_user.id)
    if not slot:
        flash("غير مصرح لك بتسليم هذا التحدي.", "error")
        return redirect(url_for("student.duels_home"))

    if not _duel_slot_has_joined(duel, slot):
        flash("يجب الانضمام للمباراة أولاً قبل التسليم.", "warning")
        return redirect(url_for("student.duel_play", duel_id=str(duel.id)))

    _duel_autosubmit_timeout(duel)
    if duel.status != "live":
        flash("المباراة ليست قيد اللعب حالياً.", "warning")
        return redirect(url_for("student.duel_play", duel_id=str(duel.id)))

    now = datetime.utcnow()
    my_left = _duel_time_left_seconds(duel, slot, now=now)

    question_ids = []
    try:
        question_ids = json.loads(duel.question_ids_json or "[]")
    except Exception:
        question_ids = []
    questions = list(Question.objects(id__in=[qid for qid in question_ids if ObjectId.is_valid(str(qid))]).all()) if question_ids else []
    qmap = {str(q.id): q for q in questions}

    DuelAnswer.objects(duel_id=duel.id, player_id=current_user.id).delete()
    score = 0
    for qid in question_ids:
        q = qmap.get(str(qid))
        if not q:
            continue
        selected = (request.form.get(f"q_{qid}") or "").strip()
        if selected and ObjectId.is_valid(selected):
            is_correct = bool(q.correct_choice_id and str(q.correct_choice_id) == selected)
            if is_correct:
                score += 1
            DuelAnswer(
                duel_id=duel.id,
                player_id=current_user.id,
                question_id=q.id,
                choice_id=ObjectId(selected),
                is_correct=is_correct,
            ).save()

    was_already_submitted = _duel_slot_submitted(duel, slot)

    if slot == "challenger":
        duel.challenger_submitted = True
        duel.challenger_finished_at = now
        duel.challenger_score = score
    else:
        duel.opponent_submitted = True
        duel.opponent_finished_at = now
        duel.opponent_score = score

    total_questions = max(0, len(question_ids))
    submitted_perfect = total_questions > 0 and score >= total_questions

    other_slot = "opponent" if slot == "challenger" else "challenger"
    other_submitted = duel.opponent_submitted if slot == "challenger" else duel.challenger_submitted
    if not was_already_submitted and not other_submitted:
        duel.first_submitter_slot = slot
        duel.first_submitter_perfect = bool(submitted_perfect)
        if submitted_perfect:
            # Perfect first finisher applies a full-minute time hit to the remaining player.
            if other_slot == "challenger":
                duel.challenger_penalty_seconds = int(duel.challenger_penalty_seconds or 0) + 60
            else:
                duel.opponent_penalty_seconds = int(duel.opponent_penalty_seconds or 0) + 60
        else:
            other_left = _duel_time_left_seconds(duel, other_slot, now=now)
            if _duel_should_apply_finish_penalty(other_left):
                if other_slot == "challenger":
                    duel.challenger_penalty_seconds = int(duel.challenger_penalty_seconds or 0) + 15
                else:
                    duel.opponent_penalty_seconds = int(duel.opponent_penalty_seconds or 0) + 15
    elif not was_already_submitted and other_submitted:
        duel.second_submitter_perfect = bool(submitted_perfect)

    if duel.challenger_submitted and duel.opponent_submitted:
        duel.status = "completed"
        duel.ended_at = now

    duel.save()
    if duel.status == "completed":
        _duel_try_settle(duel)

    if my_left <= 0:
        flash("انتهى الوقت وتم تسليم نتيجتك تلقائياً.", "warning")
    else:
        flash("تم تسليم إجابات التحدي.", "success")
    return redirect(url_for("student.duel_play", duel_id=str(duel.id)))


@student_bp.route("/duels/leaderboard", methods=["GET"])
@login_required
def duel_leaderboard():
    stats_top = list(DuelStats.objects().order_by("-wins", "-current_win_streak", "student_id").limit(10).all())
    student_ids = [s.student_id.id for s in stats_top if s.student_id]
    users = User.objects(id__in=student_ids).only("id", "username", "first_name", "last_name").all() if student_ids else []
    users_by_id = {u.id: u for u in users}
    return render_template("student/duel_leaderboard.html", stats_top=stats_top, users_by_id=users_by_id)

@student_bp.route("/tests/<test_id>", methods=["GET", "POST"])
@login_required
def take_test(test_id):
    test = Test.objects(id=test_id).first()
    if not test:
        return "404", 404
    if current_user.role == "student":
        access = AccessContext(test.section, current_user.id)
        if not access.test_open(test):
            # Redirect to appropriate activation page based on test type
            if test.lesson_id:
                flash("قم بتفعيل الدرس للوصول إلى هذا الاختبار.", "warning")
                return redirect(url_for("student.activate_lesson", lesson_id=test.lesson_id))
            else:
                flash("قم بتفعيل القسم للوصول إلى هذا الاختبار.", "warning")
                return redirect(url_for("student.activate_section", section_id=test.section_id))

    total_questions_available = len(test.questions)
    text_questions = list(TestTextQuestion.objects(test_id=test.id).order_by('created_at').all())
    min_select = 10 if total_questions_available >= 10 else total_questions_available
    max_select = min(50, total_questions_available)

    selected_count = _to_int(request.values.get("count"), None)
    easy_count = _to_int(request.values.get("easy"), 0)
    medium_count = _to_int(request.values.get("medium"), 0)
    hard_count = _to_int(request.values.get("hard"), 0)
    preset_question_ids_raw = request.values.get("question_ids", "") or ""
    preset_question_ids = [
        qid.strip() for qid in preset_question_ids_raw.split(",") if ObjectId.is_valid(qid.strip())
    ]

    retake_source_id = request.values.get("retake_source_id")
    retake_mode = request.values.get("retake_mode")

    if selected_count is None and total_questions_available == 0 and text_questions:
        selected_count = 0

    if selected_count is None and preset_question_ids:
        selected_count = len(preset_question_ids)
    if selected_count:
        lower_bound = 10 if total_questions_available >= 10 else 1
        selected_count = max(lower_bound, min(selected_count, max_select))
    selected_by_level = (easy_count + medium_count + hard_count) > 0

    if request.method == "POST":
        # Evaluate answers
        question_ids_raw = request.form.get("question_ids", "")
        text_question_ids_raw = request.form.get("text_question_ids", "")
        if question_ids_raw:
            question_ids = [qid.strip() for qid in question_ids_raw.split(",") if ObjectId.is_valid(qid.strip())]
            questions = Question.objects(id__in=question_ids).all()
            questions_by_id = {str(q.id): q for q in questions}
            ordered_questions = [questions_by_id[qid] for qid in question_ids if qid in questions_by_id]
        else:
            ordered_questions = list(test.questions)

        ordered_text_questions = list(text_questions)
        if text_question_ids_raw:
            text_question_ids = [qid.strip() for qid in text_question_ids_raw.split(",") if ObjectId.is_valid(qid.strip())]
            text_q_map = {str(tq.id): tq for tq in TestTextQuestion.objects(id__in=text_question_ids, test_id=test.id).all()}
            ordered_text_questions = [text_q_map[qid] for qid in text_question_ids if qid in text_q_map]

        settings_payload = {
            "count": _to_int(request.form.get("count"), len(ordered_questions)),
            "easy": _to_int(request.form.get("easy"), 0),
            "medium": _to_int(request.form.get("medium"), 0),
            "hard": _to_int(request.form.get("hard"), 0),
        }

        is_retake = False
        if retake_source_id and ObjectId.is_valid(str(retake_source_id)):
            source = Attempt.objects(id=retake_source_id, student_id=current_user.id).first()
            is_retake = bool(source)

        question_order = [str(q.id) for q in ordered_questions]
        text_total = sum(max(1, int(getattr(tq, "max_score", 5) or 5)) for tq in ordered_text_questions)
        total = len(ordered_questions) + text_total
        score = 0
        attempt = Attempt(
            test_id=test.id,
            student_id=current_user.id,
            score=0,
            total=total,
            question_order_json=json.dumps(question_order),
            selection_settings_json=json.dumps(settings_payload),
            is_retake=is_retake,
        )
        attempt.save()
        for q in ordered_questions:
            selected_choice_id = request.form.get(f"question_{q.id}")
            if not selected_choice_id:
                is_correct = False
                choice_id = None
            else:
                choice = next((c for c in q.choices if str(c.choice_id) == selected_choice_id), None)
                if q.correct_choice_id:
                    is_correct = bool(choice and choice.choice_id == q.correct_choice_id)
                else:
                    is_correct = bool(choice and choice.is_correct)
                choice_id = choice.choice_id if choice else None
            if is_correct:
                score += 1
            if choice_id is not None:
                ans = AttemptAnswer(
                    attempt_id=attempt.id,
                    question_id=q.id,
                    choice_id=choice_id,
                    is_correct=is_correct,
                )
                ans.save()

        for tq in ordered_text_questions:
            text_answer = (request.form.get(f"text_question_{tq.id}") or "").strip()
            if text_answer:
                AttemptTextAnswer(
                    attempt_id=attempt.id,
                    text_question_id=tq.id,
                    answer_text=text_answer,
                    max_score=max(1, int(getattr(tq, "max_score", 5) or 5)),
                    score_awarded=None,
                ).save()
        attempt.score = score
        earned_xp, _ = _award_xp_for_attempt(
            student_id=current_user.id,
            event_type="test_submit",
            source_id=str(attempt.id),
            score=score,
            total=total,
            is_retake=is_retake,
        )
        attempt.xp_earned = earned_xp
        attempt.save()
        flash(f"حصلت على {score}/{total}", "success")
        return redirect(url_for("student.test_result", attempt_id=attempt.id))

    ordered_questions = []
    question_ids_str = ""
    time_limit_seconds = None
    text_question_ids_str = ",".join(str(tq.id) for tq in text_questions)
    if selected_count:
        questions = list(test.questions)
        if preset_question_ids:
            questions_by_id = {str(q.id): q for q in Question.objects(id__in=preset_question_ids).all()}
            questions = [questions_by_id[qid] for qid in preset_question_ids if qid in questions_by_id]
            selected_count = len(questions)
        elif selected_by_level:
            level_map = {"easy": [], "medium": [], "hard": []}
            for q in questions:
                level = (getattr(q, "difficulty", "medium") or "medium").lower()
                if level not in level_map:
                    level = "medium"
                level_map[level].append(q)

            if easy_count > len(level_map["easy"]) or medium_count > len(level_map["medium"]) or hard_count > len(level_map["hard"]):
                flash("عدد الأسئلة المطلوب أعلى من المتاح لهذا المستوى.", "error")
                return redirect(url_for("student.take_test", test_id=test.id))

            if selected_count and (easy_count + medium_count + hard_count) != selected_count:
                flash("مجموع المستويات يجب أن يساوي عدد الأسئلة المختار.", "error")
                return redirect(url_for("student.take_test", test_id=test.id))

            picked = []
            if easy_count:
                picked.extend(random.sample(level_map["easy"], easy_count))
            if medium_count:
                picked.extend(random.sample(level_map["medium"], medium_count))
            if hard_count:
                picked.extend(random.sample(level_map["hard"], hard_count))
            questions = picked
        else:
            if selected_count < len(questions):
                questions = random.sample(questions, selected_count)

        if not preset_question_ids:
            random.shuffle(questions)
        question_ids = [q.id for q in questions]
        question_ids_str = ",".join(str(qid) for qid in question_ids)
        for q in questions:
            choices = list(q.choices)
            random.shuffle(choices)
            ordered_questions.append({"question": q, "choices": choices, "question_type": "mcq"})
        for tq in text_questions:
            ordered_questions.append({"text_question": tq, "choices": [], "question_type": "text"})
        time_limit_seconds = (len(question_ids) * 75) + 15
    elif selected_count is not None and total_questions_available == 0 and text_questions:
        for tq in text_questions:
            ordered_questions.append({"text_question": tq, "choices": [], "question_type": "text"})
        time_limit_seconds = (len(text_questions) * 75) + 15

    # Available counts per difficulty for UI
    def _norm_level(q):
        level = (getattr(q, "difficulty", "medium") or "medium").lower()
        return level if level in {"easy", "medium", "hard"} else "medium"

    available_easy = len([q for q in test.questions if _norm_level(q) == "easy"])
    available_medium = len([q for q in test.questions if _norm_level(q) == "medium"])
    available_hard = len([q for q in test.questions if _norm_level(q) == "hard"])

    return render_template(
        "student/take_test.html",
        test=test,
        total_questions=total_questions_available,
        min_select=min_select,
        max_select=max_select,
        selected_count=selected_count,
        available_easy=available_easy,
        available_medium=available_medium,
        available_hard=available_hard,
        easy_count=easy_count,
        medium_count=medium_count,
        hard_count=hard_count,
        ordered_questions=ordered_questions,
        question_ids_str=question_ids_str,
        text_question_ids_str=text_question_ids_str,
        time_limit_seconds=time_limit_seconds,
        retake_source_id=retake_source_id,
        retake_mode=retake_mode,
    )


@student_bp.route("/subjects/<subject_id>/activate", methods=["GET", "POST"])
@login_required
def activate_subject(subject_id):
    subject = Subject.objects(id=subject_id).first()
    if not subject:
        return "404", 404
    form = ActivationForm()
    if request.method == "POST" and form.validate_on_submit():
        code_value = form.code.data.strip().upper()
        ac = SubjectActivationCode.objects(subject_id=subject.id, student_id=current_user.id, code=code_value).first()
        if not ac:
            flash("رمز غير صحيح لهذه المادة.", "error")
            return render_template("student/activate_subject.html", subject=subject, form=form)
        if ac.is_used:
            flash("This code has already been used.", "error")
            return render_template("student/activate_subject.html", subject=subject, form=form)
        # mark used and activate
        ac.is_used = True
        ac.used_at = datetime.utcnow()
        ac.save()
        existing = SubjectActivation.objects(subject_id=subject.id, student_id=current_user.id, active=True).first()
        if not existing:
            SubjectActivation(subject_id=subject.id, student_id=current_user.id).save()
        cascade_subject_activation(subject, current_user.id)
        flash("تم تفعيل المادة بالكامل!", "success")
        return redirect(url_for("student.subject_detail", subject_id=subject.id))
    return render_template("student/activate_subject.html", subject=subject, form=form)


@student_bp.route("/sections/<section_id>/activate", methods=["GET", "POST"])
@login_required
def activate_section(section_id):
    section = Section.objects(id=section_id).first()
    if not section:
        return "404", 404
    form = ActivationForm()
    if request.method == "POST" and form.validate_on_submit():
        code_value = form.code.data.strip().upper()
        ac = ActivationCode.objects(section_id=section.id, student_id=current_user.id, code=code_value).first()
        if not ac:
            flash("رمز غير صحيح لهذا القسم.", "error")
            return render_template("student/activate_section.html", section=section, form=form)
        if ac.is_used:
            flash("This code has already been used.", "error")
            return render_template("student/activate_section.html", section=section, form=form)
        # mark used and activate
        ac.is_used = True
        ac.used_at = datetime.utcnow()
        ac.save()
        existing = SectionActivation.objects(section_id=section.id, student_id=current_user.id, active=True).first()
        if not existing:
            SectionActivation(section_id=section.id, student_id=current_user.id).save()
        cascade_section_activation(section, current_user.id)
        flash("تم تفعيل القسم!", "success")
        return redirect(url_for("student.section_detail", section_id=section.id))
    return render_template("student/activate_section.html", section=section, form=form)


@student_bp.route("/lessons/<lesson_id>/activate", methods=["GET", "POST"])
@login_required
def activate_lesson(lesson_id):
    lesson = Lesson.objects(id=lesson_id).first()
    if not lesson:
        return "404", 404
    section = lesson.section
    form = LessonActivationForm()
    if request.method == "POST" and form.validate_on_submit():
        code_value = form.code.data.strip().upper()
        ac = LessonActivationCode.objects(lesson_id=lesson.id, student_id=current_user.id, code=code_value).first()
        if not ac:
            flash("رمز غير صحيح لهذا الدرس.", "error")
            return render_template("student/activate_lesson.html", lesson=lesson, section=section, form=form)
        if ac.is_used:
            flash("This code has already been used.", "error")
            return render_template("student/activate_lesson.html", lesson=lesson, section=section, form=form)
        ac.is_used = True
        ac.used_at = datetime.utcnow()
        ac.save()
        existing = LessonActivation.objects(lesson_id=lesson.id, student_id=current_user.id, active=True).first()
        if not existing:
            LessonActivation(lesson_id=lesson.id, student_id=current_user.id).save()
        cascade_lesson_activation(lesson, current_user.id)
        flash("تم تفعيل الدرس!", "success")
        return redirect(url_for("student.lesson_detail", lesson_id=lesson.id))
    return render_template("student/activate_lesson.html", lesson=lesson, section=section, form=form)


@student_bp.route("/assignments")
@login_required
def assignments():
    if current_user.role != "student":
        flash("هذه الصفحة للطلاب فقط.", "warning")
        return redirect(url_for("index"))

    active_assignments = list(Assignment.objects(is_active=True).order_by("due_at", "-created_at").all())
    relevant_assignments = []
    for assignment in active_assignments:
        target = getattr(assignment, "target_student_id", None)
        if target and str(target.id) != str(current_user.id):
            continue
        relevant_assignments.append(assignment)

    submissions = AssignmentSubmission.objects(
        assignment_id__in=[a.id for a in relevant_assignments],
        student_id=current_user.id,
    ).all() if relevant_assignments else []
    attempts = AssignmentAttempt.objects(
        assignment_id__in=[a.id for a in relevant_assignments],
        student_id=current_user.id,
    ).all() if relevant_assignments else []
    submissions_by_assignment = {s.assignment_id.id: s for s in submissions if s.assignment_id}
    attempts_by_assignment = {a.assignment_id.id: a for a in attempts if a.assignment_id}

    assignments_data = []
    now = datetime.utcnow()
    for assignment in relevant_assignments:
        if assignment.assignment_mode == "custom_test":
            attempt = attempts_by_assignment.get(assignment.id)
            submission = None
            raw_status = attempt.status if attempt else "pending"
            status = "completed" if raw_status == "graded" else raw_status
            score_awarded = int(attempt.score_awarded or 0) if attempt else 0
            total_score = int(attempt.total_score or int(assignment.max_score or 0)) if attempt else int(assignment.max_score or 0)
        else:
            submission = submissions_by_assignment.get(assignment.id)
            attempt = None
            raw_status = submission.status if submission else "pending"
            status = submission.status if submission else "pending"
            score_awarded = None
            total_score = None
        is_overdue = bool(assignment.due_at and assignment.due_at < now and status != "completed")
        assignments_data.append(
            {
                "assignment": assignment,
                "submission": submission,
                "attempt": attempt,
                "status": status,
                "raw_status": raw_status,
                "is_overdue": is_overdue,
                "score_awarded": score_awarded,
                "total_score": total_score,
            }
        )

    return render_template("student/assignments.html", assignments_data=assignments_data)


@student_bp.route("/assignments/<assignment_id>/complete", methods=["POST"])
@login_required
def complete_assignment(assignment_id):
    if current_user.role != "student":
        flash("هذه الخاصية للطلاب فقط.", "warning")
        return redirect(url_for("index"))

    assignment = Assignment.objects(id=assignment_id, is_active=True).first()
    if not assignment:
        flash("الواجب غير موجود.", "error")
        return redirect(url_for("student.assignments"))

    if assignment.target_student_id and str(assignment.target_student_id.id) != str(current_user.id):
        flash("هذا الواجب غير مخصص لك.", "error")
        return redirect(url_for("student.assignments"))

    note = (request.form.get("note") or "").strip() or None
    submission = AssignmentSubmission.objects(
        assignment_id=assignment.id,
        student_id=current_user.id,
    ).first()
    if not submission:
        submission = AssignmentSubmission(
            assignment_id=assignment.id,
            student_id=current_user.id,
            status="completed",
            note=note,
            completed_at=datetime.utcnow(),
        )
    else:
        submission.status = "completed"
        submission.note = note
        submission.completed_at = datetime.utcnow()
    submission.save()

    _award_flat_xp_once(
        student_id=current_user.id,
        event_type="assignment_complete",
        source_id=str(assignment.id),
        amount=10,
    )
    flash("تم تسليم الواجب بنجاح.", "success")
    return redirect(url_for("student.assignments"))


@student_bp.route("/assignments/<assignment_id>/solve", methods=["GET", "POST"])
@login_required
def solve_assignment(assignment_id):
    if current_user.role != "student":
        flash("هذه الصفحة للطلاب فقط.", "warning")
        return redirect(url_for("index"))

    assignment = Assignment.objects(id=assignment_id, is_active=True, assignment_mode="custom_test").first()
    if not assignment:
        flash("الواجب غير موجود.", "error")
        return redirect(url_for("student.assignments"))

    if assignment.target_student_id and str(assignment.target_student_id.id) != str(current_user.id):
        flash("هذا الواجب غير مخصص لك.", "error")
        return redirect(url_for("student.assignments"))

    existing_attempt = AssignmentAttempt.objects(assignment_id=assignment.id, student_id=current_user.id).first()
    if existing_attempt and existing_attempt.status in {"submitted", "graded"}:
        flash("تم إرسال هذا الواجب مسبقاً.", "info")
        return redirect(url_for("student.assignments"))

    question_ids = []
    if assignment.selected_question_ids_json:
        try:
            question_ids = [qid for qid in json.loads(assignment.selected_question_ids_json) if ObjectId.is_valid(qid)]
        except Exception:
            question_ids = []

    questions = Question.objects(id__in=question_ids).all() if question_ids else []
    questions_by_id = {str(q.id): q for q in questions}
    ordered_questions = [questions_by_id[qid] for qid in question_ids if qid in questions_by_id]

    written_items = []
    if assignment.written_questions_json:
        try:
            payload = json.loads(assignment.written_questions_json)
            if isinstance(payload, list):
                written_items = payload
        except Exception:
            written_items = []

    if request.method == "POST":
        answers = []
        total_score = 0

        for q in ordered_questions:
            selected_choice_id = request.form.get(f"mcq_{q.id}")
            answers.append(
                {
                    "type": "mcq",
                    "question_id": str(q.id),
                    "selected_choice_id": selected_choice_id,
                    "max_score": 1,
                    "score_awarded": 0,
                }
            )
            total_score += 1

        for idx, item in enumerate(written_items):
            text_answer = (request.form.get(f"text_{idx}") or "").strip()
            max_score = max(1, int(item.get("max_score", 5) or 5))
            answers.append(
                {
                    "type": "text",
                    "prompt": item.get("prompt", ""),
                    "text_answer": text_answer,
                    "max_score": max_score,
                    "score_awarded": 0,
                }
            )
            total_score += max_score

        attempt = AssignmentAttempt(
            assignment_id=assignment.id,
            student_id=current_user.id,
            answers_json=json.dumps(answers, ensure_ascii=False),
            status="submitted",
            total_score=total_score,
            score_awarded=0,
            submitted_at=datetime.utcnow(),
        )
        attempt.save()
        flash("تم إرسال الحل بنجاح. بانتظار التصحيح من المعلم.", "success")
        return redirect(url_for("student.assignments"))

    return render_template(
        "student/assignment_solve.html",
        assignment=assignment,
        questions=ordered_questions,
        written_items=written_items,
    )


@student_bp.route("/assignments/<assignment_id>/view", methods=["GET"])
@login_required
def view_assignment_questions(assignment_id):
    if current_user.role != "student":
        flash("هذه الصفحة للطلاب فقط.", "warning")
        return redirect(url_for("index"))

    assignment = Assignment.objects(id=assignment_id, assignment_mode="custom_test").first()
    if not assignment:
        flash("الواجب غير موجود.", "error")
        return redirect(url_for("student.assignments"))

    if assignment.target_student_id and str(assignment.target_student_id.id) != str(current_user.id):
        flash("هذا الواجب غير مخصص لك.", "error")
        return redirect(url_for("student.assignments"))

    question_ids = []
    if assignment.selected_question_ids_json:
        try:
            question_ids = [qid for qid in json.loads(assignment.selected_question_ids_json) if ObjectId.is_valid(qid)]
        except Exception:
            question_ids = []

    questions = Question.objects(id__in=question_ids).all() if question_ids else []
    questions_by_id = {str(q.id): q for q in questions}
    ordered_questions = [questions_by_id[qid] for qid in question_ids if qid in questions_by_id]

    written_items = []
    if assignment.written_questions_json:
        try:
            payload = json.loads(assignment.written_questions_json)
            if isinstance(payload, list):
                written_items = payload
        except Exception:
            written_items = []

    attempt = AssignmentAttempt.objects(assignment_id=assignment.id, student_id=current_user.id).first()
    mcq_answers = {}
    text_answers = []
    teacher_note = None
    if attempt:
        teacher_note = attempt.teacher_note
        try:
            answers = json.loads(attempt.answers_json or "[]")
            if isinstance(answers, list):
                for ans in answers:
                    if ans.get("type") == "mcq":
                        mcq_answers[str(ans.get("question_id") or "")] = ans
                    elif ans.get("type") == "text":
                        text_answers.append(ans)
        except Exception:
            pass

    return render_template(
        "student/assignment_view.html",
        assignment=assignment,
        questions=ordered_questions,
        written_items=written_items,
        attempt=attempt,
        mcq_answers=mcq_answers,
        text_answers=text_answers,
        teacher_note=teacher_note,
    )


@student_bp.route("/study-plans")
@login_required
def study_plans():
    if current_user.role != "student":
        flash("هذه الصفحة للطلاب فقط.", "warning")
        return redirect(url_for("index"))

    plans = list(StudyPlan.objects(student_id=current_user.id, is_active=True).order_by("-created_at").all())
    items = StudyPlanItem.objects(plan_id__in=[p.id for p in plans]).order_by("due_at", "created_at").all() if plans else []
    items_by_plan = {}
    for item in items:
        pid = item.plan_id.id if item.plan_id else None
        if not pid:
            continue
        items_by_plan.setdefault(pid, []).append(item)

    return render_template("student/study_plans.html", plans=plans, items_by_plan=items_by_plan)


@student_bp.route("/study-plans/items/<item_id>/toggle", methods=["POST"])
@login_required
def toggle_study_plan_item(item_id):
    if current_user.role != "student":
        flash("هذه الخاصية للطلاب فقط.", "warning")
        return redirect(url_for("index"))

    item = StudyPlanItem.objects(id=item_id).first()
    if not item or not item.plan_id:
        flash("المهمة غير موجودة.", "error")
        return redirect(url_for("student.study_plans"))

    plan = item.plan_id
    if not plan.student_id or str(plan.student_id.id) != str(current_user.id):
        flash("غير مصرح لك بهذا الإجراء.", "error")
        return redirect(url_for("student.study_plans"))

    item.is_done = not item.is_done
    item.done_at = datetime.utcnow() if item.is_done else None
    item.save()

    if item.is_done:
        _award_flat_xp_once(
            student_id=current_user.id,
            event_type="study_plan_item_complete",
            source_id=str(item.id),
            amount=5,
        )
        flash("تم إنجاز المهمة (+5 XP).", "success")
    else:
        flash("تم إعادة المهمة إلى غير منجزة.", "info")

    return redirect(url_for("student.study_plans"))


@student_bp.route("/results")
@login_required
def results():
    # Split own attempts and others for clearer presentation; answers still gated in test_result
    def _filter_missing_tests(attempts):
        filtered = []
        for attempt in attempts:
            try:
                _ = attempt.test
                # Also check if student exists
                _ = attempt.student_id.id
                filtered.append(attempt)
            except Exception:
                continue
        return filtered

    own_attempts = _filter_missing_tests(
        Attempt.objects(student_id=current_user.id)
        .order_by("-started_at")
        .all()
    )
    own_custom_attempts = list(
        CustomTestAttempt.objects(
            student_id=current_user.id,
            status="submitted",
        )
        .order_by("-created_at")
        .all()
    )
    other_attempts = _filter_missing_tests(
        Attempt.objects(student_id__ne=current_user.id)
        .order_by("-started_at")
        .all()
    )

    all_attempts = own_attempts + other_attempts
    attempt_ids = [a.id for a in all_attempts if a.id]
    text_answers = list(AttemptTextAnswer.objects(attempt_id__in=attempt_ids).all()) if attempt_ids else []
    text_by_attempt = {}
    for ta in text_answers:
        if not ta.attempt_id:
            continue
        aid = ta.attempt_id.id
        text_by_attempt.setdefault(aid, []).append(ta)

    for attempt in all_attempts:
        tas = text_by_attempt.get(attempt.id, [])
        pending = bool(tas) and any(getattr(ta, "score_awarded", None) is None for ta in tas)
        attempt._pending_text_grading = pending
    return render_template(
        "student/results.html",
        own_attempts=own_attempts,
        own_custom_attempts=own_custom_attempts,
        other_attempts=other_attempts,
    )


def _frequently_wrong_question_counts(student_id):
    counts = {}
    if not student_id:
        return counts

    regular_attempt_ids = [a.id for a in Attempt.objects(student_id=student_id).only("id").all()]
    if regular_attempt_ids:
        rows = list(
            AttemptAnswer._get_collection().aggregate(
                [
                    {"$match": {"attempt_id": {"$in": regular_attempt_ids}, "is_correct": False}},
                    {"$group": {"_id": "$question_id", "count": {"$sum": 1}}},
                ]
            )
        )
        for row in rows:
            qid = row.get("_id")
            if qid:
                counts[qid] = counts.get(qid, 0) + int(row.get("count", 0) or 0)

    custom_attempt_ids = [
        a.id
        for a in CustomTestAttempt.objects(student_id=student_id, status="submitted").only("id").all()
    ]
    if custom_attempt_ids:
        rows = list(
            CustomTestAnswer._get_collection().aggregate(
                [
                    {"$match": {"attempt_id": {"$in": custom_attempt_ids}, "is_correct": False}},
                    {"$group": {"_id": "$question_id", "count": {"$sum": 1}}},
                ]
            )
        )
        for row in rows:
            qid = row.get("_id")
            if qid:
                counts[qid] = counts.get(qid, 0) + int(row.get("count", 0) or 0)

    return counts


@student_bp.route("/frequently-wrong", methods=["GET"])
@login_required
def frequently_wrong():
    if current_user.role != "student":
        flash("هذه الصفحة للطلاب فقط.", "warning")
        return redirect(url_for("index"))

    freq_map = _frequently_wrong_question_counts(current_user.id)
    question_ids = list(freq_map.keys())
    questions = list(Question.objects(id__in=question_ids).all()) if question_ids else []
    questions_by_id = {q.id: q for q in questions}

    rows = []
    for qid, freq in freq_map.items():
        q = questions_by_id.get(qid)
        if not q:
            continue
        rows.append(
            {
                "question": q,
                "frequency": int(freq or 0),
                "test": q.test_id,
            }
        )

    rows.sort(key=lambda r: r["frequency"], reverse=True)
    total_wrong = sum(r["frequency"] for r in rows)

    return render_template(
        "student/frequently_wrong.html",
        rows=rows,
        total_wrong=total_wrong,
    )


@student_bp.route("/frequently-wrong/start", methods=["POST"])
@login_required
def frequently_wrong_start_test():
    if current_user.role != "student":
        flash("هذه الصفحة للطلاب فقط.", "warning")
        return redirect(url_for("index"))

    selected_ids = [qid for qid in request.form.getlist("question_ids") if ObjectId.is_valid(qid)]
    if not selected_ids:
        flash("اختر سؤالاً واحداً على الأقل لبدء الاختبار.", "error")
        return redirect(url_for("student.frequently_wrong"))

    questions = list(Question.objects(id__in=selected_ids).all())
    if not questions:
        flash("تعذر تجهيز الاختبار من الأسئلة المختارة.", "error")
        return redirect(url_for("student.frequently_wrong"))

    random.shuffle(questions)
    question_order = [str(q.id) for q in questions]
    answer_order = {}
    for q in questions:
        choices = list(q.choices)
        random.shuffle(choices)
        answer_order[str(q.id)] = [str(c.choice_id) for c in choices]

    selections_payload = {
        "mode": "frequently_wrong",
        "question_count": len(question_order),
    }

    attempt = CustomTestAttempt(
        student_id=current_user.id,
        label="Frequently Wrong Review",
        total=len(question_order),
        selections_json=json.dumps(selections_payload),
        question_order_json=json.dumps(question_order),
        answer_order_json=json.dumps(answer_order),
    )
    attempt.save()

    return redirect(url_for("student.custom_test_take", attempt_id=attempt.id))

@student_bp.route("/results/<attempt_id>")
@login_required
def test_result(attempt_id):
    attempt = Attempt.objects(id=attempt_id).first()
    if not attempt:
        return "404", 404
    if str(attempt.student_id.id) != str(current_user.id) and current_user.role not in {"teacher", "admin"}:
        flash("غير مسموح", "error")
        return redirect(url_for("student.subjects"))
    
    # Get only the questions that were answered in this attempt
    answers = AttemptAnswer.objects(attempt_id=attempt.id).all()
    question_ids = [a.question_id.id for a in answers if a.question_id]
    questions = Question.objects(id__in=question_ids).all()
    questions_map = {str(q.id): q for q in questions}
    
    # Build review in the order of answers
    review = []
    for ans in answers:
        if not ans.question_id:
            continue
        q = questions_map.get(str(ans.question_id.id))
        if not q:
            continue
        selected_choice = None
        if ans.choice_id:
            selected_choice = next((c for c in q.choices if c.choice_id == ans.choice_id), None)
        correct_choice = next((c for c in q.choices if c.is_correct), None)
        review.append({
            "question": q,
            "selected_choice": selected_choice,
            "correct_choice": correct_choice,
            "is_correct": ans.is_correct,
        })

    text_answers = list(AttemptTextAnswer.objects(attempt_id=attempt.id).all())
    text_question_ids = [ta.text_question_id.id for ta in text_answers if ta.text_question_id]
    text_questions_map = {
        str(tq.id): tq for tq in TestTextQuestion.objects(id__in=text_question_ids).all()
    } if text_question_ids else {}
    text_review = []
    for ta in text_answers:
        if not ta.text_question_id:
            continue
        tq = text_questions_map.get(str(ta.text_question_id.id))
        if not tq:
            continue
        text_review.append(
            {
                "question": tq,
                "answer_text": ta.answer_text,
            }
        )

    pending_text_grading = bool(text_answers) and any(getattr(ta, "score_awarded", None) is None for ta in text_answers)

    gamification = StudentGamification.objects(student_id=attempt.student_id.id).first()
    return render_template(
        "student/test_result.html",
        attempt=attempt,
        review=review,
        text_review=text_review,
        gamification=gamification,
        pending_text_grading=pending_text_grading,
    )


@student_bp.route("/results/<attempt_id>/retake/same", methods=["POST"])
@login_required
def retake_test_same_questions(attempt_id):
    attempt = Attempt.objects(id=attempt_id).first()
    if not attempt:
        return "404", 404
    if str(attempt.student_id.id) != str(current_user.id):
        flash("غير مسموح", "error")
        return redirect(url_for("student.results"))

    question_ids = _extract_attempt_question_ids(attempt)
    if not question_ids:
        flash("تعذر تجهيز إعادة الاختبار بنفس الأسئلة.", "error")
        return redirect(url_for("student.test_result", attempt_id=attempt.id))

    settings = _load_attempt_settings(attempt)
    return redirect(
        url_for(
            "student.take_test",
            test_id=attempt.test_id.id,
            count=settings.get("count", len(question_ids)),
            easy=settings.get("easy", 0),
            medium=settings.get("medium", 0),
            hard=settings.get("hard", 0),
            question_ids=",".join(question_ids),
            retake_source_id=str(attempt.id),
            retake_mode="same",
        )
    )


@student_bp.route("/results/<attempt_id>/retake/new", methods=["POST"])
@login_required
def retake_test_new_questions(attempt_id):
    attempt = Attempt.objects(id=attempt_id).first()
    if not attempt:
        return "404", 404
    if str(attempt.student_id.id) != str(current_user.id):
        flash("غير مسموح", "error")
        return redirect(url_for("student.results"))

    settings = _load_attempt_settings(attempt)
    return redirect(
        url_for(
            "student.take_test",
            test_id=attempt.test_id.id,
            count=settings.get("count", attempt.total),
            easy=settings.get("easy", 0),
            medium=settings.get("medium", 0),
            hard=settings.get("hard", 0),
            retake_source_id=str(attempt.id),
            retake_mode="new",
        )
    )


@student_bp.route("/custom-tests/new", methods=["GET", "POST"])
@login_required
def custom_test_new():
    subjects = list(Subject.objects().order_by('created_at').all())
    selected_subject_id = request.args.get("subject_id") or request.form.get("subject_id")
    if selected_subject_id and not ObjectId.is_valid(str(selected_subject_id)):
        selected_subject_id = None

    if current_user.role == "student":
        unlocked_lessons = get_unlocked_lessons(current_user.id)
    else:
        unlocked_lessons = list(Lesson.objects().all())
    
    subject_filter = None
    if selected_subject_id:
        subject_filter = Subject.objects(id=selected_subject_id).first()
        if subject_filter:
            unlocked_lessons = [
                lesson for lesson in unlocked_lessons
                if lesson.section and lesson.section.subject_id == subject_filter
            ]

    # Bulk load tests and question counts for all unlocked lessons
    lesson_question_counts = {}
    lesson_difficulty_counts = {}
    if unlocked_lessons:
        lesson_ids = [l.id for l in unlocked_lessons]
        tests = list(Test.objects(lesson_id__in=lesson_ids).all())
        
        # Group tests by lesson
        tests_by_lesson = {}
        for test in tests:
            lesson_id = test.lesson_id.id if test.lesson_id else None
            if lesson_id:
                if lesson_id not in tests_by_lesson:
                    tests_by_lesson[lesson_id] = []
                tests_by_lesson[lesson_id].append(test.id)
        
        # Bulk count questions for each lesson (total and by difficulty)
        for lesson in unlocked_lessons:
            test_ids = tests_by_lesson.get(lesson.id, [])
            if test_ids:
                lesson_questions = list(Question.objects(test_id__in=test_ids).all())
                lesson_question_counts[lesson.id] = len(lesson_questions)
                
                # Count by difficulty
                difficulty_count = {'easy': 0, 'medium': 0, 'hard': 0}
                for q in lesson_questions:
                    diff = (getattr(q, "difficulty", "medium") or "medium").lower()
                    if diff not in difficulty_count:
                        diff = "medium"
                    difficulty_count[diff] += 1
                lesson_difficulty_counts[lesson.id] = difficulty_count
            else:
                lesson_question_counts[lesson.id] = 0
                lesson_difficulty_counts[lesson.id] = {'easy': 0, 'medium': 0, 'hard': 0}
    
    total_available_questions = sum(lesson_question_counts.values())

    if request.method == "POST":
        if not selected_subject_id:
            flash("اختر مادة قبل إنشاء اختبار مخصص.", "error")
            return redirect(url_for("student.custom_test_new"))

        selections = []
        total_questions = 0
        for lesson in unlocked_lessons:
            # Check if difficulty-based selection is used for this lesson
            easy_raw = request.form.get(f"lesson_{lesson.id}_easy", "").strip()
            medium_raw = request.form.get(f"lesson_{lesson.id}_medium", "").strip()
            hard_raw = request.form.get(f"lesson_{lesson.id}_hard", "").strip()
            
            # Parse difficulty counts
            try:
                easy_count = int(easy_raw) if easy_raw else 0
                medium_count = int(medium_raw) if medium_raw else 0
                hard_count = int(hard_raw) if hard_raw else 0
            except ValueError:
                easy_count = medium_count = hard_count = 0
            
            # Total for this lesson (difficulty-based)
            difficulty_total = easy_count + medium_count + hard_count
            
            # Check legacy total count input (fallback)
            raw = request.form.get(f"lesson_{lesson.id}")
            try:
                legacy_count = int(raw) if raw else 0
            except ValueError:
                legacy_count = 0
            
            # Use difficulty-based if any difficulty is specified, otherwise use legacy
            if difficulty_total > 0:
                count = difficulty_total
                use_difficulty = True
                
                # Validate difficulty counts against available
                available_diff = lesson_difficulty_counts.get(lesson.id, {'easy': 0, 'medium': 0, 'hard': 0})
                if easy_count > available_diff['easy']:
                    flash(f"طلب {easy_count} أسئلة سهلة من {lesson.title}، لكن {available_diff['easy']} فقط متاحة.", "error")
                    return redirect(url_for("student.custom_test_new", subject_id=selected_subject_id))
                if medium_count > available_diff['medium']:
                    flash(f"طلب {medium_count} أسئلة متوسطة من {lesson.title}، لكن {available_diff['medium']} فقط متاحة.", "error")
                    return redirect(url_for("student.custom_test_new", subject_id=selected_subject_id))
                if hard_count > available_diff['hard']:
                    flash(f"طلب {hard_count} أسئلة صعبة من {lesson.title}، لكن {available_diff['hard']} فقط متاحة.", "error")
                    return redirect(url_for("student.custom_test_new", subject_id=selected_subject_id))
            elif legacy_count > 0:
                count = legacy_count
                use_difficulty = False
            else:
                continue
            
            if count <= 0:
                continue
                
            max_available = lesson_question_counts.get(lesson.id, 0)
            if count > max_available:
                flash(f"تم طلب {count} أسئلة لـ {lesson.title}، ولكن {max_available} فقط متاحة.", "error")
                return redirect(url_for("student.custom_test_new", subject_id=selected_subject_id))
            
            selection = {
                "lesson_id": str(lesson.id),
                "count": count,
            }
            
            if use_difficulty:
                selection["difficulty"] = {
                    "easy": easy_count,
                    "medium": medium_count,
                    "hard": hard_count
                }
            
            selections.append(selection)
            total_questions += count

        if total_questions == 0:
            flash("اختر درسًا واحدًا على الأقل وعدد الأسئلة.", "error")
            return redirect(url_for("student.custom_test_new", subject_id=selected_subject_id))

        if total_questions < 10:
            flash("اختر 10 أسئلة على الأقل لإنشاء اختبار مخصص.", "error")
            return redirect(url_for("student.custom_test_new", subject_id=selected_subject_id))

        if total_questions > 50:
            flash("يمكنك اختيار حتى 50 سؤالًا للاختبار المخصص.", "error")
            return redirect(url_for("student.custom_test_new", subject_id=selected_subject_id))

        # Build question pool
        selected_questions = []
        for sel in selections:
            tests = Test.objects(lesson_id=sel["lesson_id"]).all()
            test_ids = [t.id for t in tests]
            all_lesson_questions = list(Question.objects(test_id__in=test_ids)) if test_ids else []
            
            if "difficulty" in sel:
                # Use difficulty-based selection
                diff_spec = sel["difficulty"]
                level_map = {"easy": [], "medium": [], "hard": []}
                
                for q in all_lesson_questions:
                    diff = (getattr(q, "difficulty", "medium") or "medium").lower()
                    if diff not in level_map:
                        diff = "medium"
                    level_map[diff].append(q)
                
                picked = []
                if diff_spec["easy"] > 0:
                    if len(level_map["easy"]) < diff_spec["easy"]:
                        flash("لا توجد أسئلة كافية لإنشاء الاختبار.", "error")
                        return redirect(url_for("student.custom_test_new", subject_id=selected_subject_id))
                    picked.extend(random.sample(level_map["easy"], diff_spec["easy"]))
                if diff_spec["medium"] > 0:
                    if len(level_map["medium"]) < diff_spec["medium"]:
                        flash("لا توجد أسئلة كافية لإنشاء الاختبار.", "error")
                        return redirect(url_for("student.custom_test_new", subject_id=selected_subject_id))
                    picked.extend(random.sample(level_map["medium"], diff_spec["medium"]))
                if diff_spec["hard"] > 0:
                    if len(level_map["hard"]) < diff_spec["hard"]:
                        flash("لا توجد أسئلة كافية لإنشاء الاختبار.", "error")
                        return redirect(url_for("student.custom_test_new", subject_id=selected_subject_id))
                    picked.extend(random.sample(level_map["hard"], diff_spec["hard"]))
                
                selected_questions.extend(picked)
            else:
                # Legacy: random selection without difficulty filter
                lesson_questions = all_lesson_questions
                if len(lesson_questions) < sel["count"]:
                    flash("لا توجد أسئلة كافية لإنشاء الاختبار.", "error")
                    return redirect(url_for("student.custom_test_new", subject_id=selected_subject_id))
                picked = random.sample(lesson_questions, sel["count"])
                selected_questions.extend(picked)

        # Ensure no duplicates
        selected_questions = list({q.id: q for q in selected_questions}.values())

        # Shuffle question order
        random.shuffle(selected_questions)
        question_order = [str(q.id) for q in selected_questions]

        # Shuffle answer order per question
        answer_order = {}
        for q in selected_questions:
            choices = list(q.choices)
            random.shuffle(choices)
            answer_order[str(q.id)] = [str(c.choice_id) for c in choices]

        selections_payload = {
            "subject_id": str(selected_subject_id),
            "lessons": selections,
        }

        attempt = CustomTestAttempt(
            student_id=current_user.id,
            label="Custom Test",
            total=len(question_order),
            selections_json=json.dumps(selections_payload),
            question_order_json=json.dumps(question_order),
            answer_order_json=json.dumps(answer_order),
        )
        attempt.save()

        return redirect(url_for("student.custom_test_take", attempt_id=attempt.id))

    return render_template(
        "student/custom_test_setup.html",
        subjects=subjects,
        selected_subject_id=selected_subject_id,
        lessons=unlocked_lessons,
        lesson_question_counts=lesson_question_counts,
        lesson_difficulty_counts=lesson_difficulty_counts,
        total_available_questions=total_available_questions,
    )


@student_bp.route("/custom-tests/<attempt_id>")
@login_required
def custom_test_take(attempt_id):
    attempt = CustomTestAttempt.objects(id=attempt_id).first()
    if not attempt:
        return "404", 404
    if str(attempt.student_id.id) != str(current_user.id):
        flash("غير مسموح.", "error")
        return redirect(url_for("student.subjects"))

    question_order = json.loads(attempt.question_order_json)
    answer_order = json.loads(attempt.answer_order_json)
    questions = Question.objects(id__in=question_order).all()
    questions_by_id = {str(q.id): q for q in questions}

    ordered_questions = []
    for qid in question_order:
        q = questions_by_id.get(qid)
        if not q:
            continue
        ordered_choice_ids = answer_order.get(str(qid), [])
        choices = {str(c.choice_id): c for c in q.choices}
        ordered_choices = [choices[cid] for cid in ordered_choice_ids if cid in choices]
        ordered_questions.append({"question": q, "choices": ordered_choices})

    time_limit_seconds = (attempt.total * 75) + 15
    return render_template(
        "student/custom_test_take.html",
        attempt=attempt,
        ordered_questions=ordered_questions,
        time_limit_seconds=time_limit_seconds,
    )


@student_bp.route("/custom-tests/<attempt_id>/submit", methods=["POST"])
@login_required
def custom_test_submit(attempt_id):
    attempt = CustomTestAttempt.objects(id=attempt_id).first()
    if not attempt:
        return "404", 404
    if str(attempt.student_id.id) != str(current_user.id):
        flash("غير مسموح.", "error")
        return redirect(url_for("student.subjects"))
    if attempt.status != "active":
        return redirect(url_for("student.custom_test_result", attempt_id=attempt.id))

    question_order = json.loads(attempt.question_order_json)
    questions = Question.objects(id__in=question_order).all()
    questions_by_id = {str(q.id): q for q in questions}

    score = 0
    total = len(question_order)
    for qid in question_order:
        selected_choice_id = request.form.get(f"question_{qid}")
        q = questions_by_id.get(str(qid))
        choice = next((c for c in q.choices if str(c.choice_id) == selected_choice_id), None) if (q and selected_choice_id) else None
        if q and q.correct_choice_id:
            is_correct = bool(choice and choice.choice_id == q.correct_choice_id)
        else:
            is_correct = bool(choice and choice.is_correct)
        if is_correct:
            score += 1
        choice_id = choice.choice_id if choice else None
        ans = CustomTestAnswer(
            attempt_id=attempt.id,
            question_id=q,
            choice_id=choice_id,
            is_correct=is_correct,
        )
        ans.save()

    attempt.score = score
    attempt.total = total
    attempt.status = "submitted"
    earned_xp, _ = _award_xp_for_attempt(
        student_id=current_user.id,
        event_type="custom_test_submit",
        source_id=str(attempt.id),
        score=score,
        total=total,
        is_retake=bool(attempt.is_retake),
    )
    attempt.xp_earned = earned_xp
    attempt.save()
    return redirect(url_for("student.custom_test_result", attempt_id=attempt.id))


@student_bp.route("/custom-tests/<attempt_id>/result")
@login_required
def custom_test_result(attempt_id):
    attempt = CustomTestAttempt.objects(id=attempt_id).first()
    if not attempt:
        return "404", 404
    if str(attempt.student_id.id) != str(current_user.id) and current_user.role not in {"teacher", "admin"}:
        flash("غير مسموح.", "error")
        return redirect(url_for("student.subjects"))

    question_order = json.loads(attempt.question_order_json)
    answer_order = json.loads(attempt.answer_order_json)
    questions = Question.objects(id__in=question_order).all()
    questions_by_id = {str(q.id): q for q in questions}
    answers_by_qid = {str(a.question_id.id): a for a in CustomTestAnswer.objects(attempt_id=attempt.id).all()}

    review = []
    for qid in question_order:
        q = questions_by_id.get(qid)
        if not q:
            continue
        ordered_choice_ids = answer_order.get(str(qid), [])
        choices = {str(c.choice_id): c for c in q.choices}
        ordered_choices = [choices[cid] for cid in ordered_choice_ids if cid in choices]
        ans = answers_by_qid.get(str(qid))
        selected_choice = choices.get(str(ans.choice_id)) if ans and ans.choice_id else None
        correct_choice = next((c for c in q.choices if c.is_correct), None)
        review.append({
            "question": q,
            "choices": ordered_choices,
            "selected_choice": selected_choice,
            "correct_choice": correct_choice,
            "is_correct": ans.is_correct if ans else False,
        })

    gamification = StudentGamification.objects(student_id=attempt.student_id.id).first()
    return render_template("student/custom_test_result.html", attempt=attempt, review=review, gamification=gamification)


@student_bp.route("/custom-tests/<attempt_id>/retake/same", methods=["POST"])
@login_required
def custom_test_retake_same(attempt_id):
    source = CustomTestAttempt.objects(id=attempt_id).first()
    if not source:
        return "404", 404
    if str(source.student_id.id) != str(current_user.id):
        flash("غير مسموح.", "error")
        return redirect(url_for("student.results"))

    attempt = CustomTestAttempt(
        student_id=current_user.id,
        label=source.label,
        total=source.total,
        score=0,
        status="active",
        selections_json=source.selections_json,
        question_order_json=source.question_order_json,
        answer_order_json=source.answer_order_json,
        is_retake=True,
    )
    attempt.save()
    return redirect(url_for("student.custom_test_take", attempt_id=attempt.id))


@student_bp.route("/custom-tests/<attempt_id>/retake/new", methods=["POST"])
@login_required
def custom_test_retake_new(attempt_id):
    source = CustomTestAttempt.objects(id=attempt_id).first()
    if not source:
        return "404", 404
    if str(source.student_id.id) != str(current_user.id):
        flash("غير مسموح.", "error")
        return redirect(url_for("student.results"))

    try:
        selections_payload = json.loads(source.selections_json)
    except Exception:
        selections_payload = {}
    selections = selections_payload.get("lessons", []) if isinstance(selections_payload, dict) else []

    selected_questions = []
    for sel in selections:
        lesson_id = sel.get("lesson_id")
        count = _to_int(sel.get("count"), 0)
        if not lesson_id or count <= 0:
            continue

        tests = Test.objects(lesson_id=lesson_id).all()
        test_ids = [t.id for t in tests]
        all_lesson_questions = list(Question.objects(test_id__in=test_ids)) if test_ids else []

        if "difficulty" in sel and isinstance(sel["difficulty"], dict):
            diff_spec = sel["difficulty"]
            level_map = {"easy": [], "medium": [], "hard": []}
            for q in all_lesson_questions:
                diff = (getattr(q, "difficulty", "medium") or "medium").lower()
                if diff not in level_map:
                    diff = "medium"
                level_map[diff].append(q)

            picked = []
            easy_need = _to_int(diff_spec.get("easy"), 0)
            medium_need = _to_int(diff_spec.get("medium"), 0)
            hard_need = _to_int(diff_spec.get("hard"), 0)

            if easy_need > len(level_map["easy"]) or medium_need > len(level_map["medium"]) or hard_need > len(level_map["hard"]):
                flash("لا توجد أسئلة كافية لإعادة الاختبار بنفس الإعدادات.", "error")
                return redirect(url_for("student.custom_test_result", attempt_id=source.id))

            if easy_need:
                picked.extend(random.sample(level_map["easy"], easy_need))
            if medium_need:
                picked.extend(random.sample(level_map["medium"], medium_need))
            if hard_need:
                picked.extend(random.sample(level_map["hard"], hard_need))
            selected_questions.extend(picked)
        else:
            if len(all_lesson_questions) < count:
                flash("لا توجد أسئلة كافية لإعادة الاختبار بنفس الإعدادات.", "error")
                return redirect(url_for("student.custom_test_result", attempt_id=source.id))
            selected_questions.extend(random.sample(all_lesson_questions, count))

    # Keep unique questions and randomize like original custom test generation.
    selected_questions = list({q.id: q for q in selected_questions}.values())
    random.shuffle(selected_questions)
    question_order = [str(q.id) for q in selected_questions]

    answer_order = {}
    for q in selected_questions:
        choices = list(q.choices)
        random.shuffle(choices)
        answer_order[str(q.id)] = [str(c.choice_id) for c in choices]

    attempt = CustomTestAttempt(
        student_id=current_user.id,
        label=source.label,
        total=len(question_order),
        score=0,
        status="active",
        selections_json=source.selections_json,
        question_order_json=json.dumps(question_order),
        answer_order_json=json.dumps(answer_order),
        is_retake=True,
    )
    attempt.save()
    return redirect(url_for("student.custom_test_take", attempt_id=attempt.id))


@student_bp.route("/flashcards/resource/<resource_id>")
@login_required
def view_flashcards(resource_id):
    """View flashcards from a JSON resource with pagination (6 cards per page)."""
    from .models import LessonResource
    
    resource = LessonResource.objects(id=resource_id).first()
    if not resource:
        return "404", 404
    lesson = resource.lesson_id
    section = lesson.section
    
    # Access check for students
    if current_user.role == "student":
        access = AccessContext(section, current_user.id)
        if not access.lesson_open(lesson):
            flash("قم بتفعيل هذا الدرس لعرضه.", "warning")
            return redirect(url_for("student.activate_lesson", lesson_id=lesson.id))
    
    # Fetch flashcards from URL
    try:
        import requests
        
        # Helper functions for URL normalization
        def extract_drive_file_id(url):
            """Extract Google Drive file ID from various Google Drive URL formats."""
            if "/file/d/" in url:
                return url.split("/file/d/")[-1].split("/")[0]
            if "id=" in url:
                return url.split("id=")[-1].split("&")[0]
            return None

        def normalize_drive_url_for_download(url):
            """Convert Google Drive sharing links to direct download links."""
            lowered = url.lower()
            if "drive.google.com" in lowered:
                file_id = extract_drive_file_id(url)
                if file_id:
                    return f"https://drive.google.com/uc?export=download&id={file_id}"
            return url
        
        # Normalize URL if it's a Google Drive link
        fetch_url = normalize_drive_url_for_download(resource.url)
        
        response = requests.get(fetch_url, timeout=5)
        response.raise_for_status()
        data = response.json()
        flashcards = data.get("flashcards", []) if isinstance(data, dict) else data
        
        if not isinstance(flashcards, list):
            flashcards = []
    except Exception as e:
        flash(f"خطأ في تحميل البطاقات التعليمية: {str(e)}", "danger")
        flashcards = []
    
    # Pagination
    page = request.args.get("page", 1, type=int)
    cards_per_page = 6
    total_cards = len(flashcards)
    total_pages = (total_cards + cards_per_page - 1) // cards_per_page
    
    # Ensure page is valid
    if page < 1:
        page = 1
    if page > total_pages and total_pages > 0:
        page = total_pages
    
    # Get cards for current page
    start_idx = (page - 1) * cards_per_page
    end_idx = start_idx + cards_per_page
    page_cards = flashcards[start_idx:end_idx]
    
    return render_template(
        "student/flashcard_viewer.html",
        resource=resource,
        lesson=lesson,
        section=section,
        flashcards=page_cards,
        page=page,
        total_pages=total_pages,
        total_cards=total_cards,
    )
