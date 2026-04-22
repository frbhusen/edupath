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
from bson.dbref import DBRef
from mongoengine.errors import DoesNotExist

from .models import User, Subject, Section, Lesson, Test, TestResource, Question, Choice, Attempt, AttemptAnswer, TestInteractiveQuestion, AttemptInteractiveAnswer, ActivationCode, SectionActivation, SubjectActivation, SubjectActivationCode, CustomTestAttempt, CustomTestAnswer, StudentGamification, XPEvent, LessonCompletion, Assignment, AssignmentSubmission, AssignmentAttempt, StudyPlan, StudyPlanItem, DiscussionQuestion, DiscussionAnswer, Certificate, Duel, DuelAnswer, DuelStats, CourseSet, CourseQuestion, CourseAttempt, CourseAnswer, StudentFavoriteQuestion
from .forms import ActivationForm, StudentProfileForm
from .activation_utils import cascade_subject_activation, cascade_section_activation
from .extensions import cache

student_bp = Blueprint("student", __name__, template_folder="templates")

DUEL_INVITE_COOLDOWN_SECONDS = 60
DUEL_SAME_OPPONENT_COOLDOWN_SECONDS = 180
DUEL_PENDING_LIMIT_PER_CHALLENGER = 3
DUEL_WAITING_JOIN_TIMEOUT_SECONDS = 300
DUEL_PAIR_RECENT_LOCK_SECONDS = 45
TEST_EXIT_XP_PENALTY = 25


def _duel_role_allowed(user) -> bool:
    role = (getattr(user, "role", "") or "").lower()
    return role in {"student", "admin"}


def _redirect_to_next_or(default_endpoint, **default_kwargs):
    next_path = (request.form.get("next") or "").strip()
    if next_path.startswith("/"):
        return redirect(next_path)
    return redirect(url_for(default_endpoint, **default_kwargs))


def _viewer_cache_tag():
    if current_user.is_authenticated:
        role = (getattr(current_user, "role", "") or "auth").lower()
        return f"{current_user.id}_{role}"
    return "anon"


def _first_lesson_id_for_subject(subject_id):
    if not subject_id:
        return None
    first_section = Section.objects(subject_id=subject_id).order_by("created_at", "id").first()
    if not first_section:
        return None
    first_lesson = Lesson.objects(section_id=first_section.id).order_by("created_at", "id").first()
    return first_lesson.id if first_lesson else None


def _is_guest_free_lesson(lesson):
    if not lesson:
        return False
    try:
        section = lesson.section
    except (DoesNotExist, AttributeError):
        return False
    if not section:
        return False
    try:
        subject = section.subject
    except (DoesNotExist, AttributeError):
        return False
    if not subject:
        return False
    first_lesson_id = _first_lesson_id_for_subject(subject.id)
    return bool(first_lesson_id and lesson.id == first_lesson_id)


@student_bp.route("/favorites", methods=["GET"])
@login_required
@cache.cached(timeout=30, key_prefix=lambda: f"favorites_{current_user.id}")
def favorites():
    if (current_user.role or "").lower() != "student":
        flash("غير مسموح.", "error")
        return redirect(url_for("index"))

    favorites_rows = list(
        StudentFavoriteQuestion.objects(student_id=current_user.id)
        .order_by("-created_at")
        .all()
    )
    return render_template("student/favorites.html", favorites=favorites_rows)


@student_bp.route("/favorites/add", methods=["POST"])
@login_required
def add_favorite():
    if (current_user.role or "").lower() != "student":
        flash("غير مسموح.", "error")
        return _redirect_to_next_or("index")

    question_type = (request.form.get("question_type") or "").strip().lower()
    item_id = (request.form.get("question_id") or "").strip()
    if question_type not in {"mcq", "interactive"} or not ObjectId.is_valid(item_id):
        flash("بيانات السؤال غير صحيحة.", "error")
        return _redirect_to_next_or("student.favorites")

    if question_type == "mcq":
        question = Question.objects(id=item_id).first()
        if not question:
            flash("السؤال غير موجود.", "error")
            return _redirect_to_next_or("student.favorites")

        exists = StudentFavoriteQuestion.objects(
            student_id=current_user.id,
            question_type="mcq",
            question_id=question.id,
        ).first()
        if exists:
            flash("السؤال موجود بالفعل في المفضلة.", "info")
            return _redirect_to_next_or("student.favorites")

        correct_choice = next((c for c in question.choices if c.is_correct), None)
        snapshot_choices = [
            Choice(text=c.text, image_url=c.image_url, is_correct=bool(c.is_correct))
            for c in question.choices
        ]
        StudentFavoriteQuestion(
            student_id=current_user.id,
            question_type="mcq",
            question_id=question.id,
            question_text=question.text,
            question_images=list(question.question_images or []),
            choices=snapshot_choices,
            correct_answer_text=correct_choice.text if correct_choice else None,
            correct_answer_image_url=correct_choice.image_url if correct_choice else None,
            difficulty=(question.difficulty or "medium"),
        ).save()
        cache.delete(f"favorites_{current_user.id}")
        flash("تمت إضافة السؤال إلى المفضلة.", "success")
        return _redirect_to_next_or("student.favorites")

    interactive_question = TestInteractiveQuestion.objects(id=item_id).first()
    if not interactive_question:
        flash("السؤال التفاعلي غير موجود.", "error")
        return _redirect_to_next_or("student.favorites")

    exists = StudentFavoriteQuestion.objects(
        student_id=current_user.id,
        question_type="interactive",
        interactive_question_id=interactive_question.id,
    ).first()
    if exists:
        flash("السؤال التفاعلي موجود بالفعل في المفضلة.", "info")
        return _redirect_to_next_or("student.favorites")

    StudentFavoriteQuestion(
        student_id=current_user.id,
        question_type="interactive",
        interactive_question_id=interactive_question.id,
        question_text=interactive_question.question_text,
        question_images=[interactive_question.question_image_url] if interactive_question.question_image_url else [],
        choices=[],
        correct_answer_text=interactive_question.answer_text,
        correct_answer_image_url=interactive_question.answer_image_url,
        difficulty=(interactive_question.difficulty or "medium"),
    ).save()
    cache.delete(f"favorites_{current_user.id}")
    flash("تمت إضافة السؤال التفاعلي إلى المفضلة.", "success")
    return _redirect_to_next_or("student.favorites")


@student_bp.route("/favorites/<favorite_id>/delete", methods=["POST"])
@login_required
def remove_favorite(favorite_id):
    if (current_user.role or "").lower() != "student":
        flash("غير مسموح.", "error")
        return _redirect_to_next_or("index")

    if not ObjectId.is_valid(favorite_id):
        flash("العنصر غير موجود.", "error")
        return _redirect_to_next_or("student.favorites")

    row = StudentFavoriteQuestion.objects(id=favorite_id, student_id=current_user.id).first()
    if not row:
        flash("العنصر غير موجود.", "error")
        return _redirect_to_next_or("student.favorites")

    row.delete()
    cache.delete(f"favorites_{current_user.id}")
    flash("تم حذف السؤال من المفضلة.", "success")
    return _redirect_to_next_or("student.favorites")


@student_bp.route("/favorites/toggle", methods=["POST"])
@login_required
def toggle_favorite():
    if (current_user.role or "").lower() != "student":
        return jsonify({"ok": False, "message": "غير مسموح."}), 403

    payload = request.get_json(silent=True) or {}
    question_type = (payload.get("question_type") or request.form.get("question_type") or "").strip().lower()
    item_id = (payload.get("question_id") or request.form.get("question_id") or "").strip()

    if question_type not in {"mcq", "interactive"} or not ObjectId.is_valid(item_id):
        return jsonify({"ok": False, "message": "بيانات السؤال غير صحيحة."}), 400

    if question_type == "mcq":
        exists = StudentFavoriteQuestion.objects(
            student_id=current_user.id,
            question_type="mcq",
            question_id=item_id,
        ).first()
        if exists:
            exists.delete()
            cache.delete(f"favorites_{current_user.id}")
            return jsonify({"ok": True, "is_favorite": False, "favorite_id": None})

        question = Question.objects(id=item_id).first()
        if not question:
            return jsonify({"ok": False, "message": "السؤال غير موجود."}), 404

        correct_choice = next((c for c in question.choices if c.is_correct), None)
        snapshot_choices = [
            Choice(text=c.text, image_url=c.image_url, is_correct=bool(c.is_correct))
            for c in question.choices
        ]
        favorite = StudentFavoriteQuestion(
            student_id=current_user.id,
            question_type="mcq",
            question_id=question.id,
            question_text=question.text,
            question_images=list(question.question_images or []),
            choices=snapshot_choices,
            correct_answer_text=correct_choice.text if correct_choice else None,
            correct_answer_image_url=correct_choice.image_url if correct_choice else None,
            difficulty=(question.difficulty or "medium"),
        )
        favorite.save()
        cache.delete(f"favorites_{current_user.id}")
        return jsonify({"ok": True, "is_favorite": True, "favorite_id": str(favorite.id)})

    exists = StudentFavoriteQuestion.objects(
        student_id=current_user.id,
        question_type="interactive",
        interactive_question_id=item_id,
    ).first()
    if exists:
        exists.delete()
        cache.delete(f"favorites_{current_user.id}")
        return jsonify({"ok": True, "is_favorite": False, "favorite_id": None})

    interactive_question = TestInteractiveQuestion.objects(id=item_id).first()
    if not interactive_question:
        return jsonify({"ok": False, "message": "السؤال التفاعلي غير موجود."}), 404

    favorite = StudentFavoriteQuestion(
        student_id=current_user.id,
        question_type="interactive",
        interactive_question_id=interactive_question.id,
        question_text=interactive_question.question_text,
        question_images=[interactive_question.question_image_url] if interactive_question.question_image_url else [],
        choices=[],
        correct_answer_text=interactive_question.answer_text,
        correct_answer_image_url=interactive_question.answer_image_url,
        difficulty=(interactive_question.difficulty or "medium"),
    )
    favorite.save()
    cache.delete(f"favorites_{current_user.id}")
    return jsonify({"ok": True, "is_favorite": True, "favorite_id": str(favorite.id)})


class AccessContext:
    """Per-student access computation for a section with three-level hierarchy: Subject → Section → Lesson."""

    def __init__(self, section: Section, student_id: int):
        self.section = section
        self.student_id = student_id
        try:
            self.subject = section.subject
        except (DoesNotExist, AttributeError):
            self.subject = None
        
        # Check if entire subject is activated
        self.subject_requires_code = bool(getattr(self.subject, "requires_code", False)) if self.subject else False
        self.subject_active = bool(
            SubjectActivation.objects(subject_id=self.subject.id, student_id=student_id, active=True).first()
        ) if self.subject else False
        self.subject_open = self.subject_active or not self.subject_requires_code
        
        # Check if section is activated
        self.section_requires_code = section.requires_code
        self.section_active = bool(
            SectionActivation.objects(section_id=section.id, student_id=student_id, active=True).first()
        )
        
        # Section unlock precedence:
        # - If section is activated OR does not require code, it is open
        #   regardless of subject lock state.
        self.section_open = self.section_active or not self.section_requires_code

        # Always-open rule: first lesson of first section in the subject is never locked.
        self.first_lesson_id = None
        if self.subject:
            first_section = Section.objects(subject_id=self.subject.id).order_by("created_at", "id").first()
            if first_section:
                first_lesson = Lesson.objects(section_id=first_section.id).order_by("created_at", "id").first()
                if first_lesson:
                    self.first_lesson_id = first_lesson.id
        self.first_section_wide_test_id = None

    def lesson_open(self, lesson: Lesson) -> bool:
        """Check if student can access this lesson."""
        # First lesson in the first section of a subject is always unlocked.
        if self.first_lesson_id and lesson.id == self.first_lesson_id:
            return True

        # Open section should open all lessons in that section.
        if self.section_open:
            return True

        # Keep legacy behavior: subject activation opens all content.
        if self.subject_active:
            return True

        return False

    def test_open(self, test: Test) -> bool:
        """Check if student can access this test.
        
        Logic:
        - Subject activated → all tests accessible
        - Section activated → all tests in section accessible (lesson + section-wide)
        - Lesson activated → only tests linked to that lesson accessible
        - Section-wide tests: require section or subject activation
        """
        # Lesson-linked tests follow lesson access
        if test.lesson_id:
            lesson = test.lesson
            return bool(lesson and self.lesson_open(lesson))

        # Section-wide tests follow section access first.
        if self.section_open:
            return True

        # Keep legacy behavior: subject activation opens all content.
        if self.subject_active:
            return True

        return False


def _to_int(value, default=0):
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _rebalance_difficulty_request(requested_counts, available_counts):
    levels = ("easy", "medium", "hard")
    borrow_order = {
        "easy": ("medium", "hard"),
        "medium": ("easy", "hard"),
        "hard": ("medium", "easy"),
    }

    requested = {lvl: max(0, _to_int(requested_counts.get(lvl), 0)) for lvl in levels}
    remaining = {lvl: max(0, _to_int(available_counts.get(lvl), 0)) for lvl in levels}
    allocated = {lvl: 0 for lvl in levels}

    for lvl in levels:
        need = requested[lvl]
        direct = min(need, remaining[lvl])
        allocated[lvl] += direct
        remaining[lvl] -= direct
        need -= direct

        if need <= 0:
            continue

        for donor in borrow_order[lvl]:
            if need <= 0:
                break
            take = min(need, remaining[donor])
            allocated[donor] += take
            remaining[donor] -= take
            need -= take

    return allocated


def _pack_custom_item_token(item_type, item_id):
    return f"{item_type}:{item_id}"


def _unpack_custom_item_token(token):
    raw = str(token or "").strip()
    if not raw:
        return None, None
    if ":" in raw:
        item_type, item_id = raw.split(":", 1)
        item_type = item_type.strip().lower()
        item_id = item_id.strip()
        if item_type in {"mcq", "interactive"} and ObjectId.is_valid(item_id):
            return item_type, item_id
        return None, None
    # Backward compatibility: old custom attempts stored only MCQ ids.
    if ObjectId.is_valid(raw):
        return "mcq", raw
    return None, None


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

    # استخراج معرّفات أي حساب ليس طالباً (معلمين، مدراء، الخ)
    non_student_ids = [u.id for u in User.objects(role__ne="student").only("id").all()]

    events_coll = XPEvent._get_collection()
    match_stage = {"created_at": {"$gte": start_dt}}
    
    # استثناء المعلمين والمدراء من التجميع
    if non_student_ids:
        match_stage["student_id"] = {"$nin": non_student_ids}

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

    # استخراج معرّفات الموظفين لاستثنائهم
    non_student_ids = [u.id for u in User.objects(role__ne="student").only("id").all()]

    if scope == "all":
        total_users = StudentGamification.objects(student_id__nin=non_student_ids).count()
        total_pages = max(1, math.ceil(total_users / per_page)) if total_users else 1
        if page > total_pages:
            page = total_pages

        start_rank = ((page - 1) * per_page) + 1
        profiles = list(
            StudentGamification.objects(student_id__nin=non_student_ids)
            .order_by("-xp_total", "student_id")
            .skip((page - 1) * per_page)
            .limit(per_page)
            .all()
        )

        student_ids = []
        valid_profiles = []
        for p in profiles:
            try:
                if p.student_id:
                    student_ids.append(p.student_id.id)
                    valid_profiles.append(p)
            except DoesNotExist:
                pass

        users = User.objects(id__in=student_ids).only("id", "username", "first_name", "last_name").all() if student_ids else []
        users_by_id = {u.id: u for u in users}
        cert_counts = _certificate_counts_for_students(student_ids)

        entries = []
        for i, profile in enumerate(valid_profiles):
            try:
                user = users_by_id.get(profile.student_id.id) if profile.student_id else None
                sid = profile.student_id.id if profile.student_id else None
            except DoesNotExist:
                continue
            entries.append(
                _serialize_leaderboard_entry(
                    profile,
                    user,
                    start_rank + len(entries),
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
        profiles_by_student_id = {}
        for p in profiles:
            try:
                if p.student_id:
                    profiles_by_student_id[p.student_id.id] = p
            except DoesNotExist:
                pass

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
                    rank=start_rank + len(entries),
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

    non_student_ids = [u.id for u in User.objects(role__ne="student").only("id").all()]

    if scope == "all":
        profile = StudentGamification.objects(student_id=student_id).first()
        if not profile:
            return None

        higher_count = StudentGamification.objects(
            student_id__nin=non_student_ids,
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
    match_stage = {"created_at": {"$gte": start_dt}}
    if non_student_ids:
        match_stage["student_id"] = {"$nin": non_student_ids}

    pipeline = [
        {"$match": match_stage},
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
        try:
            section_ref = lesson.section_id
        except Exception:
            # Skip orphan lessons with broken section references.
            continue
        if not section_ref:
            continue
        section_id = getattr(section_ref, "id", None)
        if not section_id:
            continue
        if section_id not in lessons_by_section:
            lessons_by_section[section_id] = []
        lessons_by_section[section_id].append(lesson)
    
    unlocked = []
    for section in sections:
        # Skip orphan sections whose subject reference no longer exists.
        try:
            _ = section.subject
        except (DoesNotExist, AttributeError):
            continue

        section_lessons = lessons_by_section.get(section.id, [])
        if not section_lessons:
            continue

        try:
            access = AccessContext(section, student_id)
        except (DoesNotExist, AttributeError):
            # Any dangling refs in section hierarchy should not break page load.
            continue

        for lesson in section_lessons:
            if access.lesson_open(lesson):
                unlocked.append(lesson)
    
    return unlocked


@student_bp.before_request
def student_duel_maintenance():
    if not current_user.is_authenticated:
        return None
    if not _duel_role_allowed(current_user):
        return None

    try:
        _duel_maintenance_tick_for_student(current_user.id)
    except Exception:
        # Maintenance should never block user flows.
        return None
    return None

@student_bp.route("/subjects")
@cache.cached(timeout=60, key_prefix=lambda: f"subjects_{_viewer_cache_tag()}")
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
    if current_user.is_authenticated and (current_user.role or "").lower() == "student":
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


@student_bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if (current_user.role or "").lower() != "student":
        flash("غير مسموح.", "error")
        return redirect(url_for("index"))

    form = StudentProfileForm()

    def _load_activated_lists():
        subject_activations = list(
            SubjectActivation.objects(student_id=current_user.id, active=True)
            .order_by("-activated_at")
            .all()
        )
        activated_subjects_local = []
        seen_subject_ids = set()
        for row in subject_activations:
            subject = row.subject_id
            if not subject:
                continue
            sid = str(subject.id)
            if sid in seen_subject_ids:
                continue
            seen_subject_ids.add(sid)
            activated_subjects_local.append({"subject": subject, "activated_at": row.activated_at})

        section_activations = list(
            SectionActivation.objects(student_id=current_user.id, active=True)
            .order_by("-activated_at")
            .all()
        )
        activated_sections_local = []
        seen_section_ids = set()
        for row in section_activations:
            section = row.section_id
            if not section:
                continue
            sec_id = str(section.id)
            if sec_id in seen_section_ids:
                continue
            seen_section_ids.add(sec_id)
            activated_sections_local.append(
                {
                    "section": section,
                    "subject": section.subject_id,
                    "activated_at": row.activated_at,
                }
            )

        return activated_subjects_local, activated_sections_local

    if request.method == "GET":
        form.first_name.data = current_user.first_name
        form.last_name.data = current_user.last_name
        form.username.data = current_user.username
        form.phone.data = current_user.phone

    if form.validate_on_submit():
        existing_username = User.objects(username=form.username.data).first()
        if existing_username and str(existing_username.id) != str(current_user.id):
            flash("اسم المستخدم مستخدم مسبقاً.", "error")
            activated_subjects, activated_sections = _load_activated_lists()
            return render_template(
                "student/profile.html",
                form=form,
                activated_subjects=activated_subjects,
                activated_sections=activated_sections,
            )

        existing_phone = User.objects(phone=form.phone.data).first()
        if existing_phone and str(existing_phone.id) != str(current_user.id):
            flash("رقم الهاتف مستخدم مسبقاً.", "error")
            activated_subjects, activated_sections = _load_activated_lists()
            return render_template(
                "student/profile.html",
                form=form,
                activated_subjects=activated_subjects,
                activated_sections=activated_sections,
            )

        new_password = (form.new_password.data or "").strip()
        current_password = form.current_password.data or ""
        password_changed = False

        if current_password and not new_password:
            flash("أدخل كلمة مرور جديدة لتغيير كلمة المرور.", "error")
            activated_subjects, activated_sections = _load_activated_lists()
            return render_template(
                "student/profile.html",
                form=form,
                activated_subjects=activated_subjects,
                activated_sections=activated_sections,
            )

        if new_password:
            if not current_password:
                flash("للتعديل على كلمة المرور يجب إدخال كلمة المرور الحالية.", "error")
                activated_subjects, activated_sections = _load_activated_lists()
                return render_template(
                    "student/profile.html",
                    form=form,
                    activated_subjects=activated_subjects,
                    activated_sections=activated_sections,
                )
            if not current_user.check_password(current_password):
                flash("كلمة المرور الحالية غير صحيحة.", "error")
                activated_subjects, activated_sections = _load_activated_lists()
                return render_template(
                    "student/profile.html",
                    form=form,
                    activated_subjects=activated_subjects,
                    activated_sections=activated_sections,
                )
            current_user.set_password(new_password)
            password_changed = True

        current_user.first_name = form.first_name.data
        current_user.last_name = form.last_name.data
        current_user.username = form.username.data
        current_user.phone = form.phone.data

        if password_changed:
            new_token = secrets.token_hex(16)
            current_user.current_session_token = new_token
            session["session_token"] = new_token
            session.modified = True

        current_user.save()
        flash("تم تحديث بياناتك بنجاح.", "success")
        return redirect(url_for("student.profile"))

    activated_subjects, activated_sections = _load_activated_lists()

    return render_template(
        "student/profile.html",
        form=form,
        activated_subjects=activated_subjects,
        activated_sections=activated_sections,
    )

@student_bp.route("/subjects/<subject_id>")
@cache.cached(
    timeout=60,
    key_prefix=lambda: f"subject_detail_{request.view_args.get('subject_id', '')}_{_viewer_cache_tag()}",
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
    
    if current_user.is_authenticated and (current_user.role or "").lower() == "student":
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

    # Subject-wide sets (no lesson/section binding) follow subject lock state.
    subject_requires_code = bool(getattr(course_set.subject_id, "requires_code", False)) if course_set.subject_id else False
    subject_active = bool(
        SubjectActivation.objects(subject_id=course_set.subject_id.id, student_id=student_id, active=True).first()
    ) if course_set.subject_id else False
    if (not subject_active) and subject_requires_code:
        return False

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
        q_type = (getattr(q, "question_type", "interactive") or "interactive").strip().lower()

        if q_type == "mcq":
            selected_raw = (request.form.get(f"question_{q.id}") or "").strip()
            selected_choice = next(
                (c for c in (q.choices or []) if str(getattr(c, "choice_id", "")) == selected_raw),
                None,
            )
            is_correct = bool(selected_choice and selected_choice.is_correct)
            if is_correct:
                score += 1

            CourseAnswer(
                attempt_id=attempt.id,
                question_id=q.id,
                choice_id=(selected_choice.choice_id if selected_choice else None),
                selected_value=None,
                is_correct=is_correct,
            ).save()
            continue

        raw = (request.form.get(f"question_{q.id}") or "").strip().lower()
        selected_value = raw == "true"
        # Self-evaluation mode: student marks if their own solution was correct.
        is_correct = bool(selected_value)
        if is_correct:
            score += 1

        CourseAnswer(
            attempt_id=attempt.id,
            question_id=q.id,
            choice_id=None,
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
@cache.cached(
    timeout=60,
    key_prefix=lambda: f"section_detail_{request.view_args.get('section_id', '')}_{_viewer_cache_tag()}",
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
        interactive_questions = list(TestInteractiveQuestion.objects(test_id__in=test_ids).only('test_id').all())
        test_resources_rows = list(TestResource.objects(test_id__in=test_ids).order_by('position').all())
        question_counts = {}
        resources_by_test = {}
        for q in questions:
            test_id = q.test_id.id
            question_counts[test_id] = question_counts.get(test_id, 0) + 1
        for iq in interactive_questions:
            if not iq.test_id:
                continue
            test_id = iq.test_id.id
            question_counts[test_id] = question_counts.get(test_id, 0) + 1
        for res in test_resources_rows:
            if not res.test_id:
                continue
            test_id = res.test_id.id
            resources_by_test.setdefault(test_id, []).append(res)
        
        # Attach counts to tests
        for test in tests:
            test._question_count = question_counts.get(test.id, 0)
            test._resources = resources_by_test.get(test.id, [])
    
    if current_user.is_authenticated and (current_user.role or "").lower() == "student":
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
    if not current_user.is_authenticated:
        subject_id = None
        try:
            subject_ref = section.subject
            subject_id = subject_ref.id if subject_ref else None
        except (DoesNotExist, AttributeError):
            subject_id = None

        guest_free_lesson_id = _first_lesson_id_for_subject(subject_id)
        lessons_data = [
            {
                "lesson": lesson,
                "is_open": bool(guest_free_lesson_id and lesson.id == guest_free_lesson_id),
                "is_completed": False,
            }
            for lesson in lessons
        ]
        tests_data = [{"test": test, "is_open": False} for test in tests]
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
def lesson_detail(lesson_id):
    lesson = Lesson.objects(id=lesson_id).first()
    if not lesson:
        return "404", 404
    section = lesson.section
    subject_open = True
    section_open = True
    if not current_user.is_authenticated:
        if not _is_guest_free_lesson(lesson):
            flash("هذا الدرس يتطلب تسجيل الدخول أو التفعيل.", "warning")
            return redirect(url_for("auth.login", next=request.path))
    elif (current_user.role or "").lower() == "student":
        access = AccessContext(section, current_user.id)
        subject_open = access.subject_open
        section_open = access.section_open
        if not access.lesson_open(lesson):
            if access.subject_requires_code and not access.subject_open:
                flash("قم بتفعيل المادة للوصول إلى الدروس.", "warning")
                return redirect(url_for("student.activate_subject", subject_id=section.subject.id))
            flash("قم بتفعيل القسم للوصول إلى الدروس.", "warning")
            return redirect(url_for("student.activate_section", section_id=section.id))
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
        interactive_questions = list(TestInteractiveQuestion.objects(test_id__in=test_ids).only('test_id').all())
        test_resources_rows = list(TestResource.objects(test_id__in=test_ids).order_by('position').all())
        question_counts = {}
        resources_by_test = {}
        for q in questions:
            test_id = q.test_id.id
            question_counts[test_id] = question_counts.get(test_id, 0) + 1
        for iq in interactive_questions:
            if not iq.test_id:
                continue
            test_id = iq.test_id.id
            question_counts[test_id] = question_counts.get(test_id, 0) + 1
        for res in test_resources_rows:
            if not res.test_id:
                continue
            test_id = res.test_id.id
            resources_by_test.setdefault(test_id, []).append(res)
        
        for test in tests:
            test._question_count = question_counts.get(test.id, 0)
            test._resources = resources_by_test.get(test.id, [])
    
    is_completed = False
    lesson_full_custom_test_enabled = bool(getattr(lesson, "allow_full_lesson_test", False))
    lesson_completion_xp = max(0, int(getattr(lesson, "xp_reward", 10) or 10))

    if current_user.is_authenticated and (current_user.role or "").lower() == "student":
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
    elif not current_user.is_authenticated:
        tests_data = [{"test": test, "is_open": False} for test in tests]
    else:
        tests_data = [{"test": t, "is_open": True} for t in tests]

    certificate = None
    if current_user.is_authenticated and (current_user.role or "").lower() == "student":
        certificate = Certificate.objects(student_id=current_user.id, lesson_id=lesson.id).first()

    return render_template(
        "student/lesson_detail.html",
        lesson=lesson,
        section=section,
        subject_open=subject_open,
        section_open=section_open,
        resources=resources,
        tests_data=tests_data,
        lesson_full_custom_test_enabled=lesson_full_custom_test_enabled,
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
            if access.subject_requires_code and not access.subject_open:
                flash("قم بتفعيل المادة للوصول إلى المناقشة.", "warning")
                return redirect(url_for("student.activate_subject", subject_id=lesson.section.subject.id))
            flash("قم بتفعيل القسم للوصول إلى المناقشة.", "warning")
            return redirect(url_for("student.activate_section", section_id=lesson.section.id))

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
@cache.cached(timeout=60, key_prefix=lambda: f"certificates_{current_user.id}")
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
        if access.subject_requires_code and not access.subject_open:
            flash("قم بتفعيل المادة أولاً.", "warning")
            return redirect(url_for("student.activate_subject", subject_id=lesson.section.subject.id))
        flash("قم بتفعيل القسم أولاً.", "warning")
        return redirect(url_for("student.activate_section", section_id=lesson.section.id))

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

    flash(f"أحسنت! تم إنهاء الدرس وربحت {earned_xp} جوهرة.", "success")
    return redirect(url_for("student.lesson_detail", lesson_id=lesson.id))


@student_bp.route("/leaderboard")
@login_required
@cache.cached(timeout=20, key_prefix=lambda: f"leaderboard_{current_user.id}_{request.args.get('scope', 'global')}_{request.args.get('page', 1)}_{request.args.get('per_page', 20)}")
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
@cache.cached(timeout=60, key_prefix=lambda: f"leaderboard_certs_{request.view_args.get('student_id', '')}")
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
    if not _duel_role_allowed(current_user):
        flash("صفحة التحديات متاحة للطلاب والإداريين فقط.", "error")
        return redirect(url_for("index"))

    created_duel = None
    share_link = None
    whatsapp_share = None
    telegram_share = None

    if (current_user.role or "").lower() == "student":
        unlocked_lessons = get_unlocked_lessons(current_user.id)
    else:
        unlocked_lessons = list(Lesson.objects().all())
    lesson_groups = []
    section_ids = []
    for lesson in unlocked_lessons:
        try:
            section_ref = lesson.section_id
            section_id = section_ref.id if section_ref else None
        except (DoesNotExist, AttributeError):
            section_id = None
        if section_id:
            section_ids.append(section_id)
    section_ids = list(set(section_ids))
    subject_ids = []
    if section_ids:
        sections = list(Section.objects(id__in=section_ids).only("id", "subject_id", "title").all())
        subject_ids_set = set()
        for section in sections:
            try:
                subject_ref = section.subject_id
                subject_id = subject_ref.id if subject_ref else None
            except (DoesNotExist, AttributeError):
                subject_id = None
            if subject_id:
                subject_ids_set.add(subject_id)
        subject_ids = list(subject_ids_set)
    else:
        sections = []
    subjects = list(Subject.objects(id__in=subject_ids).only("id", "name").all()) if subject_ids else []

    sections_by_id = {section.id: section for section in sections}
    subjects_by_id = {subject.id: subject for subject in subjects}
    lessons_by_subject = {}
    for lesson in unlocked_lessons:
        try:
            section_id = lesson.section_id.id if lesson.section_id else None
        except (DoesNotExist, AttributeError):
            section_id = None
        if not section_id:
            continue
        section = sections_by_id.get(section_id)
        if not section:
            continue
        try:
            subject_ref = section.subject_id
            subject_id = subject_ref.id if subject_ref else None
        except (DoesNotExist, AttributeError):
            subject_id = None
        if not subject_id:
            continue
        subject = subjects_by_id.get(subject_id)
        if not subject:
            continue
        lessons_by_subject.setdefault(subject_id, {})
        lessons_by_subject[subject_id].setdefault(section_id, [])
        lessons_by_subject[subject_id][section_id].append({
            "id": str(lesson.id),
            "label": lesson.title,
        })

    for subject in sorted(subjects, key=lambda item: (item.name or "")):
        section_groups = []
        subject_sections = []
        for section in sections:
            try:
                subject_ref = section.subject_id
                sid = subject_ref.id if subject_ref else None
            except (DoesNotExist, AttributeError):
                sid = None
            if sid == subject.id:
                subject_sections.append(section)
        for section in sorted(subject_sections, key=lambda item: (item.title or "")):
            lesson_rows = lessons_by_subject.get(subject.id, {}).get(section.id, [])
            if not lesson_rows:
                continue
            section_groups.append({
                "id": str(section.id),
                "label": section.title,
                "lessons": sorted(lesson_rows, key=lambda item: (item["label"] or "")),
            })
        if section_groups:
            lesson_groups.append({
                "id": str(subject.id),
                "label": subject.name,
                "sections": section_groups,
            })

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

        opponent = (
            User.objects(username=opponent_username, role__in=["student", "admin"]).first()
            if opponent_username
            else None
        )
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
            flash("يلزم أن يمتلك كل لاعب 20 جوهرة على الأقل لبدء التحدي.", "error")
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
        duel._challenger_name = _duel_safe_user_name(getattr(duel, "challenger_id", None), "لاعب")
        opponent_user = _duel_safe_user(getattr(duel, "opponent_id", None))
        duel._opponent_name = (opponent_user.username if opponent_user else None) or (getattr(duel, "opponent_username_snapshot", None) or "لاعب")

    non_student_ids = [u.id for u in User.objects(role__ne="student").only("id").all()]
    stats_top = list(DuelStats.objects(student_id__nin=non_student_ids).order_by("-wins", "-current_win_streak", "student_id").limit(10).all())
    top_ids = []
    for s in stats_top:
        sid = _duel_safe_user_id(getattr(s, "student_id", None))
        if sid:
            top_ids.append(sid)
    top_users = User.objects(id__in=top_ids).only("id", "username", "first_name", "last_name").all() if top_ids else []
    top_users_by_id = {u.id: u for u in top_users}
    leaderboard_rows = []
    for row in stats_top:
        sid = _duel_safe_user_id(getattr(row, "student_id", None))
        user = top_users_by_id.get(sid) if sid else None
        leaderboard_rows.append(
            {
                "wins": int(getattr(row, "wins", 0) or 0),
                "losses": int(getattr(row, "losses", 0) or 0),
                "streak": int(getattr(row, "current_win_streak", 0) or 0),
                "display_name": user.full_name if user else "لاعب",
            }
        )

    scope_choices = {
        "lesson_groups": lesson_groups,
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
        leaderboard_rows=leaderboard_rows,
        top_users_by_id=top_users_by_id,
        users_by_id=top_users_by_id,
    )


@student_bp.route("/duels/pending-popup", methods=["GET"])
@login_required
def duels_pending_popup():
    if not _duel_role_allowed(current_user):
        return jsonify({"ok": True, "invites": []})

    rows = list(
        Duel.objects(opponent_id=current_user.id, status="pending")
        .only("id", "invite_token", "scope_title", "expires_at", "challenger_id", "created_at")
        .no_dereference()
        .order_by("-created_at")
        .limit(3)
        .all()
    )

    def _ref_id(value):
        if not value:
            return None
        if isinstance(value, ObjectId):
            return value
        if hasattr(value, "id"):
            return value.id
        maybe = getattr(value, "$id", None)
        if maybe:
            return maybe
        return None

    challenger_ids = [cid for cid in (_ref_id(getattr(r, "challenger_id", None)) for r in rows) if cid]
    challengers = User.objects(id__in=challenger_ids).only("id", "first_name", "last_name", "username").all() if challenger_ids else []
    challenger_map = {u.id: u.full_name for u in challengers}

    payload = []
    for duel in rows:
        if _duel_expire_if_needed(duel):
            continue
        challenger_name = challenger_map.get(_ref_id(getattr(duel, "challenger_id", None)), "لاعب")
        payload.append(
            {
                "id": str(duel.id),
                "token": duel.invite_token,
                "from": challenger_name,
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
    challenger_name = _duel_safe_user_name(getattr(duel, "challenger_id", None), "لاعب")
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
        flash("تم رفض الدعوة بدون أي خصم جواهر.", "info")
        return redirect(url_for("student.duels_home"))

    if action != "accept":
        flash("إجراء غير صالح.", "error")
        return redirect(url_for("student.duel_invite", token=duel.invite_token))

    challenger_id = _duel_safe_user_id(getattr(duel, "challenger_id", None))
    opponent_id = _duel_safe_user_id(getattr(duel, "opponent_id", None))
    if not challenger_id or not opponent_id:
        flash("تعذر معالجة الدعوة بسبب بيانات مستخدم ناقصة.", "error")
        return redirect(url_for("student.duels_home"))

    challenger_profile = _get_or_create_gamification_profile(challenger_id)
    opponent_profile = _get_or_create_gamification_profile(opponent_id)
    fee = int(duel.entry_fee_xp or 20)
    if int(challenger_profile.xp_total or 0) < fee or int(opponent_profile.xp_total or 0) < fee:
        flash("لا يمكن قبول الدعوة: أحد اللاعبين لا يملك جواهر كافية.", "error")
        return redirect(url_for("student.duels_home"))

    _duel_apply_xp_delta_once(challenger_id, "duel_entry_fee", f"{duel.id}:challenger_fee", -fee)
    _duel_apply_xp_delta_once(opponent_id, "duel_entry_fee", f"{duel.id}:opponent_fee", -fee)

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

    if _duel_safe_user_id(getattr(duel, "challenger_id", None)) != current_user.id:
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
    if duel.status == "live":
        live_challenger_score, live_opponent_score = _duel_compute_live_scores(duel)
        duel.challenger_score = live_challenger_score
        duel.opponent_score = live_opponent_score

    if duel.status in {"pending", "declined", "expired", "canceled"}:
        return render_template(
            "student/duel_invite.html",
            duel=duel,
            slot=slot,
            challenger_name=_duel_safe_user_name(getattr(duel, "challenger_id", None), "لاعب"),
        )

    question_ids = []
    try:
        question_ids = json.loads(duel.question_ids_json or "[]")
    except Exception:
        question_ids = []
    questions = list(Question.objects(id__in=[qid for qid in question_ids if ObjectId.is_valid(str(qid))]).all()) if question_ids else []
    qmap = {str(q.id): q for q in questions}
    ordered_questions = [qmap[qid] for qid in question_ids if qid in qmap]

    questions_payload = []
    for q in ordered_questions:
        questions_payload.append(
            {
                "id": str(q.id),
                "text": q.text,
                "images": list(q.question_images or []),
                "correct_choice_id": str(q.correct_choice_id) if q.correct_choice_id else "",
                "choices": [
                    {
                        "choice_id": str(c.choice_id) if c.choice_id else "",
                        "text": c.text,
                        "image_url": c.image_url,
                    }
                    for c in (q.choices or [])
                ],
            }
        )

    player_answers = list(DuelAnswer.objects(duel_id=duel.id, player_id=current_user.id).all())
    answers_by_qid = {str(a.question_id.id): str(a.choice_id) if a.choice_id else "" for a in player_answers if a.question_id}

    state = _duel_build_play_state(duel, slot)
    my_left = int(state["my_left"])
    opp_left = int(state["opp_left"])
    opp_slot = "opponent" if slot == "challenger" else "challenger"
    opponent_user = _duel_safe_user(duel.opponent_id if slot == "challenger" else duel.challenger_id)
    phase = state["phase"]
    xp_summary = _duel_get_xp_change_summary(duel, current_user.id) if phase == "completed" else None

    return render_template(
        "student/duel_play.html",
        duel=duel,
        phase=phase,
        slot=slot,
        opponent_user=opponent_user,
        questions=ordered_questions,
        questions_payload=questions_payload,
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
    if duel.status == "live":
        live_challenger_score, live_opponent_score = _duel_compute_live_scores(duel)
    else:
        live_challenger_score = int(duel.challenger_score or 0)
        live_opponent_score = int(duel.opponent_score or 0)
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
            "challenger_score": int(live_challenger_score or 0),
            "opponent_score": int(live_opponent_score or 0),
            "opponent_perfect_first_warning": opponent_perfect_first_warning,
            "opponent_time_deduction_seconds": 60 if opponent_perfect_first_warning else 0,
        }
    )


@student_bp.route("/duels/<duel_id>/answer", methods=["POST"])
@login_required
def duel_answer(duel_id):
    duel = Duel.objects(id=duel_id).first() if ObjectId.is_valid(duel_id) else None
    if not duel:
        return jsonify({"ok": False, "error": "not_found"}), 404

    slot = _duel_player_slot(duel, current_user.id)
    if not slot:
        return jsonify({"ok": False, "error": "forbidden"}), 403

    if duel.status != "live":
        return jsonify({"ok": False, "error": "not_live"}), 409

    if _duel_slot_submitted(duel, slot):
        return jsonify({"ok": False, "error": "already_submitted"}), 409

    payload = request.get_json(silent=True) or {}
    question_id_raw = str(payload.get("question_id") or "").strip()
    choice_id_raw = str(payload.get("choice_id") or "").strip()
    if not ObjectId.is_valid(question_id_raw) or not ObjectId.is_valid(choice_id_raw):
        return jsonify({"ok": False, "error": "invalid_payload"}), 400

    try:
        question_ids = json.loads(duel.question_ids_json or "[]")
    except Exception:
        question_ids = []

    if question_id_raw not in question_ids:
        return jsonify({"ok": False, "error": "question_not_in_duel"}), 400

    question = Question.objects(id=question_id_raw).first()
    if not question:
        return jsonify({"ok": False, "error": "question_not_found"}), 404

    is_correct = bool(question.correct_choice_id and str(question.correct_choice_id) == choice_id_raw)
    existing = DuelAnswer.objects(duel_id=duel.id, player_id=current_user.id, question_id=question.id).first()
    if existing:
        existing.choice_id = ObjectId(choice_id_raw)
        existing.is_correct = is_correct
        existing.save()
    else:
        DuelAnswer(
            duel_id=duel.id,
            player_id=current_user.id,
            question_id=question.id,
            choice_id=ObjectId(choice_id_raw),
            is_correct=is_correct,
        ).save()

    challenger_score, opponent_score = _duel_compute_live_scores(duel)
    duel.challenger_score = int(challenger_score or 0)
    duel.opponent_score = int(opponent_score or 0)
    duel.save()

    my_score = duel.challenger_score if slot == "challenger" else duel.opponent_score
    opp_score = duel.opponent_score if slot == "challenger" else duel.challenger_score
    return jsonify(
        {
            "ok": True,
            "is_correct": bool(is_correct),
            "my_score": int(my_score or 0),
            "opponent_score": int(opp_score or 0),
            "challenger_score": int(duel.challenger_score or 0),
            "opponent_score_global": int(duel.opponent_score or 0),
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

    my_user = _duel_safe_user(duel.challenger_id if slot == "challenger" else duel.opponent_id)
    opponent_user = _duel_safe_user(duel.opponent_id if slot == "challenger" else duel.challenger_id)
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
@cache.cached(timeout=20, key_prefix="duels_leaderboard")
def duel_leaderboard():
    non_student_ids = [u.id for u in User.objects(role__ne="student").only("id").all()]
    stats_top = list(DuelStats.objects(student_id__nin=non_student_ids).order_by("-wins", "-current_win_streak", "student_id").limit(10).all())
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
                if access.subject_requires_code and not access.subject_open:
                    flash("قم بتفعيل المادة للوصول إلى هذا الاختبار.", "warning")
                    return redirect(url_for("student.activate_subject", subject_id=test.section.subject.id))
                flash("قم بتفعيل القسم للوصول إلى هذا الاختبار.", "warning")
                return redirect(url_for("student.activate_section", section_id=test.section_id))
            else:
                flash("قم بتفعيل القسم للوصول إلى هذا الاختبار.", "warning")
                return redirect(url_for("student.activate_section", section_id=test.section_id))

    interactive_questions = list(TestInteractiveQuestion.objects(test_id=test.id).order_by('created_at').all())
    test_resources = list(TestResource.objects(test_id=test.id).order_by('position').all())
    total_questions_available = len(test.questions) + len(interactive_questions)
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

    if selected_count is None and total_questions_available == 0 and interactive_questions:
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
        interactive_question_ids_raw = request.form.get("interactive_question_ids", "")
        if question_ids_raw:
            question_ids = [qid.strip() for qid in question_ids_raw.split(",") if ObjectId.is_valid(qid.strip())]
            questions = Question.objects(id__in=question_ids).all()
            questions_by_id = {str(q.id): q for q in questions}
            ordered_questions = [questions_by_id[qid] for qid in question_ids if qid in questions_by_id]
        else:
            ordered_questions = list(test.questions)

        ordered_interactive_questions = list(interactive_questions)
        if interactive_question_ids_raw:
            interactive_question_ids = [qid.strip() for qid in interactive_question_ids_raw.split(",") if ObjectId.is_valid(qid.strip())]
            interactive_q_map = {
                str(iq.id): iq
                for iq in TestInteractiveQuestion.objects(id__in=interactive_question_ids, test_id=test.id).all()
            }
            ordered_interactive_questions = [interactive_q_map[qid] for qid in interactive_question_ids if qid in interactive_q_map]

        settings_payload = {
            "count": _to_int(request.form.get("count"), len(ordered_questions) + len(ordered_interactive_questions)),
            "easy": _to_int(request.form.get("easy"), 0),
            "medium": _to_int(request.form.get("medium"), 0),
            "hard": _to_int(request.form.get("hard"), 0),
        }

        is_retake = False
        if retake_source_id and ObjectId.is_valid(str(retake_source_id)):
            source = Attempt.objects(id=retake_source_id, student_id=current_user.id).first()
            is_retake = bool(source)

        question_order = [str(q.id) for q in ordered_questions]
        interactive_total = len(ordered_interactive_questions)
        total = len(ordered_questions) + interactive_total
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

        for iq in ordered_interactive_questions:
            raw = (request.form.get(f"interactive_question_{iq.id}") or "").strip().lower()
            selected_value = raw == "true"
            is_correct = bool(selected_value)
            if is_correct:
                score += 1
            AttemptInteractiveAnswer(
                attempt_id=attempt.id,
                interactive_question_id=iq.id,
                selected_value=selected_value,
                is_correct=is_correct,
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
    interactive_question_ids_str = ""
    if selected_count:
        questions = list(test.questions)
        interactive_pool = list(interactive_questions)
        combined_pool = [("mcq", q) for q in questions] + [("interactive", iq) for iq in interactive_pool]
        selected_mcq = []
        selected_interactive = []
        if preset_question_ids:
            questions_by_id = {str(q.id): q for q in Question.objects(id__in=preset_question_ids).all()}
            questions = [questions_by_id[qid] for qid in preset_question_ids if qid in questions_by_id]
            selected_count = len(questions)
            selected_mcq = questions
        elif selected_by_level:
            level_map = {"easy": [], "medium": [], "hard": []}
            for q_type, q_obj in combined_pool:
                level = (getattr(q_obj, "difficulty", "medium") or "medium").lower()
                if level not in level_map:
                    level = "medium"
                level_map[level].append((q_type, q_obj))

            available_by_level = {
                "easy": len(level_map["easy"]),
                "medium": len(level_map["medium"]),
                "hard": len(level_map["hard"]),
            }
            requested_by_level = {
                "easy": easy_count,
                "medium": medium_count,
                "hard": hard_count,
            }
            allocated_by_level = _rebalance_difficulty_request(requested_by_level, available_by_level)

            if sum(allocated_by_level.values()) < (easy_count + medium_count + hard_count):
                flash("عدد الأسئلة المطلوب أعلى من المتاح.", "error")
                return redirect(url_for("student.take_test", test_id=test.id))

            easy_count = allocated_by_level["easy"]
            medium_count = allocated_by_level["medium"]
            hard_count = allocated_by_level["hard"]

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
            random.shuffle(picked)
            selected_mcq = [obj for q_type, obj in picked if q_type == "mcq"]
            selected_interactive = [obj for q_type, obj in picked if q_type == "interactive"]
        else:
            picked = combined_pool
            if selected_count < len(combined_pool):
                picked = random.sample(combined_pool, selected_count)
            selected_mcq = [obj for q_type, obj in picked if q_type == "mcq"]
            selected_interactive = [obj for q_type, obj in picked if q_type == "interactive"]

        if not preset_question_ids:
            random.shuffle(selected_mcq)
            random.shuffle(selected_interactive)
        question_ids = [q.id for q in selected_mcq]
        interactive_ids = [iq.id for iq in selected_interactive]
        question_ids_str = ",".join(str(qid) for qid in question_ids)
        interactive_question_ids_str = ",".join(str(iid) for iid in interactive_ids)
        for q in selected_mcq:
            choices = list(q.choices)
            random.shuffle(choices)
            ordered_questions.append({"question": q, "choices": choices, "question_type": "mcq"})
        for iq in selected_interactive:
            ordered_questions.append({"interactive_question": iq, "choices": [], "question_type": "interactive"})
        time_limit_seconds = ((len(question_ids) + len(interactive_ids)) * 75) + 15
    elif selected_count is not None and total_questions_available == 0 and interactive_questions:
        for iq in interactive_questions:
            ordered_questions.append({"interactive_question": iq, "choices": [], "question_type": "interactive"})
        interactive_question_ids_str = ",".join(str(iq.id) for iq in interactive_questions)
        time_limit_seconds = (len(interactive_questions) * 75) + 15

    # Available counts per difficulty for UI
    def _norm_level(q):
        level = (getattr(q, "difficulty", "medium") or "medium").lower()
        return level if level in {"easy", "medium", "hard"} else "medium"

    available_easy = len([q for q in test.questions if _norm_level(q) == "easy"]) + len([iq for iq in interactive_questions if _norm_level(iq) == "easy"])
    available_medium = len([q for q in test.questions if _norm_level(q) == "medium"]) + len([iq for iq in interactive_questions if _norm_level(iq) == "medium"])
    available_hard = len([q for q in test.questions if _norm_level(q) == "hard"]) + len([iq for iq in interactive_questions if _norm_level(iq) == "hard"])

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
        interactive_question_ids_str=interactive_question_ids_str,
        time_limit_seconds=time_limit_seconds,
        test_resources=test_resources,
        exit_token=secrets.token_hex(8) if selected_count else "",
        retake_source_id=retake_source_id,
        retake_mode=retake_mode,
    )


@student_bp.route("/tests/<test_id>/abandon", methods=["POST"])
@login_required
def abandon_test(test_id):
    if (current_user.role or "").lower() != "student":
        return jsonify({"ok": False, "error": "forbidden"}), 403

    test = Test.objects(id=test_id).first()
    if not test:
        return jsonify({"ok": False, "error": "not_found"}), 404

    payload = request.get_json(silent=True) if request.is_json else {}
    exit_token = (request.form.get("exit_token") or (payload or {}).get("exit_token") or "")
    exit_token = (exit_token or "").strip()
    if not exit_token:
        return jsonify({"ok": False, "error": "missing_exit_token"}), 400

    source_id = f"{test.id}:{exit_token}"
    deducted_xp, profile = _apply_xp_penalty_once(
        student_id=current_user.id,
        event_type="test_abandon_penalty",
        source_id=source_id,
        penalty_amount=TEST_EXIT_XP_PENALTY,
    )
    return jsonify(
        {
            "ok": True,
            "deducted_xp": int(deducted_xp or 0),
            "xp_total": int(profile.xp_total or 0),
        }
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
        ac = SubjectActivationCode.objects(subject_id=subject.id, code=code_value).first()
        if not ac:
            flash("رمز غير صحيح لهذه المادة.", "error")
            return render_template("student/activate_subject.html", subject=subject, form=form)
        if ac.is_used:
            flash("This code has already been used.", "error")
            return render_template("student/activate_subject.html", subject=subject, form=form)

        # Backward compatibility: old per-student codes remain limited to their owner.
        if ac.student_id and str(ac.student_id.id) != str(current_user.id):
            flash("هذا الرمز غير مخصص لهذا الحساب.", "error")
            return render_template("student/activate_subject.html", subject=subject, form=form)

        # Don't consume a code if the subject is already activated for this student.
        existing = SubjectActivation.objects(subject_id=subject.id, student_id=current_user.id, active=True).first()
        if existing:
            flash("المادة مفعلة بالفعل لهذا الحساب.", "info")
            return redirect(url_for("student.subject_detail", subject_id=subject.id))

        # mark used and activate
        ac.is_used = True
        ac.used_at = datetime.utcnow()
        ac.student_id = current_user.id
        ac.save()
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
    access = AccessContext(lesson.section, current_user.id)
    if access.subject_requires_code and not access.subject_open:
        flash("تفعيل الدروس بشكل فردي غير متاح. فعّل المادة أو القسم.", "info")
        return redirect(url_for("student.activate_subject", subject_id=lesson.section.subject.id))
    flash("تفعيل الدروس بشكل فردي غير متاح. فعّل القسم للوصول إلى جميع دروسه.", "info")
    return redirect(url_for("student.activate_section", section_id=lesson.section.id))


@student_bp.route("/assignments")
@login_required
@cache.cached(timeout=45, key_prefix=lambda: f"assignments_{current_user.id}")
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
    cache.delete(f"assignments_{current_user.id}")

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
        cache.delete(f"assignments_{current_user.id}")
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
@cache.cached(timeout=45, key_prefix=lambda: f"study_plans_{current_user.id}")
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
    cache.delete(f"study_plans_{current_user.id}")

    if item.is_done:
        _award_flat_xp_once(
            student_id=current_user.id,
            event_type="study_plan_item_complete",
            source_id=str(item.id),
            amount=5,
        )
        flash("تم إنجاز المهمة (+5 جواهر).", "success")
    else:
        flash("تم إعادة المهمة إلى غير منجزة.", "info")

    return redirect(url_for("student.study_plans"))


@student_bp.route("/results")
@login_required
@cache.cached(timeout=30, key_prefix=lambda: f"results_{current_user.id}")
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
    for attempt in all_attempts:
        attempt._pending_text_grading = False
    return render_template(
        "student/results.html",
        own_attempts=own_attempts,
        own_custom_attempts=own_custom_attempts,
        other_attempts=other_attempts,
    )


@student_bp.route("/statistics")
@login_required
@cache.cached(timeout=60, key_prefix=lambda: f"statistics_{current_user.id}_{(current_user.role or '').lower()}")
def statistics():
    role = (current_user.role or "").lower()
    if role not in {"student", "admin"}:
        flash("غير مسموح.", "error")
        return redirect(url_for("index"))

    if role == "admin":
        students = list(User.objects(role="student").order_by("first_name", "last_name", "username").all())
    else:
        students = [current_user]

    student_ids = [s.id for s in students if s and s.id]
    subjects = list(Subject.objects().order_by("created_at").all())
    subject_ids = [s.id for s in subjects]
    subject_name_by_id = {s.id: s.name for s in subjects}

    lessons_total_by_subject = {sid: 0 for sid in subject_ids}
    lesson_subject_by_id = {}
    for lesson in Lesson.objects().only("id", "section_id").all():
        try:
            if not lesson.section_id or not lesson.section_id.subject_id:
                continue
            sid = lesson.section_id.subject_id.id
        except Exception:
            continue
        if sid in lessons_total_by_subject:
            lessons_total_by_subject[sid] += 1
            lesson_subject_by_id[lesson.id] = sid

    test_subject_by_id = {}
    for test in Test.objects().only("id", "section_id").all():
        try:
            if not test.section_id or not test.section_id.subject_id:
                continue
            sid = test.section_id.subject_id.id
        except Exception:
            continue
        test_subject_by_id[test.id] = sid

    course_subject_by_id = {}
    for cs in CourseSet.objects().only("id", "subject_id").all():
        try:
            sid = cs.subject_id.id if cs.subject_id else None
        except Exception:
            sid = None
        if sid:
            course_subject_by_id[cs.id] = sid

    attempts_by_student_subject = {}
    completed_tests_by_student_subject = {}
    completed_lessons_by_student_subject = {}

    def _inc_attempt(student_id, subject_id):
        key = (student_id, subject_id)
        attempts_by_student_subject[key] = attempts_by_student_subject.get(key, 0) + 1

    def _add_completed_test(student_id, subject_id, test_key):
        key = (student_id, subject_id)
        completed_tests_by_student_subject.setdefault(key, set()).add(test_key)

    def _add_completed_lesson(student_id, subject_id, lesson_id):
        key = (student_id, subject_id)
        completed_lessons_by_student_subject.setdefault(key, set()).add(lesson_id)

    if student_ids:
        for attempt in Attempt.objects(student_id__in=student_ids).only("student_id", "test_id").all():
            try:
                stu_id = attempt.student_id.id if attempt.student_id else None
                tst_id = attempt.test_id.id if attempt.test_id else None
            except Exception:
                continue
            if not stu_id or not tst_id:
                continue
            subject_id = test_subject_by_id.get(tst_id)
            if not subject_id:
                continue
            _inc_attempt(stu_id, subject_id)
            _add_completed_test(stu_id, subject_id, f"t:{tst_id}")

        for cattempt in CourseAttempt.objects(student_id__in=student_ids, status="submitted").only("student_id", "course_set_id").all():
            try:
                stu_id = cattempt.student_id.id if cattempt.student_id else None
                cs_id = cattempt.course_set_id.id if cattempt.course_set_id else None
            except Exception:
                continue
            if not stu_id or not cs_id:
                continue
            subject_id = course_subject_by_id.get(cs_id)
            if not subject_id:
                continue
            _inc_attempt(stu_id, subject_id)
            _add_completed_test(stu_id, subject_id, f"c:{cs_id}")

        for comp in LessonCompletion.objects(student_id__in=student_ids).only("student_id", "lesson_id").all():
            try:
                stu_id = comp.student_id.id if comp.student_id else None
                les_id = comp.lesson_id.id if comp.lesson_id else None
            except Exception:
                continue
            if not stu_id or not les_id:
                continue
            subject_id = lesson_subject_by_id.get(les_id)
            if not subject_id:
                continue
            _add_completed_lesson(stu_id, subject_id, les_id)

    rows = []
    for student in students:
        student_subject_rows = []
        try:
            stu_id = student.id
        except Exception:
            continue

        for sid in subject_ids:
            completed_lessons = len(completed_lessons_by_student_subject.get((stu_id, sid), set()))
            total_lessons = lessons_total_by_subject.get(sid, 0)
            remaining_lessons = max(total_lessons - completed_lessons, 0)
            total_attempts = int(attempts_by_student_subject.get((stu_id, sid), 0) or 0)
            completed_tests = len(completed_tests_by_student_subject.get((stu_id, sid), set()))
            completion_pct = round((completed_lessons / total_lessons) * 100, 1) if total_lessons > 0 else 0.0

            student_subject_rows.append(
                {
                    "subject_id": sid,
                    "subject_name": subject_name_by_id.get(sid, "-") or "-",
                    "completed_tests": completed_tests,
                    "total_attempts": total_attempts,
                    "completed_lessons": completed_lessons,
                    "remaining_lessons": remaining_lessons,
                    "total_lessons": total_lessons,
                    "completion_pct": completion_pct,
                }
            )

        student_subject_rows.sort(key=lambda x: x["subject_name"])
        rows.append({"student": student, "subjects": student_subject_rows})

    return render_template(
        "student/statistics.html",
        is_admin_view=(role == "admin"),
        rows=rows,
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

    selected_subject_id = (request.args.get("subject_id") or "").strip()
    selected_section_id = (request.args.get("section_id") or "").strip()
    selected_lesson_id = (request.args.get("lesson_id") or "").strip()

    freq_map = _frequently_wrong_question_counts(current_user.id)
    question_ids = list(freq_map.keys())
    questions = list(Question.objects(id__in=question_ids).all()) if question_ids else []
    questions_by_id = {q.id: q for q in questions}

    rows_all = []
    for qid, freq in freq_map.items():
        q = questions_by_id.get(qid)
        if not q:
            continue

        test = q.test_id
        lesson = None
        section = None
        subject = None
        if test:
            lesson = test.lesson
            section = lesson.section if lesson else test.section
            subject = section.subject if section else None

        rows_all.append(
            {
                "question": q,
                "frequency": int(freq or 0),
                "test": test,
                "test_title": test.title if test else "-",
                "subject_id": str(subject.id) if subject else "",
                "subject_name": subject.name if subject else "غير مصنف",
                "section_id": str(section.id) if section else "",
                "section_name": section.title if section else "بدون قسم",
                "lesson_id": str(lesson.id) if lesson else "",
                "lesson_name": lesson.title if lesson else "اختبارات على مستوى القسم",
            }
        )

    rows_all.sort(key=lambda r: r["frequency"], reverse=True)
    top_rows = rows_all[:10]

    total_wrong = sum(r["frequency"] for r in rows_all)

    subject_options_map = {}
    section_options_map = {}
    lesson_options_map = {}
    for row in rows_all:
        if row["subject_id"]:
            subject_options_map[row["subject_id"]] = row["subject_name"]
        if row["section_id"]:
            section_options_map[row["section_id"]] = {
                "name": row["section_name"],
                "subject_id": row["subject_id"],
            }
        if row["lesson_id"]:
            lesson_options_map[row["lesson_id"]] = {
                "name": row["lesson_name"],
                "section_id": row["section_id"],
            }

    subject_options = [
        {"id": sid, "name": sname}
        for sid, sname in sorted(subject_options_map.items(), key=lambda x: x[1])
    ]
    section_options = [
        {
            "id": sec_id,
            "name": payload["name"],
            "subject_id": payload["subject_id"],
        }
        for sec_id, payload in sorted(section_options_map.items(), key=lambda x: x[1]["name"])
        if not selected_subject_id or payload["subject_id"] == selected_subject_id
    ]
    lesson_options = [
        {
            "id": lesson_id,
            "name": payload["name"],
            "section_id": payload["section_id"],
        }
        for lesson_id, payload in sorted(lesson_options_map.items(), key=lambda x: x[1]["name"])
        if not selected_section_id or payload["section_id"] == selected_section_id
    ]

    def _matches_filters(row):
        if selected_subject_id and row["subject_id"] != selected_subject_id:
            return False
        if selected_section_id and row["section_id"] != selected_section_id:
            return False
        if selected_lesson_id and row["lesson_id"] != selected_lesson_id:
            return False
        return True

    rows = [r for r in rows_all if _matches_filters(r)]
    filtered_total_wrong = sum(r["frequency"] for r in rows)

    grouped_map = {}
    for row in rows:
        subject_key = row["subject_id"] or "_none_subject"
        section_key = row["section_id"] or "_none_section"
        lesson_key = row["lesson_id"] or "_none_lesson"

        subject_bucket = grouped_map.setdefault(
            subject_key,
            {
                "subject_id": row["subject_id"],
                "subject_name": row["subject_name"],
                "sections": {},
            },
        )

        section_bucket = subject_bucket["sections"].setdefault(
            section_key,
            {
                "section_id": row["section_id"],
                "section_name": row["section_name"],
                "lessons": {},
            },
        )

        lesson_bucket = section_bucket["lessons"].setdefault(
            lesson_key,
            {
                "lesson_id": row["lesson_id"],
                "lesson_name": row["lesson_name"],
                "rows": [],
            },
        )
        lesson_bucket["rows"].append(row)

    grouped_subjects = []
    for subject_bucket in grouped_map.values():
        sections = []
        subject_question_count = 0
        for section_bucket in subject_bucket["sections"].values():
            lessons = []
            section_question_count = 0
            for lesson_bucket in section_bucket["lessons"].values():
                lesson_bucket["rows"].sort(key=lambda r: r["frequency"], reverse=True)
                lesson_count = len(lesson_bucket["rows"])
                section_question_count += lesson_count
                lessons.append(
                    {
                        "lesson_id": lesson_bucket["lesson_id"],
                        "lesson_name": lesson_bucket["lesson_name"],
                        "question_count": lesson_count,
                        "rows": lesson_bucket["rows"],
                    }
                )
            lessons.sort(key=lambda l: l["lesson_name"])
            subject_question_count += section_question_count
            sections.append(
                {
                    "section_id": section_bucket["section_id"],
                    "section_name": section_bucket["section_name"],
                    "question_count": section_question_count,
                    "lessons": lessons,
                }
            )
        sections.sort(key=lambda s: s["section_name"])
        grouped_subjects.append(
            {
                "subject_id": subject_bucket["subject_id"],
                "subject_name": subject_bucket["subject_name"],
                "question_count": subject_question_count,
                "sections": sections,
            }
        )

    grouped_subjects.sort(key=lambda s: s["subject_name"])

    return render_template(
        "student/frequently_wrong.html",
        rows=rows,
        top_rows=top_rows,
        grouped_subjects=grouped_subjects,
        total_wrong=total_wrong,
        filtered_total_wrong=filtered_total_wrong,
        subject_options=subject_options,
        section_options=section_options,
        lesson_options=lesson_options,
        selected_subject_id=selected_subject_id,
        selected_section_id=selected_section_id,
        selected_lesson_id=selected_lesson_id,
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

    favorite_mcq_map = {}
    favorite_interactive_map = {}
    if (current_user.role or "").lower() == "student":
        for fav in StudentFavoriteQuestion.objects(
            student_id=current_user.id,
            question_type="mcq",
            question_id__in=question_ids,
        ).only("id", "question_id"):
            if fav.question_id:
                favorite_mcq_map[str(fav.question_id.id)] = str(fav.id)
    
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
            "is_favorite": str(q.id) in favorite_mcq_map,
            "favorite_id": favorite_mcq_map.get(str(q.id)),
        })

    interactive_answers = list(AttemptInteractiveAnswer.objects(attempt_id=attempt.id).all())
    interactive_question_ids = [ia.interactive_question_id.id for ia in interactive_answers if ia.interactive_question_id]
    if (current_user.role or "").lower() == "student" and interactive_question_ids:
        for fav in StudentFavoriteQuestion.objects(
            student_id=current_user.id,
            question_type="interactive",
            interactive_question_id__in=interactive_question_ids,
        ).only("id", "interactive_question_id"):
            if fav.interactive_question_id:
                favorite_interactive_map[str(fav.interactive_question_id.id)] = str(fav.id)
    interactive_questions_map = {
        str(iq.id): iq for iq in TestInteractiveQuestion.objects(id__in=interactive_question_ids).all()
    } if interactive_question_ids else {}
    interactive_review = []
    for ia in interactive_answers:
        if not ia.interactive_question_id:
            continue
        iq = interactive_questions_map.get(str(ia.interactive_question_id.id))
        if not iq:
            continue
        interactive_review.append(
            {
                "question": iq,
                "selected_value": ia.selected_value,
                "is_correct": ia.is_correct,
                "is_favorite": str(iq.id) in favorite_interactive_map,
                "favorite_id": favorite_interactive_map.get(str(iq.id)),
            }
        )

    gamification = StudentGamification.objects(student_id=attempt.student_id.id).first()
    return render_template(
        "student/test_result.html",
        attempt=attempt,
        review=review,
        interactive_review=interactive_review,
        gamification=gamification,
        pending_text_grading=False,
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
    def _safe_section_for_lesson(lesson):
        try:
            return lesson.section
        except Exception:
            return None

    def _safe_subject_for_lesson(lesson):
        section = _safe_section_for_lesson(lesson)
        if not section:
            return None
        try:
            return section.subject_id
        except Exception:
            return None

    subjects = list(Subject.objects().order_by('created_at').all())
    selected_subject_id = request.args.get("subject_id") or request.form.get("subject_id")
    selected_lesson_id = request.args.get("lesson_id") or request.form.get("lesson_id")
    if selected_subject_id and not ObjectId.is_valid(str(selected_subject_id)):
        selected_subject_id = None
    if selected_lesson_id and not ObjectId.is_valid(str(selected_lesson_id)):
        selected_lesson_id = None

    selected_lesson = None
    forced_selection_mode = None
    if current_user.role == "student" and selected_lesson_id:
        selected_lesson = Lesson.objects(id=selected_lesson_id).first()
        if not selected_lesson:
            flash("الدرس المحدد غير موجود.", "error")
            return redirect(url_for("student.custom_test_new", subject_id=selected_subject_id) if selected_subject_id else url_for("student.custom_test_new"))

        selected_section = _safe_section_for_lesson(selected_lesson)
        if not selected_section:
            flash("تعذر الوصول إلى القسم المرتبط بهذا الدرس.", "error")
            return redirect(url_for("student.custom_test_new", subject_id=selected_subject_id) if selected_subject_id else url_for("student.custom_test_new"))

        access = AccessContext(selected_section, current_user.id)
        if not access.lesson_open(selected_lesson):
            flash("لا يمكنك الوصول إلى هذا الدرس حالياً.", "warning")
            return redirect(url_for("student.lesson_detail", lesson_id=selected_lesson.id))

        if not bool(getattr(selected_lesson, "allow_full_lesson_test", False)):
            flash("هذا الدرس لا يدعم Full lesson test حالياً.", "warning")
            return redirect(url_for("student.lesson_detail", lesson_id=selected_lesson.id))

        unlocked_lessons = [selected_lesson]
    elif current_user.role == "student":
        unlocked_lessons = get_unlocked_lessons(current_user.id)
    else:
        unlocked_lessons = [l for l in Lesson.objects().all() if _safe_section_for_lesson(l)]

    if selected_lesson_id and not selected_lesson:
        selected_lesson = Lesson.objects(id=selected_lesson_id).first()
        if not selected_lesson:
            flash("الدرس المحدد غير موجود.", "error")
            return redirect(url_for("student.custom_test_new", subject_id=selected_subject_id) if selected_subject_id else url_for("student.custom_test_new"))

        if current_user.role == "student":
            if not bool(getattr(selected_lesson, "allow_full_lesson_test", False)):
                flash("هذا الدرس لا يدعم Full lesson test حالياً.", "warning")
                return redirect(url_for("student.lesson_detail", lesson_id=selected_lesson.id))

            unlocked_ids = {lesson.id for lesson in unlocked_lessons}
            if selected_lesson.id not in unlocked_ids:
                flash("لا يمكنك الوصول إلى هذا الدرس حالياً.", "warning")
                return redirect(url_for("student.lesson_detail", lesson_id=selected_lesson.id))

    if selected_lesson:
        selected_section = _safe_section_for_lesson(selected_lesson)
        selected_subject = _safe_subject_for_lesson(selected_lesson)
        if selected_subject:
            selected_subject_id = str(selected_subject.id)
        # Full lesson test should always allocate by tests inside the lesson.
        forced_selection_mode = "test"
    
    subject_filter = None
    if selected_subject_id:
        subject_filter = Subject.objects(id=selected_subject_id).first()
        if subject_filter:
            unlocked_lessons = [
                lesson for lesson in unlocked_lessons
                if _safe_subject_for_lesson(lesson) == subject_filter
            ]

    if selected_lesson:
        unlocked_lessons = [lesson for lesson in unlocked_lessons if lesson.id == selected_lesson.id]

    lesson_question_counts = {}
    lesson_difficulty_counts = {}
    tests_data = []
    tests_by_lesson = {}
    test_question_counts = {}
    test_difficulty_counts = {}

    def _normalize_difficulty(value):
        level = str(value or "medium").strip().lower()
        return level if level in {"easy", "medium", "hard"} else "medium"

    def _aggregate_counts_by_test(model_cls, test_ids_list):
        counts = {tid: 0 for tid in test_ids_list}
        diff_counts = {tid: {"easy": 0, "medium": 0, "hard": 0} for tid in test_ids_list}
        if not test_ids_list:
            return counts, diff_counts

        test_refs = list(test_ids_list)
        test_refs.extend(DBRef("tests", tid) for tid in test_ids_list)

        pipeline = [
            {"$match": {"test_id": {"$in": test_refs}}},
            {
                "$group": {
                    "_id": {
                        "test_id": "$test_id",
                        "difficulty": {"$ifNull": ["$difficulty", "medium"]},
                    },
                    "count": {"$sum": 1},
                }
            },
        ]

        for row in model_cls._get_collection().aggregate(pipeline, allowDiskUse=True):
            key = row.get("_id") or {}
            raw_test_id = key.get("test_id")
            if isinstance(raw_test_id, DBRef):
                raw_test_id = raw_test_id.id
            if raw_test_id not in counts:
                continue
            level = _normalize_difficulty(key.get("difficulty"))
            amount = int(row.get("count", 0) or 0)
            counts[raw_test_id] += amount
            diff_counts[raw_test_id][level] += amount

        return counts, diff_counts

    if unlocked_lessons:
        lesson_ids = [l.id for l in unlocked_lessons]
        tests = list(Test.objects(lesson_id__in=lesson_ids).order_by('created_at').all())
        test_ids = [t.id for t in tests]

        for test in tests:
            lesson_id = test.lesson_id.id if test.lesson_id else None
            if not lesson_id:
                continue
            tests_by_lesson.setdefault(lesson_id, []).append(test)

        mcq_counts, mcq_diff = _aggregate_counts_by_test(Question, test_ids)
        interactive_counts, interactive_diff = _aggregate_counts_by_test(TestInteractiveQuestion, test_ids)

        for test in tests:
            tid = test.id
            test_question_counts[tid] = mcq_counts.get(tid, 0) + interactive_counts.get(tid, 0)
            test_difficulty_counts[tid] = {
                'easy': mcq_diff.get(tid, {}).get('easy', 0) + interactive_diff.get(tid, {}).get('easy', 0),
                'medium': mcq_diff.get(tid, {}).get('medium', 0) + interactive_diff.get(tid, {}).get('medium', 0),
                'hard': mcq_diff.get(tid, {}).get('hard', 0) + interactive_diff.get(tid, {}).get('hard', 0),
            }

        for lesson in unlocked_lessons:
            lesson_total = 0
            lesson_diff = {'easy': 0, 'medium': 0, 'hard': 0}
            for test in tests_by_lesson.get(lesson.id, []):
                cnt = test_question_counts.get(test.id, 0)
                lesson_total += cnt
                diff_map = test_difficulty_counts.get(test.id, {'easy': 0, 'medium': 0, 'hard': 0})
                lesson_diff['easy'] += diff_map.get('easy', 0)
                lesson_diff['medium'] += diff_map.get('medium', 0)
                lesson_diff['hard'] += diff_map.get('hard', 0)

                test._question_count = cnt
                test._difficulty_counts = diff_map
                if cnt > 0:
                    tests_data.append({'test': test, 'lesson': lesson})

            lesson_question_counts[lesson.id] = lesson_total
            lesson_difficulty_counts[lesson.id] = lesson_diff

    total_available_questions = sum(lesson_question_counts.values())

    if request.method == "POST":
        if not selected_subject_id:
            flash("اختر مادة قبل إنشاء اختبار مخصص.", "error")
            return redirect(url_for("student.custom_test_new"))

        selection_mode = (request.form.get('selection_mode') or forced_selection_mode or 'test').strip().lower()
        if forced_selection_mode:
            selection_mode = forced_selection_mode
        if selection_mode not in {'lesson', 'test'}:
            selection_mode = 'test'

        selections = []
        total_questions = 0

        scopes = tests_data if selection_mode == 'test' else [{'lesson': lesson} for lesson in unlocked_lessons]
        for scope in scopes:
            if selection_mode == 'test':
                test = scope['test']
                title = f"{scope['lesson'].title} - {test.title}"
                key_prefix = f"test_{test.id}"
                max_available = test_question_counts.get(test.id, 0)
                available_diff = test_difficulty_counts.get(test.id, {'easy': 0, 'medium': 0, 'hard': 0})
                scope_id = str(test.id)
            else:
                lesson = scope['lesson']
                title = lesson.title
                key_prefix = f"lesson_{lesson.id}"
                max_available = lesson_question_counts.get(lesson.id, 0)
                available_diff = lesson_difficulty_counts.get(lesson.id, {'easy': 0, 'medium': 0, 'hard': 0})
                scope_id = str(lesson.id)

            easy_count = _to_int((request.form.get(f"{key_prefix}_easy", "") or '').strip(), 0)
            medium_count = _to_int((request.form.get(f"{key_prefix}_medium", "") or '').strip(), 0)
            hard_count = _to_int((request.form.get(f"{key_prefix}_hard", "") or '').strip(), 0)
            difficulty_total = easy_count + medium_count + hard_count
            legacy_count = _to_int((request.form.get(key_prefix, "") or '').strip(), 0)

            if difficulty_total > 0:
                count = difficulty_total
                use_difficulty = True
                allocated_diff = _rebalance_difficulty_request(
                    {'easy': easy_count, 'medium': medium_count, 'hard': hard_count},
                    available_diff,
                )
                if sum(allocated_diff.values()) < difficulty_total:
                    flash(f"عدد الأسئلة المطلوب في {title} أعلى من المتاح.", "error")
                    return redirect(url_for("student.custom_test_new", subject_id=selected_subject_id, lesson_id=selected_lesson_id) if selected_lesson_id else url_for("student.custom_test_new", subject_id=selected_subject_id))
                easy_count = allocated_diff['easy']
                medium_count = allocated_diff['medium']
                hard_count = allocated_diff['hard']
            elif legacy_count > 0:
                count = legacy_count
                use_difficulty = False
            else:
                continue

            if count <= 0:
                continue
            if count > max_available:
                flash(f"تم طلب {count} أسئلة لـ {title}، ولكن {max_available} فقط متاحة.", "error")
                return redirect(url_for("student.custom_test_new", subject_id=selected_subject_id, lesson_id=selected_lesson_id) if selected_lesson_id else url_for("student.custom_test_new", subject_id=selected_subject_id))

            selection = {
                "scope_type": selection_mode,
                "scope_id": scope_id,
                "count": count,
            }
            if use_difficulty:
                selection['difficulty'] = {'easy': easy_count, 'medium': medium_count, 'hard': hard_count}

            selections.append(selection)
            total_questions += count

        if total_questions == 0:
            flash("اختر درسًا واحدًا على الأقل وعدد الأسئلة.", "error")
            return redirect(url_for("student.custom_test_new", subject_id=selected_subject_id, lesson_id=selected_lesson_id) if selected_lesson_id else url_for("student.custom_test_new", subject_id=selected_subject_id))

        if total_questions < 10:
            flash("اختر 10 أسئلة على الأقل لإنشاء اختبار مخصص.", "error")
            return redirect(url_for("student.custom_test_new", subject_id=selected_subject_id, lesson_id=selected_lesson_id) if selected_lesson_id else url_for("student.custom_test_new", subject_id=selected_subject_id))

        if total_questions > 50:
            flash("يمكنك اختيار حتى 50 سؤالًا للاختبار المخصص.", "error")
            return redirect(url_for("student.custom_test_new", subject_id=selected_subject_id, lesson_id=selected_lesson_id) if selected_lesson_id else url_for("student.custom_test_new", subject_id=selected_subject_id))

        # Build question pool (MCQ + interactive) using batched, lightweight queries.
        selection_test_ids = {}
        needed_test_ids = set()
        for sel in selections:
            scope_type = sel.get("scope_type")
            scope_id = sel.get("scope_id")
            test_ids = []

            if scope_type == "test":
                if ObjectId.is_valid(str(scope_id)):
                    test_ids = [ObjectId(str(scope_id))]
            else:
                lesson_oid = ObjectId(str(scope_id)) if ObjectId.is_valid(str(scope_id)) else None
                if lesson_oid:
                    test_ids = [t.id for t in tests_by_lesson.get(lesson_oid, [])]
                    if not test_ids:
                        test_ids = [t.id for t in Test.objects(lesson_id=lesson_oid).only("id").all()]

            selection_test_ids[(scope_type, str(scope_id))] = test_ids
            needed_test_ids.update(test_ids)

        test_ids_list = list(needed_test_ids)

        mcq_light = list(
            Question.objects(test_id__in=test_ids_list)
            .only("id", "test_id", "difficulty")
            .all()
        ) if test_ids_list else []
        interactive_light = list(
            TestInteractiveQuestion.objects(test_id__in=test_ids_list)
            .only("id", "test_id", "difficulty")
            .all()
        ) if test_ids_list else []

        mcq_by_test = {}
        interactive_by_test = {}

        for q in mcq_light:
            tid = getattr(getattr(q, "test_id", None), "id", getattr(q, "test_id", None))
            if isinstance(tid, DBRef):
                tid = tid.id
            if not tid:
                continue
            mcq_by_test.setdefault(tid, []).append(q)

        for iq in interactive_light:
            tid = getattr(getattr(iq, "test_id", None), "id", getattr(iq, "test_id", None))
            if isinstance(tid, DBRef):
                tid = tid.id
            if not tid:
                continue
            interactive_by_test.setdefault(tid, []).append(iq)

        selected_items = []  # tuples: (item_type, item_id)
        for sel in selections:
            scope_key = (sel.get("scope_type"), str(sel.get("scope_id")))
            test_ids = selection_test_ids.get(scope_key, [])

            combined_pool = []  # tuples: (item_type, item_id, difficulty)
            for tid in test_ids:
                for q in mcq_by_test.get(tid, []):
                    combined_pool.append(("mcq", q.id, _normalize_difficulty(getattr(q, "difficulty", "medium"))))
                for iq in interactive_by_test.get(tid, []):
                    combined_pool.append(("interactive", iq.id, _normalize_difficulty(getattr(iq, "difficulty", "medium"))))

            if "difficulty" in sel:
                diff_spec = sel["difficulty"]
                level_map = {"easy": [], "medium": [], "hard": []}

                for item_type, item_id, diff in combined_pool:
                    level_map[diff].append((item_type, item_id))

                picked = []
                if diff_spec["easy"] > 0:
                    if len(level_map["easy"]) < diff_spec["easy"]:
                        flash("لا توجد أسئلة كافية لإنشاء الاختبار.", "error")
                        return redirect(url_for("student.custom_test_new", subject_id=selected_subject_id, lesson_id=selected_lesson_id) if selected_lesson_id else url_for("student.custom_test_new", subject_id=selected_subject_id))
                    picked.extend(random.sample(level_map["easy"], diff_spec["easy"]))
                if diff_spec["medium"] > 0:
                    if len(level_map["medium"]) < diff_spec["medium"]:
                        flash("لا توجد أسئلة كافية لإنشاء الاختبار.", "error")
                        return redirect(url_for("student.custom_test_new", subject_id=selected_subject_id, lesson_id=selected_lesson_id) if selected_lesson_id else url_for("student.custom_test_new", subject_id=selected_subject_id))
                    picked.extend(random.sample(level_map["medium"], diff_spec["medium"]))
                if diff_spec["hard"] > 0:
                    if len(level_map["hard"]) < diff_spec["hard"]:
                        flash("لا توجد أسئلة كافية لإنشاء الاختبار.", "error")
                        return redirect(url_for("student.custom_test_new", subject_id=selected_subject_id, lesson_id=selected_lesson_id) if selected_lesson_id else url_for("student.custom_test_new", subject_id=selected_subject_id))
                    picked.extend(random.sample(level_map["hard"], diff_spec["hard"]))

                selected_items.extend(picked)
            else:
                if len(combined_pool) < sel["count"]:
                    flash("لا توجد أسئلة كافية لإنشاء الاختبار.", "error")
                    return redirect(url_for("student.custom_test_new", subject_id=selected_subject_id, lesson_id=selected_lesson_id) if selected_lesson_id else url_for("student.custom_test_new", subject_id=selected_subject_id))
                picked = random.sample(combined_pool, sel["count"])
                selected_items.extend((item_type, item_id) for item_type, item_id, _ in picked)

        # Ensure no duplicates for mixed item types.
        dedup = {}
        for item_type, item_id in selected_items:
            dedup[_pack_custom_item_token(item_type, item_id)] = (item_type, item_id)
        selected_items = list(dedup.values())

        random.shuffle(selected_items)
        question_order = [_pack_custom_item_token(item_type, item_id) for item_type, item_id in selected_items]

        selected_mcq_ids = [item_id for item_type, item_id in selected_items if item_type == "mcq"]
        selected_mcq_rows = list(Question.objects(id__in=selected_mcq_ids).only("id", "choices").all()) if selected_mcq_ids else []
        selected_mcq_by_id = {str(q.id): q for q in selected_mcq_rows}

        answer_order = {}
        for token, (item_type, item_id) in zip(question_order, selected_items):
            if item_type != 'mcq':
                continue
            q_obj = selected_mcq_by_id.get(str(item_id))
            if not q_obj:
                continue
            choices = list(q_obj.choices)
            random.shuffle(choices)
            answer_order[token] = [str(c.choice_id) for c in choices]

        selections_payload = {
            "subject_id": str(selected_subject_id),
            "selection_mode": selection_mode,
            "scopes": selections,
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
        selected_lesson_id=selected_lesson_id,
        selected_lesson=selected_lesson,
        selection_mode=(
            forced_selection_mode
            or (request.form.get('selection_mode', 'test') if request.method == 'POST' else request.args.get('mode', 'test'))
        ),
        lessons=unlocked_lessons,
        tests_data=tests_data,
        lesson_question_counts=lesson_question_counts,
        lesson_difficulty_counts=lesson_difficulty_counts,
        test_question_counts=test_question_counts,
        test_difficulty_counts=test_difficulty_counts,
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

    parsed = []
    mcq_ids = []
    interactive_ids = []
    for token in question_order:
        item_type, item_id = _unpack_custom_item_token(token)
        if not item_type:
            continue
        parsed.append((str(token), item_type, item_id))
        if item_type == 'mcq':
            mcq_ids.append(item_id)
        else:
            interactive_ids.append(item_id)

    questions = Question.objects(id__in=mcq_ids).all() if mcq_ids else []
    interactive_questions = TestInteractiveQuestion.objects(id__in=interactive_ids).all() if interactive_ids else []
    questions_by_id = {str(q.id): q for q in questions}
    interactive_by_id = {str(iq.id): iq for iq in interactive_questions}

    ordered_questions = []
    for token, item_type, item_id in parsed:
        if item_type == 'mcq':
            q = questions_by_id.get(item_id)
            if not q:
                continue
            ordered_choice_ids = answer_order.get(token) or answer_order.get(item_id, [])
            choices = {str(c.choice_id): c for c in q.choices}
            ordered_choices = [choices[cid] for cid in ordered_choice_ids if cid in choices]
            if not ordered_choices:
                ordered_choices = list(q.choices)
            ordered_questions.append({"question": q, "choices": ordered_choices, "question_type": "mcq"})
        else:
            iq = interactive_by_id.get(item_id)
            if not iq:
                continue
            ordered_questions.append({"interactive_question": iq, "choices": [], "question_type": "interactive"})

    time_limit_seconds = (attempt.total * 75) + 15
    return render_template(
        "student/custom_test_take.html",
        attempt=attempt,
        ordered_questions=ordered_questions,
        time_limit_seconds=time_limit_seconds,
        exit_token=secrets.token_hex(8),
    )


@student_bp.route("/custom-tests/<attempt_id>/abandon", methods=["POST"])
@login_required
def custom_test_abandon(attempt_id):
    if (current_user.role or "").lower() != "student":
        return jsonify({"ok": False, "error": "forbidden"}), 403

    attempt = CustomTestAttempt.objects(id=attempt_id).first()
    if not attempt:
        return jsonify({"ok": False, "error": "not_found"}), 404
    if str(attempt.student_id.id) != str(current_user.id):
        return jsonify({"ok": False, "error": "forbidden"}), 403
    if attempt.status != "active":
        profile = _get_or_create_gamification_profile(current_user.id)
        return jsonify({"ok": True, "deducted_xp": 0, "xp_total": int(profile.xp_total or 0)})

    payload = request.get_json(silent=True) if request.is_json else {}
    exit_token = (request.form.get("exit_token") or (payload or {}).get("exit_token") or "")
    exit_token = (exit_token or "").strip()
    if not exit_token:
        return jsonify({"ok": False, "error": "missing_exit_token"}), 400

    source_id = f"{attempt.id}:{exit_token}"
    deducted_xp, profile = _apply_xp_penalty_once(
        student_id=current_user.id,
        event_type="custom_test_abandon_penalty",
        source_id=source_id,
        penalty_amount=TEST_EXIT_XP_PENALTY,
    )
    return jsonify(
        {
            "ok": True,
            "deducted_xp": int(deducted_xp or 0),
            "xp_total": int(profile.xp_total or 0),
        }
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
    parsed = []
    mcq_ids = []
    interactive_ids = []
    for token in question_order:
        item_type, item_id = _unpack_custom_item_token(token)
        if not item_type:
            continue
        parsed.append((str(token), item_type, item_id))
        if item_type == 'mcq':
            mcq_ids.append(item_id)
        else:
            interactive_ids.append(item_id)

    questions = Question.objects(id__in=mcq_ids).all() if mcq_ids else []
    interactive_questions = TestInteractiveQuestion.objects(id__in=interactive_ids).all() if interactive_ids else []
    questions_by_id = {str(q.id): q for q in questions}
    interactive_by_id = {str(iq.id): iq for iq in interactive_questions}

    score = 0
    total = len(parsed)
    for token, item_type, item_id in parsed:
        if item_type == 'mcq':
            q = questions_by_id.get(item_id)
            if not q:
                continue
            selected_choice_id = request.form.get(f"question_{item_id}")
            choice = next((c for c in q.choices if str(c.choice_id) == selected_choice_id), None) if selected_choice_id else None
            if q.correct_choice_id:
                is_correct = bool(choice and choice.choice_id == q.correct_choice_id)
            else:
                is_correct = bool(choice and choice.is_correct)
            if is_correct:
                score += 1
            CustomTestAnswer(
                attempt_id=attempt.id,
                question_id=q,
                choice_id=choice.choice_id if choice else None,
                selected_value=None,
                is_correct=is_correct,
            ).save()
        else:
            iq = interactive_by_id.get(item_id)
            if not iq:
                continue
            raw = (request.form.get(f"interactive_question_{item_id}") or "").strip().lower()
            selected_value = raw == "true"
            is_correct = bool(selected_value)
            if is_correct:
                score += 1
            CustomTestAnswer(
                attempt_id=attempt.id,
                question_id=None,
                interactive_question_id=iq,
                choice_id=None,
                selected_value=selected_value,
                is_correct=is_correct,
            ).save()

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

    parsed = []
    mcq_ids = []
    interactive_ids = []
    for token in question_order:
        item_type, item_id = _unpack_custom_item_token(token)
        if not item_type:
            continue
        parsed.append((str(token), item_type, item_id))
        if item_type == 'mcq':
            mcq_ids.append(item_id)
        else:
            interactive_ids.append(item_id)

    questions = Question.objects(id__in=mcq_ids).all() if mcq_ids else []
    interactive_questions = TestInteractiveQuestion.objects(id__in=interactive_ids).all() if interactive_ids else []
    questions_by_id = {str(q.id): q for q in questions}
    interactive_by_id = {str(iq.id): iq for iq in interactive_questions}

    favorite_mcq_map = {}
    favorite_interactive_map = {}
    if (current_user.role or "").lower() == "student":
        if mcq_ids:
            for fav in StudentFavoriteQuestion.objects(
                student_id=current_user.id,
                question_type="mcq",
                question_id__in=mcq_ids,
            ).only("id", "question_id"):
                if fav.question_id:
                    favorite_mcq_map[str(fav.question_id.id)] = str(fav.id)
        if interactive_ids:
            for fav in StudentFavoriteQuestion.objects(
                student_id=current_user.id,
                question_type="interactive",
                interactive_question_id__in=interactive_ids,
            ).only("id", "interactive_question_id"):
                if fav.interactive_question_id:
                    favorite_interactive_map[str(fav.interactive_question_id.id)] = str(fav.id)

    answers = list(CustomTestAnswer.objects(attempt_id=attempt.id).all())
    answers_by_qid = {str(a.question_id.id): a for a in answers if a.question_id}
    answers_by_iqid = {str(a.interactive_question_id.id): a for a in answers if a.interactive_question_id}

    review = []
    for token, item_type, item_id in parsed:
        if item_type == 'mcq':
            q = questions_by_id.get(item_id)
            if not q:
                continue
            ordered_choice_ids = answer_order.get(token) or answer_order.get(item_id, [])
            choices = {str(c.choice_id): c for c in q.choices}
            ordered_choices = [choices[cid] for cid in ordered_choice_ids if cid in choices]
            if not ordered_choices:
                ordered_choices = list(q.choices)
            ans = answers_by_qid.get(item_id)
            selected_choice = choices.get(str(ans.choice_id)) if ans and ans.choice_id else None
            correct_choice = next((c for c in q.choices if c.is_correct), None)
            review.append({
                "question_type": "mcq",
                "question": q,
                "choices": ordered_choices,
                "selected_choice": selected_choice,
                "correct_choice": correct_choice,
                "is_correct": ans.is_correct if ans else False,
                "is_favorite": item_id in favorite_mcq_map,
                "favorite_id": favorite_mcq_map.get(item_id),
            })
        else:
            iq = interactive_by_id.get(item_id)
            if not iq:
                continue
            ans = answers_by_iqid.get(item_id)
            review.append({
                "question_type": "interactive",
                "question": iq,
                "selected_value": ans.selected_value if ans else False,
                "is_correct": ans.is_correct if ans else False,
                "is_favorite": item_id in favorite_interactive_map,
                "favorite_id": favorite_interactive_map.get(item_id),
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
    if isinstance(selections_payload, dict):
        selections = selections_payload.get("scopes", [])
        if not selections:
            # Backward compatibility with old payloads.
            legacy = selections_payload.get("lessons", [])
            selections = [
                {
                    "scope_type": "lesson",
                    "scope_id": sel.get("lesson_id"),
                    "count": sel.get("count"),
                    "difficulty": sel.get("difficulty"),
                }
                for sel in legacy
            ]
    else:
        selections = []

    selected_items = []
    for sel in selections:
        scope_type = (sel.get("scope_type") or "lesson").strip().lower()
        scope_id = sel.get("scope_id")
        count = _to_int(sel.get("count"), 0)
        if not scope_id or count <= 0:
            continue

        if scope_type == "test":
            test_ids = [scope_id] if ObjectId.is_valid(str(scope_id)) else []
        else:
            tests = Test.objects(lesson_id=scope_id).all() if ObjectId.is_valid(str(scope_id)) else []
            test_ids = [t.id for t in tests]

        mcq_pool = list(Question.objects(test_id__in=test_ids)) if test_ids else []
        interactive_pool = list(TestInteractiveQuestion.objects(test_id__in=test_ids)) if test_ids else []
        combined_pool = [("mcq", q) for q in mcq_pool] + [("interactive", iq) for iq in interactive_pool]

        if "difficulty" in sel and isinstance(sel["difficulty"], dict):
            diff_spec = sel["difficulty"]
            level_map = {"easy": [], "medium": [], "hard": []}
            for item_type, obj in combined_pool:
                diff = (getattr(obj, "difficulty", "medium") or "medium").lower()
                if diff not in level_map:
                    diff = "medium"
                level_map[diff].append((item_type, obj))

            requested = {
                "easy": _to_int(diff_spec.get("easy"), 0),
                "medium": _to_int(diff_spec.get("medium"), 0),
                "hard": _to_int(diff_spec.get("hard"), 0),
            }
            available = {
                "easy": len(level_map["easy"]),
                "medium": len(level_map["medium"]),
                "hard": len(level_map["hard"]),
            }
            allocated = _rebalance_difficulty_request(requested, available)

            picked = []
            if allocated["easy"]:
                picked.extend(random.sample(level_map["easy"], allocated["easy"]))
            if allocated["medium"]:
                picked.extend(random.sample(level_map["medium"], allocated["medium"]))
            if allocated["hard"]:
                picked.extend(random.sample(level_map["hard"], allocated["hard"]))

            if len(picked) < count:
                flash("لا توجد أسئلة كافية لإعادة الاختبار بنفس الإعدادات.", "error")
                return redirect(url_for("student.custom_test_result", attempt_id=source.id))
            selected_items.extend(picked)
        else:
            if len(combined_pool) < count:
                flash("لا توجد أسئلة كافية لإعادة الاختبار بنفس الإعدادات.", "error")
                return redirect(url_for("student.custom_test_result", attempt_id=source.id))
            selected_items.extend(random.sample(combined_pool, count))

    # Keep unique items and randomize like original custom test generation.
    dedup = {}
    for item_type, obj in selected_items:
        dedup[_pack_custom_item_token(item_type, obj.id)] = (item_type, obj)
    selected_items = list(dedup.values())
    random.shuffle(selected_items)
    question_order = [_pack_custom_item_token(item_type, obj.id) for item_type, obj in selected_items]

    answer_order = {}
    for token, (item_type, obj) in zip(question_order, selected_items):
        if item_type != 'mcq':
            continue
        choices = list(obj.choices)
        random.shuffle(choices)
        answer_order[token] = [str(c.choice_id) for c in choices]

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
            if access.subject_requires_code and not access.subject_open:
                flash("قم بتفعيل المادة لعرض هذا الدرس.", "warning")
                return redirect(url_for("student.activate_subject", subject_id=section.subject.id))
            flash("قم بتفعيل القسم لعرض هذا الدرس.", "warning")
            return redirect(url_for("student.activate_section", section_id=section.id))
    
    # Fetch flashcards from URL
    try:
        from urllib.request import Request, urlopen
        
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

        req = Request(fetch_url, headers={"User-Agent": "EduPath/1.0"})
        with urlopen(req, timeout=7) as response:
            raw = response.read().decode("utf-8")
        data = json.loads(raw)
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
