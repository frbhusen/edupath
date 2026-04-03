from flask import Blueprint, render_template, redirect, url_for, flash, request, Response
import json
import os
from datetime import datetime, timedelta
from flask_login import login_required, current_user
from werkzeug.exceptions import NotFound
from mongoengine.errors import DoesNotExist
from bson import ObjectId
from bson.dbref import DBRef

try:
    from fpdf import FPDF
except Exception:  # pragma: no cover
    FPDF = None

from .models import (
    User, Subject, Section, Lesson, LessonResource, Test, Question, Choice, 
    ActivationCode, SectionActivation, LessonActivation, LessonActivationCode,
    SubjectActivation, SubjectActivationCode, Attempt, AttemptAnswer, CustomTestAttempt, CustomTestAnswer,
    StudentGamification, XPEvent, Assignment, AssignmentSubmission, LessonCompletion,
    StudyPlan, StudyPlanItem, AssignmentAttempt,
    DiscussionQuestion, DiscussionAnswer, Certificate, Duel, DuelAnswer, DuelStats,
    CourseSet, CourseQuestion, TestInteractiveQuestion
)
from .activation_utils import (
    cascade_subject_activation, cascade_section_activation, cascade_lesson_activation,
    revoke_subject_activation, revoke_section_activation, lock_subject_access_for_all, lock_section_access_for_all
)
from .forms import SubjectForm, SectionForm, LessonForm, TestForm, StudentEditForm
from .extensions import cache
from .permissions import is_admin, is_question_editor, has_subject_access, get_staff_subject_ids
from .account_cleanup import delete_user_with_related_data

teacher_bp = Blueprint("teacher", __name__, template_folder="templates")


def _generate_unique_code(model_cls, length: int = 6) -> str:
    """Generate a unique activation code for the given model class."""
    import random
    import string

    code_value = ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))
    while model_cls.objects(code=code_value).first():
        code_value = ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))
    return code_value


def _award_certificate_xp_once(student_id, certificate_id, amount: int = 30):
    if not student_id or not certificate_id:
        return 0

    source_id = str(certificate_id)
    event = XPEvent.objects(
        student_id=student_id,
        event_type="certificate_earned",
        source_id=source_id,
    ).first()
    if event:
        return 0

    xp_amount = max(0, int(amount or 0))
    XPEvent(
        student_id=student_id,
        event_type="certificate_earned",
        source_id=source_id,
        xp=xp_amount,
    ).save()

    profile = StudentGamification.objects(student_id=student_id).first()
    if not profile:
        profile = StudentGamification(student_id=student_id, xp_total=0, level=1)

    profile.xp_total = int(profile.xp_total or 0) + xp_amount
    profile.level = (int(profile.xp_total or 0) // 200) + 1
    profile.updated_at = datetime.utcnow()
    profile.save()
    return xp_amount


def _extract_drive_file_id(url: str):
    lowered = (url or "").lower()
    if "/file/d/" in lowered:
        return (url or "").split("/file/d/")[-1].split("/")[0]
    if "id=" in lowered:
        return (url or "").split("id=")[-1].split("&")[0]
    return None


def _normalize_image_url(url: str):
    cleaned = (url or "").strip()
    if not cleaned:
        return None
    lowered = cleaned.lower()
    if "drive.google.com" in lowered:
        file_id = _extract_drive_file_id(cleaned)
        if file_id:
            return f"https://drive.google.com/uc?export=view&id={file_id}"
    return cleaned

# Role guard decorator

def role_required(*roles):
    def decorator(fn):
        from functools import wraps
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated:
                flash("ليس لديك صلاحية للوصول إلى هذه الصفحة.", "error")
                return redirect(url_for("auth.login"))
            allowed_roles = {"admin", *[str(r).lower() for r in roles]}
            if (current_user.role or "").lower() not in allowed_roles:
                flash("ليس لديك صلاحية للوصول إلى هذه الصفحة.", "error")
                return redirect(url_for("auth.login"))
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def _deny_permission():
    flash("ليس لديك صلاحية للوصول إلى هذه الصفحة.", "error")
    return redirect(url_for("auth.login"))


def _ensure_subject_scope(subject_id):
    if is_admin(current_user):
        return None
    if has_subject_access(current_user, subject_id):
        return None
    return _deny_permission()


def _ensure_scope_for_section(section):
    if not section or not section.subject_id:
        return _deny_permission()
    return _ensure_subject_scope(section.subject_id.id)


def _ensure_scope_for_lesson(lesson):
    if not lesson or not lesson.section_id:
        return _deny_permission()
    return _ensure_scope_for_section(lesson.section_id)


def _ensure_scope_for_test(test):
    if not test or not test.section_id:
        return _deny_permission()
    return _ensure_scope_for_section(test.section_id)


def _allowed_subject_ids_for_current_user():
    if is_admin(current_user):
        return None
    return set(get_staff_subject_ids(current_user.id))


def _subject_allowed_for_current_user(subject_id):
    allowed = _allowed_subject_ids_for_current_user()
    if allowed is None:
        return True
    if not subject_id:
        return False
    return subject_id in allowed


def _subject_id_for_lesson(lesson):
    if not lesson or not lesson.section_id or not lesson.section_id.subject_id:
        return None
    return lesson.section_id.subject_id.id


def _subject_id_for_section(section):
    if not section or not section.subject_id:
        return None
    return section.subject_id.id


def _subject_id_for_test(test):
    if not test or not test.section_id or not test.section_id.subject_id:
        return None
    return test.section_id.subject_id.id


def _subject_id_for_course_set(course_set):
    if not course_set or not course_set.subject_id:
        return None
    return course_set.subject_id.id


def _subject_id_for_assignment(assignment):
    if not assignment:
        return None
    if assignment.subject_id:
        return assignment.subject_id.id
    if assignment.section_id and assignment.section_id.subject_id:
        return assignment.section_id.subject_id.id
    if assignment.lesson_id and assignment.lesson_id.section_id and assignment.lesson_id.section_id.subject_id:
        return assignment.lesson_id.section_id.subject_id.id
    return None


def _custom_attempt_subject_id(custom_attempt):
    if not custom_attempt:
        return None

    question_ids = []
    try:
        payload = json.loads(custom_attempt.selections_json or "[]")
        if isinstance(payload, list):
            question_ids = [qid for qid in payload if ObjectId.is_valid(str(qid))]
    except Exception:
        question_ids = []

    if not question_ids:
        return None

    question = Question.objects(id=ObjectId(str(question_ids[0]))).first()
    if not question or not question.test_id:
        return None
    return _subject_id_for_test(question.test_id)


def _aggregate_question_counts_by_test(model_cls, test_ids):
    """Return {test_id: count} using Mongo aggregation without loading all question docs."""
    counts = {}
    if not test_ids:
        return counts

    try:
        pipeline = [
            {"$match": {"test_id": {"$in": test_ids}}},
            {"$group": {"_id": "$test_id", "count": {"$sum": 1}}},
        ]
        for row in model_cls._get_collection().aggregate(pipeline, allowDiskUse=True):
            test_ref = row.get("_id")
            if isinstance(test_ref, DBRef):
                test_ref = test_ref.id
            if test_ref is not None:
                counts[test_ref] = int(row.get("count", 0))
    except Exception:
        # Fall back to zero counts on aggregation edge-cases instead of blocking dashboard rendering.
        return {}
    return counts

@teacher_bp.route("/dashboard")
@login_required
@role_required("teacher")
@cache.cached(timeout=180, key_prefix=lambda: f"teacher_dashboard_{current_user.id}_{request.args.get('page', 1)}")
def dashboard():
    # Keep payload predictable so large datasets don't hit Heroku timeouts.
    page = request.args.get('page', 1, type=int)
    per_page = 5
    max_rows_per_section = 20
    
    if is_admin(current_user):
        subjects_query = Subject.objects().order_by('created_at')
    else:
        allowed_ids = list(_allowed_subject_ids_for_current_user() or [])
        subjects_query = Subject.objects(id__in=allowed_ids).order_by('created_at')
    total_subjects = subjects_query.count()
    subjects = list(subjects_query.skip((page - 1) * per_page).limit(per_page))
    
    # Bulk load sections, lessons, and tests to avoid N+1 in templates
    if subjects:
        subject_ids = [s.id for s in subjects]
        sections = list(
            Section.objects(subject_id__in=subject_ids)
            .only("id", "title", "requires_code", "subject_id")
            .all()
        )
        
        # Group sections by subject
        sections_by_subject = {}
        for section in sections:
            try:
                subject_id = section.subject_id.id if section.subject_id else None
            except (DoesNotExist, AttributeError):
                continue
            if not subject_id:
                continue
            if subject_id not in sections_by_subject:
                sections_by_subject[subject_id] = []
            sections_by_subject[subject_id].append(section)
        
        # Bulk load lessons and tests
        section_ids = [s.id for s in sections]
        lessons = list(
            Lesson.objects(section_id__in=section_ids)
            .only("id", "title", "link_label", "link_url", "requires_code", "section_id", "allow_full_lesson_test")
            .all()
        )
        tests = list(
            Test.objects(section_id__in=section_ids)
            .only("id", "title", "requires_code", "section_id", "lesson_id")
            .all()
        )

        # Count attached lesson resources in bulk to avoid per-lesson property queries.
        lesson_ids = [lesson.id for lesson in lessons]
        resource_counts = {}
        if lesson_ids:
            try:
                pipeline = [
                    {"$match": {"lesson_id": {"$in": lesson_ids}}},
                    {"$group": {"_id": "$lesson_id", "count": {"$sum": 1}}},
                ]
                for row in LessonResource._get_collection().aggregate(pipeline, allowDiskUse=True):
                    lesson_ref = row.get("_id")
                    if isinstance(lesson_ref, DBRef):
                        lesson_ref = lesson_ref.id
                    if lesson_ref is not None:
                        resource_counts[lesson_ref] = int(row.get("count", 0))
            except Exception:
                resource_counts = {}

        # Count questions with aggregation to avoid loading all question docs into Python.
        test_ids = [t.id for t in tests]
        mcq_counts = _aggregate_question_counts_by_test(Question, test_ids)
        interactive_counts = _aggregate_question_counts_by_test(TestInteractiveQuestion, test_ids)
        lesson_title_by_id = {lesson.id: lesson.title for lesson in lessons}
        
        # Group by section
        lessons_by_section = {}
        tests_by_section = {}
        tests_by_lesson = {}
        
        for lesson in lessons:
            try:
                section_id = lesson.section_id.id if lesson.section_id else None
            except (DoesNotExist, AttributeError):
                continue
            if not section_id:
                continue
            if section_id not in lessons_by_section:
                lessons_by_section[section_id] = []
            lessons_by_section[section_id].append(lesson)
        
        for test in tests:
            try:
                section_id = test.section_id.id if test.section_id else None
            except (DoesNotExist, AttributeError):
                continue
            if not section_id:
                continue
            if section_id not in tests_by_section:
                tests_by_section[section_id] = []
            tests_by_section[section_id].append(test)
            test._cached_question_count = mcq_counts.get(test.id, 0) + interactive_counts.get(test.id, 0)
            test._cached_lesson_title = "اختبار شامل للقسم"
            
            # Also group by lesson if test is linked to lesson
            if test.lesson_id:
                try:
                    lesson_id = test.lesson_id.id
                except (DoesNotExist, AttributeError):
                    lesson_id = None
                if not lesson_id:
                    continue
                test._cached_lesson_title = lesson_title_by_id.get(lesson_id, "درس محذوف")
                if lesson_id not in tests_by_lesson:
                    tests_by_lesson[lesson_id] = []
                tests_by_lesson[lesson_id].append(test)
        
        # Attach to subjects for template use
        for subject in subjects:
            subject._cached_sections = sections_by_subject.get(subject.id, [])
            for section in subject._cached_sections:
                section_lessons = lessons_by_section.get(section.id, [])
                section_tests = tests_by_section.get(section.id, [])
                section._cached_total_lessons = len(section_lessons)
                section._cached_total_tests = len(section_tests)
                section._cached_lessons = section_lessons[:max_rows_per_section]
                section._cached_tests = section_tests[:max_rows_per_section]
                # Also attach test counts to lessons
                for lesson in section._cached_lessons:
                    lesson._cached_test_count = len(tests_by_lesson.get(lesson.id, []))
                    lesson._cached_resource_count = resource_counts.get(lesson.id, 0) + (
                        1 if lesson.link_label and lesson.link_url else 0
                    )
    
    # Calculate pagination info
    total_pages = (total_subjects + per_page - 1) // per_page
    has_prev = page > 1
    has_next = page < total_pages
    
    primary_subject = subjects[0] if subjects else None

    return render_template(
        "teacher/dashboard.html", 
        subjects=subjects,
        primary_subject=primary_subject,
        page=page,
        total_pages=total_pages,
        has_prev=has_prev,
        has_next=has_next,
        total_subjects=total_subjects
    )


# Redirect teacher base to dashboard for Up navigation
@teacher_bp.route("/")
@login_required
@role_required("teacher", "question_editor")
def root():
    if is_question_editor(current_user):
        return redirect(url_for("teacher.question_editor_dashboard"))
    return redirect(url_for("teacher.dashboard"))


@teacher_bp.route("/question-editor", methods=["GET"])
@login_required
@role_required("question_editor", "admin")
def question_editor_dashboard():
    allowed_subject_ids = _allowed_subject_ids_for_current_user()

    tests_q = Test.objects()
    if allowed_subject_ids is not None:
        allowed_sections = [s.id for s in Section.objects(subject_id__in=list(allowed_subject_ids)).only("id").all()]
        tests_q = tests_q.filter(section_id__in=allowed_sections)

    tests = list(tests_q.order_by("created_at").all())
    return render_template("teacher/question_editor_dashboard.html", tests=tests)


@teacher_bp.route("/results")
@login_required
@role_required("teacher")
def results_overview():
    # Pagination for performance
    page = request.args.get('page', 1, type=int)
    per_page = 50

    regular_attempts = list(Attempt.objects().all())
    custom_attempts = list(CustomTestAttempt.objects(status="submitted").all())
    allowed_subject_ids = _allowed_subject_ids_for_current_user()

    # Merge both regular and custom attempts for the management overview.
    attempts = []
    for attempt in regular_attempts:
        try:
            _ = attempt.student_id.id
            if allowed_subject_ids is not None:
                sid = _subject_id_for_test(attempt.test_id)
                if sid not in allowed_subject_ids:
                    continue
            attempt._result_type = "regular"
            attempt._taken_at = attempt.started_at
            attempts.append(attempt)
        except Exception:
            continue

    for attempt in custom_attempts:
        try:
            _ = attempt.student_id.id
            if allowed_subject_ids is not None:
                sid = _custom_attempt_subject_id(attempt)
                if sid not in allowed_subject_ids:
                    continue
            attempt._result_type = "custom"
            attempt._taken_at = attempt.created_at
            attempts.append(attempt)
        except Exception:
            continue

    attempts.sort(key=lambda a: a._taken_at or datetime.min, reverse=True)
    total_attempts = len(attempts)
    attempts = attempts[(page - 1) * per_page : page * per_page]
    
    total_pages = (total_attempts + per_page - 1) // per_page
    has_prev = page > 1
    has_next = page < total_pages
    
    return render_template(
        "teacher/results.html", 
        attempts=attempts,
        page=page,
        total_pages=total_pages,
        has_prev=has_prev,
        has_next=has_next
    )


@teacher_bp.route("/students/<student_id>/results")
@login_required
@role_required("teacher")
def student_results(student_id):
    student = User.objects(id=student_id).first()
    if not student:
        raise NotFound()
    
    # Pagination
    page = request.args.get('page', 1, type=int)
    per_page = 30

    regular_attempts = list(Attempt.objects(student_id=student.id).all())
    custom_attempts = list(
        CustomTestAttempt.objects(student_id=student.id, status="submitted").all()
    )
    allowed_subject_ids = _allowed_subject_ids_for_current_user()

    attempts = []
    for attempt in regular_attempts:
        if allowed_subject_ids is not None:
            sid = _subject_id_for_test(attempt.test_id)
            if sid not in allowed_subject_ids:
                continue
        attempt._result_type = "regular"
        attempt._taken_at = attempt.started_at
        attempts.append(attempt)

    for attempt in custom_attempts:
        if allowed_subject_ids is not None:
            sid = _custom_attempt_subject_id(attempt)
            if sid not in allowed_subject_ids:
                continue
        attempt._result_type = "custom"
        attempt._taken_at = attempt.created_at
        attempts.append(attempt)

    attempts.sort(key=lambda a: a._taken_at or datetime.min, reverse=True)
    total_attempts = len(attempts)
    attempts = attempts[(page - 1) * per_page : page * per_page]
    
    total_pages = (total_attempts + per_page - 1) // per_page
    has_prev = page > 1
    has_next = page < total_pages
    
    return render_template(
        "teacher/student_results.html", 
        student=student, 
        attempts=attempts,
        page=page,
        total_pages=total_pages,
        has_prev=has_prev,
        has_next=has_next
    )


@teacher_bp.route("/attempts/<attempt_id>", methods=["GET", "POST"])
@login_required
@role_required("teacher")
def manage_attempt(attempt_id):
    attempt = Attempt.objects(id=attempt_id).first()
    if not attempt:
        raise NotFound()
    if not _subject_allowed_for_current_user(_subject_id_for_test(attempt.test_id)):
        return _deny_permission()
    student = attempt.student_id
    test = attempt.test_id
    questions = Question.objects(test_id=test.id).all()

    if request.method == "POST":
        action = request.form.get("action")
        if action == "delete":
            # Delete answers then attempt
            AttemptAnswer.objects(attempt_id=attempt.id).delete()
            attempt.delete()
            flash("تم حذف محاولة الاختبار بنجاح.", "success")
            return_student_id = (request.form.get("return_student_id") or "").strip()
            if return_student_id and ObjectId.is_valid(return_student_id):
                return redirect(url_for("teacher.student_results", student_id=return_student_id))
            return redirect(url_for("teacher.results_overview"))
        elif action == "save_scores":
            # Update scores from form
            for q in questions:
                choice_id_val = request.form.get(f"question_{q.id}")
                choice = None
                if choice_id_val:
                    choice = next((c for c in q.choices if str(c.choice_id) == choice_id_val), None)
                
                ans = AttemptAnswer.objects(attempt_id=attempt.id, question_id=q.id).first()
                if not ans:
                    ans = AttemptAnswer(attempt_id=attempt.id, question_id=q.id)
                ans.choice_id = choice.choice_id if choice else None
                ans.is_correct = choice.is_correct if choice else False
                ans.save()

            mcq_score = sum(1 for aa in AttemptAnswer.objects(attempt_id=attempt.id) if aa.is_correct)
            mcq_total = len(questions)
            attempt.score = mcq_score
            attempt.total = mcq_total
            attempt.save()
            flash("تم حفظ الدرجات بنجاح.", "success")
            return redirect(url_for("teacher.student_results", student_id=student.id))

    answers = {aa.question_id: aa for aa in AttemptAnswer.objects(attempt_id=attempt.id).all()}
    return render_template(
        "teacher/attempt_manage.html",
        attempt=attempt,
        student=student,
        test=test,
        questions=questions,
        answers=answers,
    )


@teacher_bp.route("/custom-attempts/<attempt_id>/delete", methods=["POST"])
@login_required
@role_required("teacher")
def delete_custom_attempt(attempt_id):
    attempt = CustomTestAttempt.objects(id=attempt_id).first() if ObjectId.is_valid(attempt_id) else None
    if not attempt:
        flash("محاولة الاختبار المخصص غير موجودة.", "error")
        return redirect(url_for("teacher.results_overview"))
    if not _subject_allowed_for_current_user(_custom_attempt_subject_id(attempt)):
        return _deny_permission()

    CustomTestAnswer.objects(attempt_id=attempt.id).delete()
    attempt.delete()
    flash("تم حذف محاولة الاختبار المخصص.", "success")

    return_student_id = (request.form.get("return_student_id") or "").strip()
    if return_student_id and ObjectId.is_valid(return_student_id):
        return redirect(url_for("teacher.student_results", student_id=return_student_id))
    return redirect(url_for("teacher.results_overview"))


@teacher_bp.route("/my-students", methods=["GET"])
@login_required
@role_required("teacher")
def my_students():
    allowed_subject_ids = _allowed_subject_ids_for_current_user()
    selected_subject = None
    if allowed_subject_ids is None:
        return redirect(url_for("teacher.students"))
    scoped_subjects = list(Subject.objects(id__in=list(allowed_subject_ids)).order_by("created_at").all())
    selected_subject_id = (request.args.get("subject_id") or "").strip()
    if selected_subject_id and ObjectId.is_valid(selected_subject_id):
        candidate = Subject.objects(id=selected_subject_id).first()
        if candidate and candidate.id in allowed_subject_ids:
            selected_subject = candidate
    if not selected_subject:
        selected_subject = scoped_subjects[0] if scoped_subjects else None

    if not selected_subject:
        return render_template("teacher/students.html", students=[], selected_subject=None)

    section_ids = [s.id for s in Section.objects(subject_id=selected_subject.id).only("id").all()]
    lesson_ids = [l.id for l in Lesson.objects(section_id__in=section_ids).only("id").all()] if section_ids else []
    test_ids = [t.id for t in Test.objects(section_id__in=section_ids).only("id").all()] if section_ids else []

    student_ids = set()
    for row in SubjectActivation.objects(subject_id=selected_subject.id).only("student_id").all():
        if row.student_id:
            student_ids.add(row.student_id.id)
    for row in SectionActivation.objects(section_id__in=section_ids).only("student_id").all():
        if row.student_id:
            student_ids.add(row.student_id.id)
    for row in LessonActivation.objects(lesson_id__in=lesson_ids).only("student_id").all():
        if row.student_id:
            student_ids.add(row.student_id.id)
    for row in Attempt.objects(test_id__in=test_ids).only("student_id").all():
        if row.student_id:
            student_ids.add(row.student_id.id)

    if student_ids:
        students = User.objects(role="student", id__in=list(student_ids)).order_by('-created_at').all()
    else:
        students = []

    return render_template("teacher/students.html", students=students, selected_subject=selected_subject)


@teacher_bp.route("/students", methods=["GET"])
@login_required
@role_required("admin")
def students():
    students = User.objects(role="student").order_by('-created_at').all()
    return render_template("teacher/students.html", students=students, selected_subject=None)


@teacher_bp.route("/students/<student_id>/delete", methods=["POST"])
@login_required
@role_required("admin")
def delete_student(student_id):
    student = User.objects(id=student_id, role="student").first() if ObjectId.is_valid(student_id) else None
    if not student:
        flash("الطالب غير موجود.", "error")
        return redirect(url_for("teacher.students"))
    delete_user_with_related_data(student)

    flash("تم حذف الطالب وجميع بياناته المرتبطة بنجاح.", "success")
    return redirect(url_for("teacher.students"))


@teacher_bp.route("/discussions", methods=["GET", "POST"])
@login_required
@role_required("teacher")
def discussions_manage():
    allowed_subject_ids = _allowed_subject_ids_for_current_user()

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()

        if action == "add_answer":
            question_id = (request.form.get("question_id") or "").strip()
            body = (request.form.get("body") or "").strip()
            question = DiscussionQuestion.objects(id=question_id).first() if ObjectId.is_valid(question_id) else None
            if question and not _subject_allowed_for_current_user(_subject_id_for_lesson(question.lesson_id)):
                return _deny_permission()
            if not question or not body:
                flash("تعذر إضافة الرد. تأكد من صحة البيانات.", "error")
                return redirect(url_for("teacher.discussions_manage"))

            DiscussionAnswer(
                question_id=question.id,
                author_id=current_user.id,
                body=body,
            ).save()
            flash("تمت إضافة الرد بنجاح.", "success")
            return redirect(url_for("teacher.discussions_manage"))

        if action == "toggle_pin":
            question_id = (request.form.get("question_id") or "").strip()
            question = DiscussionQuestion.objects(id=question_id).first() if ObjectId.is_valid(question_id) else None
            if question and not _subject_allowed_for_current_user(_subject_id_for_lesson(question.lesson_id)):
                return _deny_permission()
            if question:
                question.is_pinned = not bool(getattr(question, "is_pinned", False))
                question.save()
                flash("تم تحديث حالة تثبيت السؤال.", "success")
            return redirect(url_for("teacher.discussions_manage"))

        if action == "toggle_resolved":
            question_id = (request.form.get("question_id") or "").strip()
            question = DiscussionQuestion.objects(id=question_id).first() if ObjectId.is_valid(question_id) else None
            if question and not _subject_allowed_for_current_user(_subject_id_for_lesson(question.lesson_id)):
                return _deny_permission()
            if question:
                question.is_resolved = not bool(question.is_resolved)
                question.save()
                flash("تم تحديث حالة السؤال.", "success")
            return redirect(url_for("teacher.discussions_manage"))

        if action == "delete_question":
            question_id = (request.form.get("question_id") or "").strip()
            question = DiscussionQuestion.objects(id=question_id).first() if ObjectId.is_valid(question_id) else None
            if question and not _subject_allowed_for_current_user(_subject_id_for_lesson(question.lesson_id)):
                return _deny_permission()
            if question:
                DiscussionAnswer.objects(question_id=question.id).delete()
                question.delete()
                flash("تم حذف السؤال وجميع الردود.", "success")
            return redirect(url_for("teacher.discussions_manage"))

        if action == "delete_answer":
            answer_id = (request.form.get("answer_id") or "").strip()
            answer = DiscussionAnswer.objects(id=answer_id).first() if ObjectId.is_valid(answer_id) else None
            if answer and answer.question_id and not _subject_allowed_for_current_user(_subject_id_for_lesson(answer.question_id.lesson_id)):
                return _deny_permission()
            if answer:
                answer.delete()
                flash("تم حذف الرد.", "success")
            return redirect(url_for("teacher.discussions_manage"))

    lesson_id = (request.args.get("lesson_id") or "").strip()
    pinned_only = (request.args.get("pinned") or "").strip().lower() in {"1", "true", "yes", "on"}
    questions_q = DiscussionQuestion.objects()
    if pinned_only:
        questions_q = questions_q.filter(is_pinned=True)
    if allowed_subject_ids is not None:
        allowed_sections = [s.id for s in Section.objects(subject_id__in=list(allowed_subject_ids)).only("id").all()]
        allowed_lessons = [l.id for l in Lesson.objects(section_id__in=allowed_sections).only("id").all()] if allowed_sections else []
        questions_q = questions_q.filter(lesson_id__in=allowed_lessons)
    if lesson_id and ObjectId.is_valid(lesson_id):
        lesson_obj = Lesson.objects(id=ObjectId(lesson_id)).first()
        if lesson_obj and _subject_allowed_for_current_user(_subject_id_for_lesson(lesson_obj)):
            questions_q = questions_q.filter(lesson_id=ObjectId(lesson_id))
        else:
            questions_q = questions_q.filter(id=None)

    questions = list(questions_q.order_by("-is_pinned", "is_resolved", "-created_at").all())
    answers = list(DiscussionAnswer.objects(question_id__in=[q.id for q in questions]).order_by("created_at").all()) if questions else []

    answers_by_question = {}
    user_ids = set()
    lesson_ids = set()
    for q in questions:
        if q.author_id:
            user_ids.add(q.author_id.id)
        if q.lesson_id:
            lesson_ids.add(q.lesson_id.id)
    for ans in answers:
        qid = ans.question_id.id if ans.question_id else None
        if qid is None:
            continue
        answers_by_question.setdefault(qid, []).append(ans)
        if ans.author_id:
            user_ids.add(ans.author_id.id)

    users_by_id = {u.id: u for u in User.objects(id__in=list(user_ids)).all()} if user_ids else {}
    lessons = list(Lesson.objects(id__in=list(lesson_ids)).all()) if lesson_ids else []
    lessons_by_id = {l.id: l for l in lessons}
    all_lessons_q = Lesson.objects()
    if allowed_subject_ids is not None:
        allowed_sections = [s.id for s in Section.objects(subject_id__in=list(allowed_subject_ids)).only("id").all()]
        all_lessons_q = all_lessons_q.filter(section_id__in=allowed_sections)
    all_lessons = list(all_lessons_q.order_by("created_at").all())

    return render_template(
        "teacher/discussions_manage.html",
        questions=questions,
        answers_by_question=answers_by_question,
        users_by_id=users_by_id,
        lessons_by_id=lessons_by_id,
        all_lessons=all_lessons,
        selected_lesson_id=lesson_id,
        pinned_only=pinned_only,
    )


@teacher_bp.route("/certificates", methods=["GET", "POST"])
@login_required
@role_required("teacher")
def certificates_manage():
    allowed_subject_ids = _allowed_subject_ids_for_current_user()

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()

        if action == "delete":
            certificate_id = (request.form.get("certificate_id") or "").strip()
            cert = Certificate.objects(id=certificate_id).first() if ObjectId.is_valid(certificate_id) else None
            if cert and not _subject_allowed_for_current_user(_subject_id_for_lesson(cert.lesson_id)):
                return _deny_permission()
            if cert:
                cert.delete()
                flash("تم حذف الشهادة.", "success")
            return redirect(url_for("teacher.certificates_manage"))

        if action == "issue":
            student_id = (request.form.get("student_id") or "").strip()
            lesson_id = (request.form.get("lesson_id") or "").strip()
            certificate_url = (request.form.get("certificate_url") or "").strip()
            student = User.objects(id=student_id, role="student").first() if ObjectId.is_valid(student_id) else None
            lesson = Lesson.objects(id=lesson_id).first() if ObjectId.is_valid(lesson_id) else None
            if lesson and not _subject_allowed_for_current_user(_subject_id_for_lesson(lesson)):
                return _deny_permission()
            if not student or not lesson:
                flash("بيانات الطالب أو الدرس غير صحيحة.", "error")
                return redirect(url_for("teacher.certificates_manage"))
            if not certificate_url or not (certificate_url.startswith("http://") or certificate_url.startswith("https://")):
                flash("رابط الشهادة غير صالح. أدخل رابطًا يبدأ بـ http أو https.", "error")
                return redirect(url_for("teacher.certificates_manage"))
            existing = Certificate.objects(student_id=student.id, lesson_id=lesson.id).first()
            if existing:
                existing.certificate_url = certificate_url
                existing.issued_at = datetime.utcnow()
                existing.is_verified = False
                existing.verified_by = None
                existing.verified_at = None
                existing.save()
                flash("تم تحديث رابط الشهادة وإعادة تعيين حالة التحقق.", "success")
            else:
                Certificate(
                    student_id=student.id,
                    lesson_id=lesson.id,
                    certificate_url=certificate_url,
                    issued_at=datetime.utcnow(),
                    is_verified=False,
                ).save()
                flash("تم إضافة الشهادة بالرابط بنجاح.", "success")
            return redirect(url_for("teacher.certificates_manage"))

    certificates = list(Certificate.objects().order_by("-issued_at").all())
    if allowed_subject_ids is not None:
        certificates = [
            c for c in certificates
            if _subject_id_for_lesson(c.lesson_id) in allowed_subject_ids
        ]
    student_ids = [c.student_id.id for c in certificates if c.student_id]
    lesson_ids = [c.lesson_id.id for c in certificates if c.lesson_id]
    students_map = {u.id: u for u in User.objects(id__in=student_ids).all()} if student_ids else {}
    lessons_map = {l.id: l for l in Lesson.objects(id__in=lesson_ids).all()} if lesson_ids else {}

    all_students = list(User.objects(role="student").order_by("first_name", "last_name").all())
    all_lessons_q = Lesson.objects()
    if allowed_subject_ids is not None:
        allowed_sections = [s.id for s in Section.objects(subject_id__in=list(allowed_subject_ids)).only("id").all()]
        all_lessons_q = all_lessons_q.filter(section_id__in=allowed_sections)
    all_lessons = list(all_lessons_q.order_by("created_at").all())

    return render_template(
        "teacher/certificates_manage.html",
        certificates=certificates,
        students_map=students_map,
        lessons_map=lessons_map,
        all_students=all_students,
        all_lessons=all_lessons,
    )


@teacher_bp.route("/certificates/<certificate_id>/download", methods=["GET"])
@login_required
@role_required("teacher")
def download_certificate_teacher(certificate_id):
    cert = Certificate.objects(id=certificate_id).first() if ObjectId.is_valid(certificate_id) else None
    if not cert:
        raise NotFound()
    if not _subject_allowed_for_current_user(_subject_id_for_lesson(cert.lesson_id)):
        return _deny_permission()
    cert_url = (getattr(cert, "certificate_url", "") or "").strip()
    if not cert_url:
        flash("لا يوجد رابط شهادة لهذا السجل.", "error")
        return redirect(url_for("teacher.certificates_manage"))
    return redirect(cert_url)


@teacher_bp.route("/certificates/verification", methods=["GET", "POST"])
@login_required
@role_required("teacher")
def certificates_verification():
    allowed_subject_ids = _allowed_subject_ids_for_current_user()

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()
        certificate_id = (request.form.get("certificate_id") or "").strip()
        cert = Certificate.objects(id=certificate_id).first() if ObjectId.is_valid(certificate_id) else None
        if cert and not _subject_allowed_for_current_user(_subject_id_for_lesson(cert.lesson_id)):
            return _deny_permission()
        if not cert:
            flash("الشهادة غير موجودة.", "error")
            return redirect(url_for("teacher.certificates_verification"))

        if action == "mark_verified":
            cert.is_verified = True
            cert.verified_at = datetime.utcnow()
            cert.verified_by = current_user.id
            cert.save()
            xp_awarded = _award_certificate_xp_once(
                student_id=(cert.student_id.id if cert.student_id else None),
                certificate_id=cert.id,
            )
            if xp_awarded > 0:
                flash(f"تم اعتماد الشهادة وإضافة {xp_awarded} XP للطالب.", "success")
            else:
                flash("تم اعتماد الشهادة.", "success")
            return redirect(url_for("teacher.certificates_verification"))

        if action == "mark_unverified":
            cert.is_verified = False
            cert.verified_at = None
            cert.verified_by = None
            cert.save()
            flash("تم إلغاء اعتماد الشهادة.", "success")
            return redirect(url_for("teacher.certificates_verification"))

    certificates = list(Certificate.objects().order_by("-issued_at").all())
    if allowed_subject_ids is not None:
        certificates = [
            c for c in certificates
            if _subject_id_for_lesson(c.lesson_id) in allowed_subject_ids
        ]
    student_ids = [c.student_id.id for c in certificates if c.student_id]
    lesson_ids = [c.lesson_id.id for c in certificates if c.lesson_id]
    verifier_ids = [c.verified_by.id for c in certificates if getattr(c, "verified_by", None)]

    students_map = {u.id: u for u in User.objects(id__in=student_ids).all()} if student_ids else {}
    lessons_map = {l.id: l for l in Lesson.objects(id__in=lesson_ids).all()} if lesson_ids else {}
    verifiers_map = {u.id: u for u in User.objects(id__in=verifier_ids).all()} if verifier_ids else {}

    return render_template(
        "teacher/certificates_verification.html",
        certificates=certificates,
        students_map=students_map,
        lessons_map=lessons_map,
        verifiers_map=verifiers_map,
    )


@teacher_bp.route("/assignments", methods=["GET", "POST"])
@login_required
@role_required("teacher")
def assignments_manage():
    allowed_subject_ids = _allowed_subject_ids_for_current_user()

    if request.method == "POST":
        action = (request.form.get("action") or "create").strip()
        if action == "create":
            title = (request.form.get("title") or "").strip()
            description = (request.form.get("description") or "").strip() or None
            due_at_raw = (request.form.get("due_at") or "").strip()
            target_student_id = (request.form.get("target_student_id") or "").strip()
            subject_id = (request.form.get("subject_id") or "").strip()
            section_id = (request.form.get("section_id") or "").strip()
            lesson_id = (request.form.get("lesson_id") or "").strip()

            if not title:
                flash("عنوان الواجب مطلوب.", "error")
                return redirect(url_for("teacher.assignments_manage"))

            due_at = None
            if due_at_raw:
                try:
                    due_at = datetime.fromisoformat(due_at_raw)
                except Exception:
                    flash("تنسيق تاريخ الاستحقاق غير صحيح.", "error")
                    return redirect(url_for("teacher.assignments_manage"))

            assignment = Assignment(
                title=title,
                description=description,
                created_by=current_user.id,
                due_at=due_at,
                is_active=True,
            )

            if target_student_id and ObjectId.is_valid(target_student_id):
                target_student = User.objects(id=target_student_id, role="student").first()
                if target_student:
                    assignment.target_student_id = target_student.id
            if subject_id and ObjectId.is_valid(subject_id):
                subject = Subject.objects(id=subject_id).first()
                if subject:
                    if not _subject_allowed_for_current_user(subject.id):
                        return _deny_permission()
                    assignment.subject_id = subject.id
            if section_id and ObjectId.is_valid(section_id):
                section = Section.objects(id=section_id).first()
                if section:
                    if not _subject_allowed_for_current_user(_subject_id_for_section(section)):
                        return _deny_permission()
                    assignment.section_id = section.id
            if lesson_id and ObjectId.is_valid(lesson_id):
                lesson = Lesson.objects(id=lesson_id).first()
                if lesson:
                    if not _subject_allowed_for_current_user(_subject_id_for_lesson(lesson)):
                        return _deny_permission()
                    assignment.lesson_id = lesson.id

            assignment.save()
            flash("تم إنشاء الواجب بنجاح.", "success")
            return redirect(url_for("teacher.assignments_manage"))

        if action == "create_custom_test":
            title = (request.form.get("title") or "").strip()
            description = (request.form.get("description") or "").strip() or None
            due_at_raw = (request.form.get("due_at") or "").strip()
            target_student_id = (request.form.get("target_student_id") or "").strip()
            carried_ids_csv = (request.form.get("selected_question_ids_csv") or "").strip()
            carried_ids = [qid.strip() for qid in carried_ids_csv.split(",") if ObjectId.is_valid(qid.strip())]
            question_ids = [qid for qid in request.form.getlist("question_ids") if ObjectId.is_valid(qid)]
            # Keep selected questions across paginated pages.
            question_ids = list(dict.fromkeys(carried_ids + question_ids))
            written_raw = (request.form.get("written_questions") or "").strip()

            if not title:
                flash("عنوان الواجب مطلوب.", "error")
                return redirect(url_for("teacher.assignments_manage"))
            if not question_ids and not written_raw:
                flash("اختر أسئلة MCQ أو أضف سؤالاً كتابياً واحداً على الأقل.", "error")
                return redirect(url_for("teacher.assignments_manage"))

            due_at = None
            if due_at_raw:
                try:
                    due_at = datetime.fromisoformat(due_at_raw)
                except Exception:
                    flash("تنسيق تاريخ الاستحقاق غير صحيح.", "error")
                    return redirect(url_for("teacher.assignments_manage"))

            written_items = []
            for line in written_raw.splitlines():
                text = line.strip()
                if not text:
                    continue
                max_score = 5
                prompt = text
                if "||" in text:
                    parts = text.split("||", 1)
                    prompt = parts[0].strip()
                    try:
                        max_score = max(1, int(parts[1].strip()))
                    except Exception:
                        max_score = 5
                written_items.append({"prompt": prompt, "max_score": max_score})

            assignment = Assignment(
                title=title,
                description=description,
                created_by=current_user.id,
                due_at=due_at,
                is_active=True,
                assignment_mode="custom_test",
                selected_question_ids_json=json.dumps(question_ids),
                written_questions_json=json.dumps(written_items),
                max_score=0,
            )
            if target_student_id and ObjectId.is_valid(target_student_id):
                target_student = User.objects(id=target_student_id, role="student").first()
                if target_student:
                    assignment.target_student_id = target_student.id

            total_score = 0
            if question_ids:
                total_score += len(question_ids)
                first_question = Question.objects(id=ObjectId(str(question_ids[0]))).first()
                if first_question and first_question.test_id:
                    sid = _subject_id_for_test(first_question.test_id)
                    if not _subject_allowed_for_current_user(sid):
                        return _deny_permission()
                    assignment.subject_id = sid
            total_score += sum(int(i.get("max_score", 0) or 0) for i in written_items)
            assignment.max_score = total_score
            assignment.save()

            flash("تم إنشاء واجب اختبار مخصص.", "success")
            return redirect(url_for("teacher.assignments_manage"))

        if action == "toggle_active":
            assignment_id = request.form.get("assignment_id")
            assignment = Assignment.objects(id=assignment_id).first()
            if assignment and not _subject_allowed_for_current_user(_subject_id_for_assignment(assignment)):
                return _deny_permission()
            if assignment:
                assignment.is_active = not assignment.is_active
                assignment.save()
                flash("تم تحديث حالة الواجب.", "success")
            return redirect(url_for("teacher.assignments_manage"))

        if action == "delete":
            assignment_id = request.form.get("assignment_id")
            assignment = Assignment.objects(id=assignment_id).first()
            if assignment and not _subject_allowed_for_current_user(_subject_id_for_assignment(assignment)):
                return _deny_permission()
            if assignment:
                AssignmentSubmission.objects(assignment_id=assignment.id).delete()
                AssignmentAttempt.objects(assignment_id=assignment.id).delete()
                assignment.delete()
                flash("تم حذف الواجب.", "success")
            return redirect(url_for("teacher.assignments_manage"))

    assignments = list(Assignment.objects().order_by("-created_at").all())
    if allowed_subject_ids is not None:
        assignments = [
            a for a in assignments
            if _subject_id_for_assignment(a) in allowed_subject_ids
        ]
    submissions = AssignmentSubmission.objects(assignment_id__in=[a.id for a in assignments]).all() if assignments else []
    attempts = AssignmentAttempt.objects(assignment_id__in=[a.id for a in assignments]).all() if assignments else []
    submissions_by_assignment = {}
    for sub in submissions:
        aid = sub.assignment_id.id if sub.assignment_id else None
        if not aid:
            continue
        submissions_by_assignment.setdefault(aid, []).append(sub)

    attempts_by_assignment = {}
    for attempt in attempts:
        aid = attempt.assignment_id.id if attempt.assignment_id else None
        if not aid:
            continue
        attempts_by_assignment.setdefault(aid, []).append(attempt)

    students = list(User.objects(role="student").order_by("username").all())
    subjects_q = Subject.objects()
    sections_q = Section.objects()
    lessons_q = Lesson.objects()
    if allowed_subject_ids is not None:
        subjects_q = subjects_q.filter(id__in=list(allowed_subject_ids))
        sections_q = sections_q.filter(subject_id__in=list(allowed_subject_ids))
        allowed_section_ids = [s.id for s in sections_q.only("id").all()]
        lessons_q = lessons_q.filter(section_id__in=allowed_section_ids)

    subjects = list(subjects_q.order_by("created_at").all())
    sections = list(sections_q.order_by("created_at").all())
    lessons = list(lessons_q.order_by("created_at").all())

    selected_custom_lesson_id = (request.args.get("custom_lesson_id") or "").strip()
    questions_page = max(1, int(request.args.get("questions_page", 1) or 1))
    questions_per_page = 25
    selected_question_ids = [
        qid.strip()
        for qid in (request.args.get("selected_question_ids") or "").split(",")
        if ObjectId.is_valid(qid.strip())
    ]

    selected_custom_lesson = None
    questions = []
    questions_total = 0
    questions_total_pages = 1

    if selected_custom_lesson_id and ObjectId.is_valid(selected_custom_lesson_id):
        selected_custom_lesson = Lesson.objects(id=selected_custom_lesson_id).first()
        if selected_custom_lesson and not _subject_allowed_for_current_user(_subject_id_for_lesson(selected_custom_lesson)):
            selected_custom_lesson = None
        if selected_custom_lesson:
            lesson_test_ids = [t.id for t in Test.objects(lesson_id=selected_custom_lesson.id).only("id").all()]
            if lesson_test_ids:
                q_query = Question.objects(test_id__in=lesson_test_ids).order_by("-created_at")
                questions_total = q_query.count()
                questions_total_pages = max(1, (questions_total + questions_per_page - 1) // questions_per_page)
                if questions_page > questions_total_pages:
                    questions_page = questions_total_pages
                questions = list(
                    q_query.skip((questions_page - 1) * questions_per_page).limit(questions_per_page).all()
                )
            else:
                questions_total = 0
                questions_total_pages = 1

    return render_template(
        "teacher/assignments_manage.html",
        assignments=assignments,
        submissions_by_assignment=submissions_by_assignment,
        attempts_by_assignment=attempts_by_assignment,
        students=students,
        subjects=subjects,
        sections=sections,
        lessons=lessons,
        questions=questions,
        selected_custom_lesson_id=selected_custom_lesson_id,
        selected_custom_lesson=selected_custom_lesson,
        questions_page=questions_page,
        questions_per_page=questions_per_page,
        questions_total=questions_total,
        questions_total_pages=questions_total_pages,
        selected_question_ids=selected_question_ids,
    )


@teacher_bp.route("/assignments/<assignment_id>/submissions", methods=["GET"])
@login_required
@role_required("teacher")
def assignment_submissions(assignment_id):
    assignment = Assignment.objects(id=assignment_id).first()
    if not assignment:
        raise NotFound()
    if not _subject_allowed_for_current_user(_subject_id_for_assignment(assignment)):
        return _deny_permission()

    attempts = list(AssignmentAttempt.objects(assignment_id=assignment.id).order_by("-submitted_at").all())
    return render_template("teacher/assignment_submissions.html", assignment=assignment, attempts=attempts)


@teacher_bp.route("/assignments/<assignment_id>/questions", methods=["GET"])
@login_required
@role_required("teacher")
def assignment_questions(assignment_id):
    assignment = Assignment.objects(id=assignment_id, assignment_mode="custom_test").first()
    if not assignment:
        raise NotFound()
    if not _subject_allowed_for_current_user(_subject_id_for_assignment(assignment)):
        return _deny_permission()

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

    return render_template(
        "teacher/assignment_questions.html",
        assignment=assignment,
        questions=ordered_questions,
        written_items=written_items,
    )


@teacher_bp.route("/assignment-attempts/<attempt_id>/grade", methods=["GET", "POST"])
@login_required
@role_required("teacher")
def grade_assignment_attempt(attempt_id):
    attempt = AssignmentAttempt.objects(id=attempt_id).first()
    if not attempt:
        raise NotFound()
    assignment = attempt.assignment_id
    if not assignment:
        raise NotFound()
    if not _subject_allowed_for_current_user(_subject_id_for_assignment(assignment)):
        return _deny_permission()

    question_ids = []
    if assignment.selected_question_ids_json:
        try:
            question_ids = [qid for qid in json.loads(assignment.selected_question_ids_json) if ObjectId.is_valid(qid)]
        except Exception:
            question_ids = []
    questions = Question.objects(id__in=question_ids).all() if question_ids else []
    questions_by_id = {str(q.id): q for q in questions}

    written_items = []
    if assignment.written_questions_json:
        try:
            payload = json.loads(assignment.written_questions_json)
            if isinstance(payload, list):
                written_items = payload
        except Exception:
            written_items = []

    answers = []
    try:
        raw_answers = json.loads(attempt.answers_json or "[]")
        if isinstance(raw_answers, list):
            answers = raw_answers
    except Exception:
        answers = []

    if request.method == "POST":
        scored_answers = []
        total_awarded = 0

        for idx, ans in enumerate(answers):
            score_val = request.form.get(f"score_{idx}")
            try:
                score = max(0, int(score_val or 0))
            except Exception:
                score = 0
            max_for_answer = max(0, int(ans.get("max_score", 1) or 1))
            if score > max_for_answer:
                score = max_for_answer
            ans["score_awarded"] = score
            total_awarded += score
            scored_answers.append(ans)

        attempt.answers_json = json.dumps(scored_answers, ensure_ascii=False)
        attempt.score_awarded = max(0, total_awarded)
        attempt.total_score = int(assignment.max_score or 0)
        attempt.teacher_note = (request.form.get("teacher_note") or "").strip() or None
        attempt.status = "graded"
        attempt.graded_by = current_user.id
        attempt.graded_at = datetime.utcnow()
        attempt.save()

        flash("تم تصحيح المحاولة وإضافة الدرجة.", "success")
        return redirect(url_for("teacher.assignment_submissions", assignment_id=assignment.id))

    prepared_answers = []
    for idx, ans in enumerate(answers):
        item_type = ans.get("type")
        row = {
            "index": idx,
            "type": item_type,
            "answer": ans,
            "question": None,
            "max_score": int(ans.get("max_score", 1) or 1),
        }
        if item_type == "mcq":
            qid = str(ans.get("question_id") or "")
            row["question"] = questions_by_id.get(qid)
            row["max_score"] = 1
        prepared_answers.append(row)

    return render_template(
        "teacher/assignment_grade.html",
        assignment=assignment,
        attempt=attempt,
        prepared_answers=prepared_answers,
        written_items=written_items,
    )


def _pdf_font_candidates():
    return [
        ("C:\\Windows\\Fonts\\tahoma.ttf", "C:\\Windows\\Fonts\\tahomabd.ttf"),
        ("C:\\Windows\\Fonts\\arial.ttf", "C:\\Windows\\Fonts\\arialbd.ttf"),
    ]


def _pdf_pick_font_paths():
    for regular, bold in _pdf_font_candidates():
        if os.path.exists(regular):
            return regular, bold if os.path.exists(bold) else regular
    return None, None


def _shape_arabic_text(text):
    value = str(text or "")
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display

        return get_display(arabic_reshaper.reshape(value))
    except Exception:
        return value


def _collect_report_data(filter_student=None):
    attempts_q = Attempt.objects(student_id=filter_student.id) if filter_student else Attempt.objects()
    custom_attempts_q = CustomTestAttempt.objects(student_id=filter_student.id, status="submitted") if filter_student else CustomTestAttempt.objects(status="submitted")
    lessons_done_q = LessonCompletion.objects(student_id=filter_student.id) if filter_student else LessonCompletion.objects()
    standard_assignments_done_q = AssignmentSubmission.objects(student_id=filter_student.id, status="completed") if filter_student else AssignmentSubmission.objects(status="completed")
    custom_assignments_done_q = AssignmentAttempt.objects(student_id=filter_student.id, status="graded") if filter_student else AssignmentAttempt.objects(status="graded")
    gamification_q = StudentGamification.objects(student_id=filter_student.id) if filter_student else StudentGamification.objects()

    attempts = list(attempts_q.only("score", "total", "test_id", "student_id", "submitted_at").all())
    avg_score = 0
    pass_rate = 0
    if attempts:
        vals = [((a.score / a.total) * 100) for a in attempts if a.total]
        if vals:
            avg_score = round(sum(vals) / len(vals), 2)
            pass_rate = round((len([x for x in vals if x >= 50]) / len(vals)) * 100, 2)

    assignments_total_q = Assignment.objects()
    if filter_student:
        assignments_total_q = assignments_total_q.filter(__raw__={
            "$or": [
                {"target_student_id": filter_student.id},
                {"target_student_id": None},
                {"target_student_id": {"$exists": False}},
            ]
        })

    assignments_total = assignments_total_q.count()
    assignments_completed = int(standard_assignments_done_q.count() + custom_assignments_done_q.count())
    assignment_completion_rate = round((assignments_completed / assignments_total) * 100, 2) if assignments_total else 0

    profiles = list(gamification_q.only("xp_total").all())
    avg_xp = round(sum(int(p.xp_total or 0) for p in profiles) / len(profiles), 2) if profiles else 0

    by_test = {}
    for a in attempts:
        if not a.test_id or not a.total:
            continue
        tid = str(a.test_id.id)
        pct = (a.score / a.total) * 100
        bucket = by_test.setdefault(
            tid,
            {
                "title": a.test_id.title if a.test_id else "-",
                "sum": 0.0,
                "count": 0,
                "pass": 0,
            },
        )
        bucket["sum"] += pct
        bucket["count"] += 1
        if pct >= 50:
            bucket["pass"] += 1

    tests_stats = []
    for _, v in by_test.items():
        avg = (v["sum"] / v["count"]) if v["count"] else 0
        tests_stats.append(
            {
                "title": v["title"],
                "avg": round(avg, 2),
                "count": v["count"],
                "pass_rate": round((v["pass"] / v["count"]) * 100, 2) if v["count"] else 0,
            }
        )
    tests_stats.sort(key=lambda x: x["avg"])

    top_students = []
    if not filter_student:
        top_profiles = list(StudentGamification.objects.order_by("-xp_total").limit(5).all())
        user_map = {u.id: u for u in User.objects(id__in=[p.student_id.id for p in top_profiles if p.student_id]).all()}
        for idx, profile in enumerate(top_profiles, start=1):
            if not profile.student_id:
                continue
            top_students.append(
                {
                    "rank": idx,
                    "name": (user_map.get(profile.student_id.id).full_name if user_map.get(profile.student_id.id) else "-") ,
                    "xp": int(profile.xp_total or 0),
                    "level": int(profile.level or 1),
                }
            )
    elif filter_student:
        profile = StudentGamification.objects(student_id=filter_student.id).first()
        top_students.append(
            {
                "rank": 1,
                "name": filter_student.full_name,
                "xp": int(profile.xp_total or 0) if profile else 0,
                "level": int(profile.level or 1) if profile else 1,
            }
        )

    return {
        "regular_attempts": attempts_q.count(),
        "custom_attempts": custom_attempts_q.count(),
        "lessons_completed": lessons_done_q.count(),
        "assignments_completed": assignments_completed,
        "avg_score": avg_score,
        "students_count": gamification_q.count(),
        "pass_rate": pass_rate,
        "avg_xp": avg_xp,
        "assignment_completion_rate": assignment_completion_rate,
        "subjects_count": Subject.objects.count(),
        "sections_count": Section.objects.count(),
        "lessons_count": Lesson.objects.count(),
        "tests_count": Test.objects.count(),
        "weak_tests": tests_stats[:5],
        "strong_tests": sorted(tests_stats, key=lambda x: x["avg"], reverse=True)[:5],
        "top_students": top_students,
        "generated_at": datetime.utcnow(),
    }


@teacher_bp.route("/reports", methods=["GET"])
@login_required
@role_required("admin")
def reports_dashboard():
    student_id = (request.args.get("student_id") or "").strip()
    students = list(User.objects(role="student").order_by("username").all())

    filter_student = None
    if student_id and ObjectId.is_valid(student_id):
        filter_student = User.objects(id=student_id, role="student").first()
    report = _collect_report_data(filter_student=filter_student)

    return render_template(
        "teacher/reports_dashboard.html",
        report=report,
        students=students,
        selected_student=filter_student,
    )


@teacher_bp.route("/reports/download", methods=["GET"])
@login_required
@role_required("admin")
def reports_download_pdf():
    if FPDF is None:
        flash("مكتبة PDF غير متوفرة. ثبت fpdf2 أولاً.", "error")
        return redirect(url_for("teacher.reports_dashboard"))

    student_id = (request.args.get("student_id") or "").strip()
    student = None
    if student_id and ObjectId.is_valid(student_id):
        student = User.objects(id=student_id, role="student").first()

    report = _collect_report_data(filter_student=student)

    pdf = FPDF()
    if hasattr(pdf, "set_auto_page_break"):
        pdf.set_auto_page_break(auto=True, margin=12)

    regular_font, bold_font = _pdf_pick_font_paths()
    using_ar_font = False
    if regular_font:
        try:
            pdf.add_font("Arabic", "", regular_font)
            pdf.add_font("Arabic", "B", bold_font or regular_font)
            using_ar_font = True
        except Exception:
            using_ar_font = False

    pdf.add_page()

    logo_path = os.path.join(os.path.dirname(__file__), "static", "edupath-logo.png")
    if os.path.exists(logo_path):
        try:
            pdf.image(logo_path, x=12, y=8, w=26)
        except Exception:
            pass

    title_font = "Arabic" if using_ar_font else "Helvetica"
    body_font = "Arabic" if using_ar_font else "Helvetica"
    title = _shape_arabic_text("تقرير منصة EduPath") if using_ar_font else "EduPath Report"
    scope_text = student.full_name if student else "كل الطلاب"

    if using_ar_font and hasattr(pdf, "set_text_shaping"):
        try:
            pdf.set_text_shaping(True)
        except Exception:
            pass

    pdf.set_font(title_font, "B", 17)
    pdf.cell(0, 10, title, ln=1, align="R" if using_ar_font else "L")
    pdf.set_font(body_font, "", 11)
    generated_at_text = _shape_arabic_text(f"تاريخ الإنشاء: {report['generated_at'].strftime('%Y-%m-%d %H:%M UTC')}") if using_ar_font else f"Generated At: {report['generated_at'].strftime('%Y-%m-%d %H:%M UTC')}"
    scope_line = _shape_arabic_text(f"نطاق التقرير: {scope_text}") if using_ar_font else f"Scope: {scope_text}"
    pdf.cell(0, 8, generated_at_text, ln=1, align="R" if using_ar_font else "L")
    pdf.cell(0, 8, scope_line, ln=1, align="R" if using_ar_font else "L")
    pdf.ln(3)

    metric_lines = [
        ("عدد محاولات الاختبارات", report["regular_attempts"]),
        ("عدد المحاولات المخصصة", report["custom_attempts"]),
        ("عدد الدروس المكتملة", report["lessons_completed"]),
        ("الواجبات المكتملة", report["assignments_completed"]),
        ("متوسط النتائج", f"{report['avg_score']}%"),
        ("نسبة النجاح", f"{report['pass_rate']}%"),
        ("متوسط XP", report["avg_xp"]),
        ("نسبة إنجاز الواجبات", f"{report['assignment_completion_rate']}%"),
        ("عدد المواد", report["subjects_count"]),
        ("عدد الأقسام", report["sections_count"]),
        ("عدد الدروس", report["lessons_count"]),
        ("عدد الاختبارات", report["tests_count"]),
    ]

    pdf.set_font(title_font, "B", 13)
    section_title = _shape_arabic_text("المؤشرات الرئيسية") if using_ar_font else "Key Metrics"
    pdf.cell(0, 8, section_title, ln=1, align="R" if using_ar_font else "L")
    pdf.set_font(body_font, "", 11)
    for key, value in metric_lines:
        line = _shape_arabic_text(f"{key}: {value}") if using_ar_font else f"{key}: {value}"
        pdf.cell(0, 7, line, ln=1, align="R" if using_ar_font else "L")

    if report.get("weak_tests"):
        pdf.ln(2)
        pdf.set_font(title_font, "B", 13)
        weak_title = _shape_arabic_text("أضعف الاختبارات") if using_ar_font else "Weakest Tests"
        pdf.cell(0, 8, weak_title, ln=1, align="R" if using_ar_font else "L")
        pdf.set_font(body_font, "", 10)
        for t in report["weak_tests"][:5]:
            line = _shape_arabic_text(f"- {t['title']} | متوسط: {t['avg']}% | محاولات: {t['count']}") if using_ar_font else f"- {t['title']} | Avg: {t['avg']}% | Attempts: {t['count']}"
            pdf.multi_cell(0, 6, line, align="R" if using_ar_font else "L")

    if report.get("top_students"):
        pdf.ln(1)
        pdf.set_font(title_font, "B", 13)
        top_title = _shape_arabic_text("أفضل الطلاب (XP)") if using_ar_font else "Top Students (XP)"
        pdf.cell(0, 8, top_title, ln=1, align="R" if using_ar_font else "L")
        pdf.set_font(body_font, "", 10)
        for st in report["top_students"][:5]:
            line = _shape_arabic_text(f"#{st['rank']} - {st['name']} | XP: {st['xp']} | Level: {st['level']}") if using_ar_font else f"#{st['rank']} - {st['name']} | XP: {st['xp']} | Level: {st['level']}"
            pdf.cell(0, 6, line, ln=1, align="R" if using_ar_font else "L")

    out = pdf.output(dest="S")
    if isinstance(out, bytearray):
        out = bytes(out)
    elif isinstance(out, str):
        out = out.encode("latin-1", errors="ignore")

    filename = f"report_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.pdf"
    headers = {"Content-Disposition": f"attachment; filename={filename}"}
    return Response(out, mimetype="application/pdf", headers=headers)


@teacher_bp.route("/analytics", methods=["GET"])
@login_required
@role_required("admin")
def analytics_dashboard():
    attempts = list(Attempt.objects().only("test_id", "student_id", "score", "total", "submitted_at").all())
    lessons_completed = LessonCompletion.objects.count()
    assignments_total = Assignment.objects.count()
    assignments_done = int(AssignmentSubmission.objects(status="completed").count() + AssignmentAttempt.objects(status="graded").count())
    students_count = User.objects(role="student").count()
    subjects_count = Subject.objects.count()
    sections_count = Section.objects.count()
    lessons_count = Lesson.objects.count()
    tests_count = Test.objects.count()
    questions_count = int(Question.objects.count() + TestInteractiveQuestion.objects.count())

    overall_avg = 0
    scored = [((a.score / a.total) * 100) for a in attempts if a.total]
    if scored:
        overall_avg = round(sum(scored) / len(scored), 2)
    pass_rate = round((len([s for s in scored if s >= 50]) / len(scored)) * 100, 2) if scored else 0

    by_test = {}
    for a in attempts:
        if not a.test_id or not a.total:
            continue
        tid = str(a.test_id.id)
        bucket = by_test.setdefault(tid, {"title": a.test_id.title if a.test_id else "-", "sum": 0.0, "count": 0})
        bucket["sum"] += (a.score / a.total) * 100
        bucket["count"] += 1

    weak_tests = []
    for _, v in by_test.items():
        avg = (v["sum"] / v["count"]) if v["count"] else 0
        weak_tests.append({"title": v["title"], "avg": round(avg, 2), "count": v["count"]})
    weak_tests.sort(key=lambda x: x["avg"])

    strong_tests = sorted(weak_tests, key=lambda x: x["avg"], reverse=True)

    diff_counts = {
        "easy": Question.objects(difficulty="easy").count(),
        "medium": Question.objects(difficulty="medium").count(),
        "hard": Question.objects(difficulty="hard").count(),
    }
    diff_total = max(1, sum(diff_counts.values()))
    difficulty_stats = {
        "easy": {"count": diff_counts["easy"], "pct": round((diff_counts["easy"] / diff_total) * 100, 2)},
        "medium": {"count": diff_counts["medium"], "pct": round((diff_counts["medium"] / diff_total) * 100, 2)},
        "hard": {"count": diff_counts["hard"], "pct": round((diff_counts["hard"] / diff_total) * 100, 2)},
    }

    xp_profiles = list(StudentGamification.objects.only("student_id", "xp_total", "level").order_by("-xp_total").limit(5).all())
    top_users = User.objects(id__in=[p.student_id.id for p in xp_profiles if p.student_id]).all() if xp_profiles else []
    user_map = {u.id: u for u in top_users}
    top_students = []
    for idx, p in enumerate(xp_profiles, start=1):
        if not p.student_id:
            continue
        user = user_map.get(p.student_id.id)
        top_students.append(
            {
                "rank": idx,
                "name": user.full_name if user else "-",
                "xp": int(p.xp_total or 0),
                "level": int(p.level or 1),
            }
        )

    trend_points = []
    for i in range(6, -1, -1):
        day_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=i)
        day_end = day_start + timedelta(days=1)
        trend_points.append(
            {
                "label": day_start.strftime("%m-%d"),
                "attempts": Attempt.objects(submitted_at__gte=day_start, submitted_at__lt=day_end).count(),
                "lessons": LessonCompletion.objects(completed_at__gte=day_start, completed_at__lt=day_end).count(),
            }
        )

    metrics = {
        "overall_avg": overall_avg,
        "pass_rate": pass_rate,
        "lessons_completed": lessons_completed,
        "assignments_total": assignments_total,
        "assignments_done": assignments_done,
        "assignment_completion_rate": round((assignments_done / assignments_total) * 100, 2) if assignments_total else 0,
        "active_study_plans": StudyPlan.objects(is_active=True).count(),
        "students_count": students_count,
        "subjects_count": subjects_count,
        "sections_count": sections_count,
        "lessons_count": lessons_count,
        "tests_count": tests_count,
        "questions_count": questions_count,
        "attempts_total": len(attempts),
        "avg_attempts_per_student": round((len(attempts) / students_count), 2) if students_count else 0,
    }
    return render_template(
        "teacher/analytics_dashboard.html",
        metrics=metrics,
        weak_tests=weak_tests[:10],
        strong_tests=strong_tests[:10],
        top_students=top_students,
        difficulty_stats=difficulty_stats,
        trend_points=trend_points,
    )


@teacher_bp.route("/study-plans", methods=["GET", "POST"])
@login_required
@role_required("admin")
def study_plans_manage():
    if request.method == "POST":
        action = (request.form.get("action") or "").strip()

        if action == "create_plan":
            student_id = request.form.get("student_id")
            title = (request.form.get("title") or "").strip()
            description = (request.form.get("description") or "").strip() or None
            week_start_raw = (request.form.get("week_start") or "").strip()
            week_end_raw = (request.form.get("week_end") or "").strip()

            if not title:
                flash("عنوان الخطة مطلوب.", "error")
                return redirect(url_for("teacher.study_plans_manage"))

            student = User.objects(id=student_id, role="student").first()
            if not student:
                flash("الطالب غير موجود.", "error")
                return redirect(url_for("teacher.study_plans_manage"))

            week_start = None
            week_end = None
            try:
                if week_start_raw:
                    week_start = datetime.fromisoformat(week_start_raw)
                if week_end_raw:
                    week_end = datetime.fromisoformat(week_end_raw)
            except Exception:
                flash("تنسيق تاريخ الخطة غير صحيح.", "error")
                return redirect(url_for("teacher.study_plans_manage"))

            plan = StudyPlan(
                student_id=student.id,
                title=title,
                description=description,
                week_start=week_start,
                week_end=week_end,
                created_by=current_user.id,
                is_active=True,
            )
            plan.save()
            flash("تم إنشاء الخطة الدراسية.", "success")
            return redirect(url_for("teacher.study_plans_manage"))

        if action == "add_item":
            plan_id = request.form.get("plan_id")
            title = (request.form.get("item_title") or "").strip()
            lesson_id = (request.form.get("lesson_id") or "").strip()
            test_id = (request.form.get("test_id") or "").strip()
            due_at_raw = (request.form.get("due_at") or "").strip()

            plan = StudyPlan.objects(id=plan_id).first()
            if not plan:
                flash("الخطة غير موجودة.", "error")
                return redirect(url_for("teacher.study_plans_manage"))
            if not title:
                flash("عنوان المهمة مطلوب.", "error")
                return redirect(url_for("teacher.study_plans_manage"))

            due_at = None
            if due_at_raw:
                try:
                    due_at = datetime.fromisoformat(due_at_raw)
                except Exception:
                    flash("تنسيق موعد المهمة غير صحيح.", "error")
                    return redirect(url_for("teacher.study_plans_manage"))

            item = StudyPlanItem(plan_id=plan.id, title=title, due_at=due_at)
            if lesson_id and ObjectId.is_valid(lesson_id):
                lesson = Lesson.objects(id=lesson_id).first()
                if lesson:
                    item.lesson_id = lesson.id
            if test_id and ObjectId.is_valid(test_id):
                test = Test.objects(id=test_id).first()
                if test:
                    item.test_id = test.id
            item.save()
            flash("تمت إضافة مهمة للخطة.", "success")
            return redirect(url_for("teacher.study_plans_manage"))

        if action == "toggle_plan":
            plan_id = request.form.get("plan_id")
            plan = StudyPlan.objects(id=plan_id).first()
            if plan:
                plan.is_active = not plan.is_active
                plan.save()
                flash("تم تحديث حالة الخطة.", "success")
            return redirect(url_for("teacher.study_plans_manage"))

    students = list(User.objects(role="student").order_by("username").all())
    lessons = list(Lesson.objects().order_by("created_at").all())
    tests = list(Test.objects().order_by("created_at").all())

    plans = list(StudyPlan.objects().order_by("-created_at").all())
    items = StudyPlanItem.objects(plan_id__in=[p.id for p in plans]).all() if plans else []
    items_by_plan = {}
    for item in items:
        pid = item.plan_id.id if item.plan_id else None
        if not pid:
            continue
        items_by_plan.setdefault(pid, []).append(item)

    return render_template(
        "teacher/study_plans_manage.html",
        students=students,
        lessons=lessons,
        tests=tests,
        plans=plans,
        items_by_plan=items_by_plan,
    )


def _rebuild_ranked_students():
    profiles = list(StudentGamification.objects.order_by("-xp_total", "student_id").all())
    if not profiles:
        return []
    student_ids = [p.student_id.id for p in profiles if p.student_id]
    users = User.objects(id__in=student_ids).all() if student_ids else []
    users_by_id = {u.id: u for u in users}

    ranked = []
    for idx, profile in enumerate(profiles, start=1):
        if not profile.student_id:
            continue
        ranked.append(
            {
                "rank": idx,
                "profile": profile,
                "student": users_by_id.get(profile.student_id.id),
            }
        )
    return ranked


def _apply_xp_delta(profile, delta, actor_label):
    delta = int(delta or 0)
    current_xp = int(profile.xp_total or 0)
    next_xp = max(0, current_xp + delta)
    applied_delta = next_xp - current_xp
    if applied_delta == 0:
        return 0

    profile.xp_total = next_xp
    profile.level = (next_xp // 200) + 1
    profile.updated_at = datetime.utcnow()
    profile.save()

    XPEvent(
        student_id=profile.student_id.id,
        event_type="admin_xp_adjust",
        source_id=f"{actor_label}:{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}",
        xp=applied_delta,
    ).save()
    return applied_delta


@teacher_bp.route("/gamification", methods=["GET", "POST"])
@login_required
@role_required("admin")
def gamification_admin():
    if request.method == "POST":
        action = (request.form.get("action") or "").strip()

        if action in {"set_student_xp", "adjust_student_xp", "set_student_rank"}:
            student_id = request.form.get("student_id")
            student = User.objects(id=student_id, role="student").first()
            if not student:
                flash("الطالب غير موجود.", "error")
                return redirect(url_for("teacher.gamification_admin"))

            profile = StudentGamification.objects(student_id=student.id).first()
            if not profile:
                profile = StudentGamification(student_id=student.id, xp_total=0, level=1)
                profile.save()

            if action == "set_student_xp":
                target_xp = max(0, int(request.form.get("target_xp") or 0))
                delta = target_xp - int(profile.xp_total or 0)
                applied = _apply_xp_delta(profile, delta, actor_label=f"set_xp:{current_user.id}")
                flash(f"تم تحديث XP للطالب {student.full_name} (Δ {applied}).", "success")

            elif action == "adjust_student_xp":
                delta = int(request.form.get("xp_delta") or 0)
                applied = _apply_xp_delta(profile, delta, actor_label=f"delta_xp:{current_user.id}")
                flash(f"تم تعديل XP للطالب {student.full_name} (Δ {applied}).", "success")

            elif action == "set_student_rank":
                requested_rank = max(1, int(request.form.get("target_rank") or 1))
                ranked = _rebuild_ranked_students()
                others = [r for r in ranked if str(r["profile"].student_id.id) != str(student.id)]
                if not others:
                    flash("لا يوجد طلاب كفاية لإعادة الترتيب.", "info")
                    return redirect(url_for("teacher.gamification_admin"))

                requested_rank = min(requested_rank, len(others) + 1)
                if requested_rank == 1:
                    desired_xp = int(others[0]["profile"].xp_total or 0) + 1
                elif requested_rank == len(others) + 1:
                    desired_xp = max(0, int(others[-1]["profile"].xp_total or 0) - 1)
                else:
                    upper = int(others[requested_rank - 2]["profile"].xp_total or 0)
                    lower = int(others[requested_rank - 1]["profile"].xp_total or 0)
                    desired_xp = upper if upper == lower else max(lower, upper - 1)

                delta = desired_xp - int(profile.xp_total or 0)
                applied = _apply_xp_delta(profile, delta, actor_label=f"set_rank:{current_user.id}")
                flash(
                    f"تم ضبط XP للطالب {student.full_name} لمحاولة الوصول للرتبة {requested_rank} (Δ {applied}).",
                    "success",
                )

            cache.clear()
            return redirect(url_for("teacher.gamification_admin"))

        if action == "set_lesson_xp":
            lesson_id = request.form.get("lesson_id")
            lesson = Lesson.objects(id=lesson_id).first()
            if not lesson:
                flash("الدرس غير موجود.", "error")
                return redirect(url_for("teacher.gamification_admin"))

            lesson_xp = max(0, int(request.form.get("lesson_xp") or 0))
            lesson.xp_reward = lesson_xp
            lesson.save()
            cache.clear()
            flash(f"تم تحديث XP للدرس {lesson.title} إلى {lesson_xp}.", "success")
            return redirect(url_for("teacher.gamification_admin"))

    ranked_students = _rebuild_ranked_students()
    lessons = list(Lesson.objects().order_by("created_at").all())
    return render_template(
        "teacher/gamification_manage.html",
        ranked_students=ranked_students,
        lessons=lessons,
    )


@teacher_bp.route("/students/<user_id>/edit", methods=["GET", "POST"])
@login_required
@role_required("admin")
def edit_student(user_id):
    student = User.objects(id=user_id).first()
    if not student:
        raise NotFound()
    form = StudentEditForm()
    if form.validate_on_submit():
        student.username = form.username.data
        student.phone = form.phone.data
        student.role = form.role.data
        student.save()
        flash("تم تحديث بيانات الطالب بنجاح.", "success")
        return redirect(url_for("teacher.students"))
    elif request.method == "GET":
        form.username.data = student.username
        form.phone.data = student.phone
        form.role.data = student.role
    return render_template("teacher/student_form.html", form=form, student=student)


@teacher_bp.route("/subjects/<subject_id>/access", methods=["GET", "POST"])
@login_required
@role_required("teacher")
def manage_subject_access(subject_id):
    subject = Subject.objects(id=subject_id).first()
    if not subject:
        raise NotFound()
    scope_response = _ensure_subject_scope(subject.id)
    if scope_response:
        return scope_response
    
    if request.method == "POST":
        action = request.form.get("action")
        if action == "toggle_requires_code":
            subject.requires_code = not subject.requires_code
            subject.save()
            if subject.requires_code:
                lock_subject_access_for_all(subject.id)
            flash("تم تحديث حالة التفعيل للمادة.", "success")
        elif action == "generate_code":
            student_id = request.form.get("student_id")
            student = User.objects(id=student_id).first()
            
            if student:
                existing_any = SubjectActivationCode.objects(
                    subject_id=subject.id, student_id=student.id
                ).first()
                if existing_any:
                    flash("لا يمكن إنشاء أكثر من كود لهذا الطالب.", "info")
                else:
                    # Generate unique 6-char code
                    code_value = _generate_unique_code(SubjectActivationCode)
                    
                    ac = SubjectActivationCode(
                        subject_id=subject.id, student_id=student.id, code=code_value
                    )
                    ac.save()
                    # Auto-activate if not already
                    existing = SubjectActivation.objects(
                        subject_id=subject.id, student_id=student.id, active=True
                    ).first()
                    if not existing:
                        sa = SubjectActivation(subject_id=subject.id, student_id=student.id)
                        sa.save()
                    flash("تم تفعيل المادة للطالب.", "success")
                    cascade_subject_activation(subject, student.id)
        
        elif action in {"revoke_access", "revoke"}:
            student_id = request.form.get("student_id")
            student = User.objects(id=student_id).first()
            if student:
                revoke_subject_activation(subject.id, student.id)
                flash("تم إلغاء التفعيل للطالب.", "success")

        elif action == "activate":
            student_id = request.form.get("student_id")
            student = User.objects(id=student_id).first()
            if student:
                existing = SubjectActivation.objects(
                    subject_id=subject.id, student_id=student.id, active=True
                ).first()
                if not existing:
                    sa = SubjectActivation(subject_id=subject.id, student_id=student.id)
                    sa.save()
                cascade_subject_activation(subject, student.id)
                flash("تم تفعيل المادة للطالب.", "success")
        
        elif action == "delete_code":
            code_id = request.form.get("code_id")
            ac = SubjectActivationCode.objects(id=code_id).first()
            if ac:
                if ac.subject_id and str(ac.subject_id.id) == str(subject.id):
                    ac.delete()
                    flash("تم حذف الكود بنجاح.", "success")
        return redirect(url_for("teacher.manage_subject_access", subject_id=subject.id))
    
    students = User.objects(role="student").order_by('-created_at').all()
    activated_students = {}
    for sa in SubjectActivation.objects(subject_id=subject.id, active=True).all():
        try:
            key = sa.student_id.id
        except (DoesNotExist, AttributeError):
            continue
        if key is not None:
            activated_students[key] = sa

    codes = SubjectActivationCode.objects(subject_id=subject.id).order_by('-created_at').all()
    codes_by_student = {}
    for code in codes:
        try:
            key = code.student_id.id if code.student_id else None
        except (DoesNotExist, AttributeError):
            key = None
        if key is not None:
            codes_by_student.setdefault(key, []).append(code)
    
    return render_template(
        "teacher/subject_access.html",
        subject=subject,
        students=students,
        activations=activated_students,
        codes=codes,
        codes_by_student=codes_by_student,
    )


@teacher_bp.route("/sections/<section_id>/access", methods=["GET", "POST"])
@login_required
@role_required("teacher")
def manage_section_access(section_id):
    section = Section.objects(id=section_id).first()
    if not section:
        raise NotFound()
    scope_response = _ensure_scope_for_section(section)
    if scope_response:
        return scope_response
    
    if request.method == "POST":
        action = request.form.get("action")
        if action == "toggle_requires_code":
            section.requires_code = not section.requires_code
            section.save()
            if section.requires_code:
                lock_section_access_for_all(section.id)
            flash("تم تحديث حالة التفعيل للقسم.", "success")
        elif action == "generate_code":
            student_id = request.form.get("student_id")
            student = User.objects(id=student_id).first()
            
            if student:
                existing_any = ActivationCode.objects(
                    section_id=section.id, student_id=student.id
                ).first()
                if existing_any:
                    flash("لا يمكن إنشاء أكثر من كود لهذا الطالب.", "info")
                else:
                    code_value = _generate_unique_code(ActivationCode)
                    
                    ac = ActivationCode(
                        section_id=section.id, student_id=student.id, code=code_value
                    )
                    ac.save()
                    existing = SectionActivation.objects(
                        section_id=section.id, student_id=student.id, active=True
                    ).first()
                    if not existing:
                        sa = SectionActivation(section_id=section.id, student_id=student.id)
                        sa.save()
                    flash("تم تفعيل القسم للطالب.", "success")
                    cascade_section_activation(section, student.id)
        
        elif action in {"revoke_access", "revoke"}:
            student_id = request.form.get("student_id")
            student = User.objects(id=student_id).first()
            if student:
                revoke_section_activation(section.id, student.id)
                flash("تم إلغاء التفعيل للطالب.", "success")

        elif action == "activate":
            student_id = request.form.get("student_id")
            student = User.objects(id=student_id).first()
            if student:
                existing = SectionActivation.objects(
                    section_id=section.id, student_id=student.id, active=True
                ).first()
                if not existing:
                    sa = SectionActivation(section_id=section.id, student_id=student.id)
                    sa.save()
                cascade_section_activation(section, student.id)
                flash("تم تفعيل القسم للطالب.", "success")
        
        elif action == "delete_code":
            code_id = request.form.get("code_id")
            ac = ActivationCode.objects(id=code_id).first()
            if ac:
                if ac.section_id and str(ac.section_id.id) == str(section_id):
                    ac.delete()
                    flash("تم حذف الكود بنجاح.", "success")
        return redirect(url_for("teacher.manage_section_access", section_id=section.id))
    
    students = User.objects(role="student").order_by('-created_at').all()
    activations = {}
    for sa in SectionActivation.objects(section_id=section.id, active=True).all():
        try:
            key = sa.student_id.id
        except (DoesNotExist, AttributeError):
            continue
        if key is not None:
            activations[key] = sa

    codes = ActivationCode.objects(section_id=section.id).order_by('-created_at').all()
    codes_by_student = {}
    for code in codes:
        try:
            key = code.student_id.id if code.student_id else None
        except (DoesNotExist, AttributeError):
            key = None
        if key is not None:
            codes_by_student.setdefault(key, []).append(code)
    
    return render_template(
        "teacher/section_access.html",
        section=section,
        students=students,
        activations=activations,
        codes=codes,
        codes_by_student=codes_by_student,
    )


@teacher_bp.route("/lessons/<lesson_id>/access", methods=["GET", "POST"])
@login_required
@role_required("teacher")
def manage_lesson_access(lesson_id):
    lesson = Lesson.objects(id=lesson_id).first()
    if not lesson:
        raise NotFound()
    scope_response = _ensure_scope_for_lesson(lesson)
    if scope_response:
        return scope_response
    section = lesson.section_id
    
    if request.method == "POST":
        action = request.form.get("action")
        if action == "generate_code":
            student_id = request.form.get("student_id")
            student = User.objects(id=student_id).first()
            
            if student:
                existing_any = LessonActivationCode.objects(
                    lesson_id=lesson.id, student_id=student.id
                ).first()
                if existing_any:
                    flash("لا يمكن إنشاء أكثر من كود لهذا الطالب.", "info")
                else:
                    code_value = _generate_unique_code(LessonActivationCode)
                    
                    lac = LessonActivationCode(
                        lesson_id=lesson.id, student_id=student.id, code=code_value
                    )
                    lac.save()
                    existing = LessonActivation.objects(
                        lesson_id=lesson.id, student_id=student.id, active=True
                    ).first()
                    if not existing:
                        la = LessonActivation(lesson_id=lesson.id, student_id=student.id)
                        la.save()
                    flash("تم تفعيل الدرس للطالب.", "success")
                    cascade_lesson_activation(lesson, student.id)
        
        elif action in {"revoke_access", "revoke"}:
            student_id = request.form.get("student_id")
            student = User.objects(id=student_id).first()
            if student:
                revoke_lesson_activation(lesson.id, student.id)
                flash("تم إلغاء التفعيل للطالب.", "success")

        elif action == "activate":
            student_id = request.form.get("student_id")
            student = User.objects(id=student_id).first()
            if student:
                existing = LessonActivation.objects(
                    lesson_id=lesson.id, student_id=student.id, active=True
                ).first()
                if not existing:
                    la = LessonActivation(lesson_id=lesson.id, student_id=student.id)
                    la.save()
                cascade_lesson_activation(lesson, student.id)
                flash("تم تفعيل الدرس للطالب.", "success")
        
        elif action == "delete_code":
            code_id = request.form.get("code_id")
            lac = LessonActivationCode.objects(id=code_id).first()
            if lac:
                if lac.lesson_id and str(lac.lesson_id.id) == str(lesson_id):
                    lac.delete()
                    flash("تم حذف الكود بنجاح.", "success")
        return redirect(url_for("teacher.manage_lesson_access", lesson_id=lesson.id))
    
    students = User.objects(role="student").order_by('-created_at').all()
    activations = {}
    for la in LessonActivation.objects(lesson_id=lesson.id, active=True).all():
        try:
            key = la.student_id.id
        except (DoesNotExist, AttributeError):
            continue
        if key is not None:
            activations[key] = la

    codes = LessonActivationCode.objects(lesson_id=lesson.id).order_by('-created_at').all()
    codes_by_student = {}
    for code in codes:
        try:
            key = code.student_id.id if code.student_id else None
        except (DoesNotExist, AttributeError):
            key = None
        if key is not None:
            codes_by_student.setdefault(key, []).append(code)
    
    return render_template(
        "teacher/lesson_access.html",
        lesson=lesson,
        section=section,
        students=students,
        activations=activations,
        codes=codes,
        codes_by_student=codes_by_student,
    )


# Subject CRUD

@teacher_bp.route("/subjects/new", methods=["GET", "POST"])
@login_required
@role_required("admin")
def new_subject():
    form = SubjectForm()
    if form.validate_on_submit():
        subject = Subject(
            name=form.name.data,
            description=form.description.data,
            requires_code=form.requires_code.data,
            created_by=current_user.id,
        )
        subject.save()
        flash("تم إنشاء المادة بنجاح.", "success")
        return redirect(url_for("teacher.subject_detail", subject_id=subject.id))
    return render_template("teacher/subject_form.html", form=form)


@teacher_bp.route("/subjects/<subject_id>")
@login_required
@role_required("teacher")
def subject_detail(subject_id):
    if isinstance(subject_id, Subject):
        subject_id = subject_id.id
    subject = Subject.objects(id=subject_id).first()
    if not subject:
        raise NotFound()
    scope_response = _ensure_subject_scope(subject.id)
    if scope_response:
        return scope_response
    sections = Section.objects(subject_id=subject.id).all()
    return render_template("teacher/subject_detail.html", subject=subject, sections=sections)


@teacher_bp.route("/subjects/<subject_id>/courses", methods=["GET"])
@login_required
@role_required("teacher", "question_editor")
def courses_manage(subject_id):
    subject = Subject.objects(id=subject_id).first()
    if not subject:
        raise NotFound()

    scope_response = _ensure_subject_scope(subject.id)
    if scope_response:
        return scope_response

    sets = list(CourseSet.objects(subject_id=subject.id).order_by("created_at").all())
    set_ids = [s.id for s in sets]
    question_counts = {}
    if set_ids:
        for row in CourseQuestion.objects(course_set_id__in=set_ids).only("course_set_id").all():
            sid = row.course_set_id.id if row.course_set_id else None
            if sid:
                question_counts[sid] = question_counts.get(sid, 0) + 1

    sections = list(Section.objects(subject_id=subject.id).order_by("created_at").all())
    section_ids = [s.id for s in sections]
    lessons = list(Lesson.objects(section_id__in=section_ids).order_by("created_at").all()) if section_ids else []

    return render_template(
        "teacher/course_sets_manage.html",
        subject=subject,
        sets=sets,
        question_counts=question_counts,
        sections=sections,
        lessons=lessons,
    )


@teacher_bp.route("/subjects/<subject_id>/courses/new", methods=["POST"])
@login_required
@role_required("teacher", "question_editor")
def course_set_new(subject_id):
    subject = Subject.objects(id=subject_id).first()
    if not subject:
        raise NotFound()

    scope_response = _ensure_subject_scope(subject.id)
    if scope_response:
        return scope_response

    title = (request.form.get("title") or "").strip()
    description = (request.form.get("description") or "").strip() or None
    xp_per_question_raw = (request.form.get("xp_per_question") or "").strip()
    section_id = (request.form.get("section_id") or "").strip()
    lesson_id = (request.form.get("lesson_id") or "").strip()
    is_active = (request.form.get("is_active") or "").strip().lower() == "on"

    if not title:
        flash("عنوان الدورة مطلوب.", "error")
        return redirect(url_for("teacher.courses_manage", subject_id=subject.id))

    try:
        xp_per_question = max(1, int(xp_per_question_raw or "1"))
    except Exception:
        flash("قيمة XP لكل سؤال غير صالحة.", "error")
        return redirect(url_for("teacher.courses_manage", subject_id=subject.id))

    section = None
    if section_id and ObjectId.is_valid(section_id):
        section = Section.objects(id=section_id, subject_id=subject.id).first()
    if section_id and not section:
        flash("القسم المحدد غير صالح.", "error")
        return redirect(url_for("teacher.courses_manage", subject_id=subject.id))

    lesson = None
    if lesson_id and ObjectId.is_valid(lesson_id):
        if section:
            lesson = Lesson.objects(id=lesson_id, section_id=section.id).first()
        else:
            lesson = Lesson.objects(id=lesson_id).first()
            if lesson and lesson.section_id and lesson.section_id.subject_id != subject:
                lesson = None
    if lesson_id and not lesson:
        flash("الدرس المحدد غير صالح.", "error")
        return redirect(url_for("teacher.courses_manage", subject_id=subject.id))

    if lesson and not section:
        section = lesson.section_id

    row = CourseSet(
        subject_id=subject.id,
        section_id=section.id if section else None,
        lesson_id=lesson.id if lesson else None,
        title=title,
        description=description,
        xp_per_question=xp_per_question,
        created_by=current_user.id,
        is_active=is_active,
    )
    row.save()

    flash("تم إنشاء الدورة. أضف الأسئلة الآن.", "success")
    return redirect(url_for("teacher.course_set_edit", course_set_id=row.id))


@teacher_bp.route("/courses/<course_set_id>/edit", methods=["GET", "POST"])
@login_required
@role_required("teacher", "question_editor")
def course_set_edit(course_set_id):
    course_set = CourseSet.objects(id=course_set_id).first()
    if not course_set:
        raise NotFound()

    scope_response = _ensure_subject_scope(_subject_id_for_course_set(course_set))
    if scope_response:
        return scope_response

    subject = course_set.subject_id
    sections = list(Section.objects(subject_id=subject.id).order_by("created_at").all())
    section_ids = [s.id for s in sections]
    lessons = list(Lesson.objects(section_id__in=section_ids).order_by("created_at").all()) if section_ids else []

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()

        if action == "update_set":
            title = (request.form.get("title") or "").strip()
            description = (request.form.get("description") or "").strip() or None
            xp_per_question_raw = (request.form.get("xp_per_question") or "").strip()
            section_id = (request.form.get("section_id") or "").strip()
            lesson_id = (request.form.get("lesson_id") or "").strip()
            is_active = (request.form.get("is_active") or "").strip().lower() == "on"

            if not title:
                flash("عنوان الدورة مطلوب.", "error")
                return redirect(url_for("teacher.course_set_edit", course_set_id=course_set.id))

            try:
                xp_per_question = max(1, int(xp_per_question_raw or "1"))
            except Exception:
                flash("قيمة XP لكل سؤال غير صالحة.", "error")
                return redirect(url_for("teacher.course_set_edit", course_set_id=course_set.id))

            section = None
            if section_id and ObjectId.is_valid(section_id):
                section = Section.objects(id=section_id, subject_id=subject.id).first()
            if section_id and not section:
                flash("القسم المحدد غير صالح.", "error")
                return redirect(url_for("teacher.course_set_edit", course_set_id=course_set.id))

            lesson = None
            if lesson_id and ObjectId.is_valid(lesson_id):
                if section:
                    lesson = Lesson.objects(id=lesson_id, section_id=section.id).first()
                else:
                    lesson = Lesson.objects(id=lesson_id).first()
                    if lesson and lesson.section_id and lesson.section_id.subject_id != subject:
                        lesson = None
            if lesson_id and not lesson:
                flash("الدرس المحدد غير صالح.", "error")
                return redirect(url_for("teacher.course_set_edit", course_set_id=course_set.id))

            if lesson and not section:
                section = lesson.section_id

            course_set.title = title
            course_set.description = description
            course_set.xp_per_question = xp_per_question
            course_set.section_id = section.id if section else None
            course_set.lesson_id = lesson.id if lesson else None
            course_set.is_active = is_active
            course_set.save()
            flash("تم تحديث بيانات الدورة.", "success")
            return redirect(url_for("teacher.course_set_edit", course_set_id=course_set.id))

        if action == "add_question":
            q_text = (request.form.get("question_text") or "").strip() or None
            q_img = (request.form.get("question_image_url") or "").strip()
            a_text = (request.form.get("answer_text") or "").strip() or None
            a_img = (request.form.get("answer_image_url") or "").strip()

            if not q_text and not q_img:
                flash("أدخل نص السؤال أو رابط صورة السؤال على الأقل.", "error")
                return redirect(url_for("teacher.course_set_edit", course_set_id=course_set.id))
            if not a_text and not a_img:
                flash("أدخل نص الإجابة أو رابط صورة الإجابة على الأقل.", "error")
                return redirect(url_for("teacher.course_set_edit", course_set_id=course_set.id))

            CourseQuestion(
                course_set_id=course_set.id,
                question_text=q_text,
                question_image_url=q_img or None,
                answer_text=a_text,
                answer_image_url=a_img or None,
                correct_value=True,
            ).save()
            flash("تمت إضافة سؤال جديد.", "success")
            return redirect(url_for("teacher.course_set_edit", course_set_id=course_set.id))

        if action == "update_question":
            qid = (request.form.get("question_id") or "").strip()
            q = CourseQuestion.objects(id=qid, course_set_id=course_set.id).first() if ObjectId.is_valid(qid) else None
            if not q:
                flash("السؤال غير موجود.", "error")
                return redirect(url_for("teacher.course_set_edit", course_set_id=course_set.id))

            q_text = (request.form.get("question_text") or "").strip() or None
            q_img = (request.form.get("question_image_url") or "").strip()
            a_text = (request.form.get("answer_text") or "").strip() or None
            a_img = (request.form.get("answer_image_url") or "").strip()

            if not q_text and not q_img:
                flash("أدخل نص السؤال أو رابط صورة السؤال على الأقل.", "error")
                return redirect(url_for("teacher.course_set_edit", course_set_id=course_set.id))
            if not a_text and not a_img:
                flash("أدخل نص الإجابة أو رابط صورة الإجابة على الأقل.", "error")
                return redirect(url_for("teacher.course_set_edit", course_set_id=course_set.id))

            q.question_text = q_text
            q.question_image_url = q_img
            q.answer_text = a_text
            q.answer_image_url = a_img
            q.correct_value = True
            q.save()
            flash("تم تحديث السؤال.", "success")
            return redirect(url_for("teacher.course_set_edit", course_set_id=course_set.id))

        if action == "delete_question":
            qid = (request.form.get("question_id") or "").strip()
            q = CourseQuestion.objects(id=qid, course_set_id=course_set.id).first() if ObjectId.is_valid(qid) else None
            if not q:
                flash("السؤال غير موجود.", "error")
                return redirect(url_for("teacher.course_set_edit", course_set_id=course_set.id))
            q.delete()
            flash("تم حذف السؤال.", "success")
            return redirect(url_for("teacher.course_set_edit", course_set_id=course_set.id))

    questions = list(CourseQuestion.objects(course_set_id=course_set.id).order_by("created_at").all())

    return render_template(
        "teacher/course_set_edit.html",
        subject=subject,
        course_set=course_set,
        questions=questions,
        sections=sections,
        lessons=lessons,
    )


@teacher_bp.route("/courses/<course_set_id>/delete", methods=["POST"])
@login_required
@role_required("teacher", "question_editor")
def course_set_delete(course_set_id):
    course_set = CourseSet.objects(id=course_set_id).first()
    if not course_set:
        raise NotFound()

    scope_response = _ensure_subject_scope(_subject_id_for_course_set(course_set))
    if scope_response:
        return scope_response

    subject_id = course_set.subject_id.id if course_set.subject_id else None
    CourseQuestion.objects(course_set_id=course_set.id).delete()
    course_set.delete()
    flash("تم حذف الدورة.", "success")
    return redirect(url_for("teacher.courses_manage", subject_id=subject_id))


@teacher_bp.route("/subjects/<subject_id>/edit", methods=["GET", "POST"])
@login_required
@role_required("teacher")
def edit_subject(subject_id):
    subject = Subject.objects(id=subject_id).first()
    if not subject:
        raise NotFound()
    scope_response = _ensure_subject_scope(subject.id)
    if scope_response:
        return scope_response
    form = SubjectForm()
    if form.validate_on_submit():
        subject.name = form.name.data
        subject.description = form.description.data
        subject.requires_code = form.requires_code.data
        subject.save()
        flash("تم تحديث المادة بنجاح.", "success")
        return redirect(url_for("teacher.subject_detail", subject_id=subject.id))
    elif request.method == "GET":
        form.name.data = subject.name
        form.description.data = subject.description
        form.requires_code.data = subject.requires_code
    return render_template("teacher/subject_form.html", form=form, subject=subject)


@teacher_bp.route("/subjects/<subject_id>/delete", methods=["POST"])
@login_required
@role_required("teacher")
def delete_subject(subject_id):
    subject = Subject.objects(id=subject_id).first()
    if not subject:
        raise NotFound()
    scope_response = _ensure_subject_scope(subject.id)
    if scope_response:
        return scope_response
    subject.delete()
    flash("تم حذف المادة بنجاح.", "success")
    return redirect(url_for("teacher.dashboard"))


# Section CRUD

@teacher_bp.route("/subjects/<subject_id>/sections/new", methods=["GET", "POST"])
@login_required
@role_required("teacher")
def new_section(subject_id):
    subject = Subject.objects(id=subject_id).first()
    if not subject:
        raise NotFound()
    scope_response = _ensure_subject_scope(subject.id)
    if scope_response:
        return scope_response
    form = SectionForm()
    if form.validate_on_submit():
        section = Section(
            subject_id=subject.id,
            title=form.title.data,
            description=form.description.data,
            requires_code=form.requires_code.data,
        )
        section.save()
        flash("تم إنشاء القسم بنجاح.", "success")
        return redirect(url_for("teacher.section_detail", section_id=section.id))
    return render_template("teacher/section_form.html", form=form, subject=subject)


@teacher_bp.route("/sections/<section_id>", methods=["GET", "POST"])
@login_required
@role_required("teacher")
def section_detail(section_id):
    section = Section.objects(id=section_id).first()
    if not section:
        raise NotFound()
    scope_response = _ensure_scope_for_section(section)
    if scope_response:
        return scope_response

    form = SectionForm()
    if request.method == "POST":
        form_name = (request.form.get("form_name") or "").strip()
        if form_name == "update_section" and form.validate_on_submit():
            section.title = form.title.data
            section.description = form.description.data
            section.requires_code = form.requires_code.data
            section.save()
            flash("تم تحديث القسم بنجاح.", "success")
            return redirect(url_for("teacher.section_detail", section_id=section.id))
        elif form_name == "batch_add_lessons":
            raw_titles = (request.form.get("lesson_titles") or "").strip()
            requires_code = (request.form.get("lessons_requires_code") or "").strip().lower() in {"1", "true", "yes", "on"}

            titles = []
            seen = set()
            for line in raw_titles.splitlines():
                title = line.strip()
                if not title:
                    continue
                if title in seen:
                    continue
                seen.add(title)
                titles.append(title)

            if not titles:
                flash("أدخل اسم درس واحد على الأقل (اسم في كل سطر).", "error")
                return redirect(url_for("teacher.section_detail", section_id=section.id))

            created = 0
            for title in titles:
                Lesson(
                    section_id=section.id,
                    title=title,
                    content="",
                    requires_code=requires_code,
                ).save()
                created += 1

            flash(f"تم إنشاء {created} درس بنجاح.", "success")
            return redirect(url_for("teacher.section_detail", section_id=section.id))

    if request.method == "GET":
        form.title.data = section.title
        form.description.data = section.description
        form.requires_code.data = section.requires_code

    lessons = Lesson.objects(section_id=section.id).all()
    tests = Test.objects(section_id=section.id, lesson_id=None).all()  # section-wide tests
    return render_template("teacher/section_detail.html", section=section, lessons=lessons, tests=tests, form=form)


@teacher_bp.route("/sections/<section_id>/edit", methods=["GET", "POST"])
@login_required
@role_required("teacher")
def edit_section(section_id):
    return redirect(url_for("teacher.section_detail", section_id=section_id))


@teacher_bp.route("/lessons/batch-new", methods=["GET", "POST"])
@login_required
@role_required("teacher")
def batch_new_lessons():
    allowed_subject_ids = _allowed_subject_ids_for_current_user()

    subjects_q = Subject.objects()
    if allowed_subject_ids is not None:
        subjects_q = subjects_q.filter(id__in=list(allowed_subject_ids))
    subjects = list(subjects_q.order_by("created_at").all())

    sections_q = Section.objects()
    if allowed_subject_ids is not None:
        sections_q = sections_q.filter(subject_id__in=list(allowed_subject_ids))
    sections = list(sections_q.order_by("created_at").all())

    if request.method == "POST":
        subject_ids = request.form.getlist("subject_id[]")
        section_ids = request.form.getlist("section_id[]")
        titles = request.form.getlist("title[]")
        requires_code = (request.form.get("requires_code") or "").strip().lower() in {"1", "true", "yes", "on"}

        rows_count = max(len(subject_ids), len(section_ids), len(titles))
        created = 0
        created_section_ids = set()
        invalid_rows = 0

        for idx in range(rows_count):
            sid = (subject_ids[idx] if idx < len(subject_ids) else "").strip()
            sec_id = (section_ids[idx] if idx < len(section_ids) else "").strip()
            title = (titles[idx] if idx < len(titles) else "").strip()

            if not sid and not sec_id and not title:
                continue
            if not sid or not sec_id or not title:
                invalid_rows += 1
                continue
            if not ObjectId.is_valid(sid) or not ObjectId.is_valid(sec_id):
                invalid_rows += 1
                continue

            subject = Subject.objects(id=sid).first()
            section = Section.objects(id=sec_id).first()
            if not subject or not section:
                invalid_rows += 1
                continue
            if not section.subject_id or str(section.subject_id.id) != str(subject.id):
                invalid_rows += 1
                continue
            if not _subject_allowed_for_current_user(subject.id):
                invalid_rows += 1
                continue

            Lesson(
                section_id=section.id,
                title=title,
                content="",
                requires_code=requires_code,
            ).save()
            created += 1
            created_section_ids.add(str(section.id))

        if created == 0:
            flash("لم يتم إنشاء أي درس. تحقق من البيانات.", "error")
            return render_template("teacher/lessons_batch_form.html", subjects=subjects, sections=sections)

        if invalid_rows:
            flash(f"تم إنشاء {created} درس، مع تجاهل {invalid_rows} صف غير صالح.", "warning")
        else:
            flash(f"تم إنشاء {created} درس بنجاح.", "success")

        if len(created_section_ids) == 1:
            return redirect(url_for("teacher.section_detail", section_id=list(created_section_ids)[0]))
        return redirect(url_for("teacher.dashboard"))

    return render_template("teacher/lessons_batch_form.html", subjects=subjects, sections=sections)


@teacher_bp.route("/sections/<section_id>/delete", methods=["POST"])
@login_required
@role_required("teacher")
def delete_section(section_id):
    section = Section.objects(id=section_id).first()
    if not section:
        raise NotFound()
    scope_response = _ensure_scope_for_section(section)
    if scope_response:
        return scope_response
    subject_id = section.subject_id
    section.delete()
    flash("تم حذف القسم بنجاح.", "success")
    return redirect(url_for("teacher.subject_detail", subject_id=subject_id))


# Lesson CRUD

@teacher_bp.route("/sections/<section_id>/lessons/new", methods=["GET", "POST"])
@login_required
@role_required("teacher")
def new_lesson(section_id):
    section = Section.objects(id=section_id).first()
    if not section:
        raise NotFound()
    scope_response = _ensure_scope_for_section(section)
    if scope_response:
        return scope_response
    form = LessonForm()
    if form.validate_on_submit():
        resource_labels = request.form.getlist("resource_label[]")
        resource_urls = request.form.getlist("resource_url[]")
        resource_types = request.form.getlist("resource_type[]")
        resources = [
            (lbl.strip(), url.strip(), (rtype or "").strip().lower() or None)
            for lbl, url, rtype in zip(resource_labels, resource_urls, resource_types)
            if lbl.strip() and url.strip()
        ]

        requires_code = bool(form.requires_code.data)

        lesson = Lesson(
            section_id=section.id,
            title=form.title.data,
            content=form.content.data,
            requires_code=requires_code,
            link_label=form.link_label.data,
            link_url=form.link_url.data,
            link_label_2=form.link_label_2.data,
            link_url_2=form.link_url_2.data,
        )
        lesson.save()

        if resources:
            for idx, (lbl, url, rtype) in enumerate(resources):
                res = LessonResource(
                    lesson_id=lesson.id,
                    label=lbl,
                    url=url,
                    resource_type=rtype,
                    position=idx,
                )
                res.save()
            flash("تم إنشاء الدرس بنجاح.", "success")
            return redirect(url_for("teacher.section_detail", section_id=section.id))
    return render_template("teacher/lesson_form.html", form=form, section=section)


@teacher_bp.route("/lessons/<lesson_id>")
@login_required
@role_required("teacher")
def lesson_detail(lesson_id):
    lesson = Lesson.objects(id=lesson_id).first()
    if not lesson:
        raise NotFound()
    scope_response = _ensure_scope_for_lesson(lesson)
    if scope_response:
        return scope_response
    return redirect(url_for("teacher.edit_lesson", lesson_id=lesson.id))


@teacher_bp.route("/lessons/<lesson_id>/edit", methods=["GET", "POST"])
@login_required
@role_required("teacher")
def edit_lesson(lesson_id):
    lesson = Lesson.objects(id=lesson_id).first()
    if not lesson:
        raise NotFound()
    scope_response = _ensure_scope_for_lesson(lesson)
    if scope_response:
        return scope_response
    form = LessonForm()
    if form.validate_on_submit():
        resource_labels = request.form.getlist("resource_label[]")
        resource_urls = request.form.getlist("resource_url[]")
        resource_types = request.form.getlist("resource_type[]")
        resources = [
            (lbl.strip(), url.strip(), (rtype or "").strip().lower() or None)
            for lbl, url, rtype in zip(resource_labels, resource_urls, resource_types)
            if lbl.strip() and url.strip()
        ]

        lesson.title = form.title.data
        lesson.content = form.content.data
        lesson.requires_code = form.requires_code.data
        lesson.link_label = form.link_label.data
        lesson.link_url = form.link_url.data
        lesson.link_label_2 = form.link_label_2.data
        lesson.link_url_2 = form.link_url_2.data
        lesson.save()

        # Replace resources from form
        LessonResource.objects(lesson_id=lesson.id).delete()
        for idx, (lbl, url, rtype) in enumerate(resources):
            res = LessonResource(
                lesson_id=lesson.id,
                label=lbl,
                url=url,
                resource_type=rtype,
                position=idx,
            )
            res.save()
        flash("تم تحديث الدرس بنجاح.", "success")
        return redirect(url_for("teacher.lesson_detail", lesson_id=lesson.id))
    elif request.method == "GET":
        form.title.data = lesson.title
        form.content.data = lesson.content
        form.requires_code.data = lesson.requires_code
        form.link_label.data = lesson.link_label
        form.link_url.data = lesson.link_url
        form.link_label_2.data = lesson.link_label_2
        form.link_url_2.data = lesson.link_url_2
    return render_template("teacher/lesson_form.html", form=form, lesson=lesson)


@teacher_bp.route("/lessons/<lesson_id>/delete", methods=["POST"])
@login_required
@role_required("teacher")
def delete_lesson(lesson_id):
    lesson = Lesson.objects(id=lesson_id).first()
    if not lesson:
        raise NotFound()
    scope_response = _ensure_scope_for_lesson(lesson)
    if scope_response:
        return scope_response
    section_id = lesson.section_id.id if lesson.section_id else None
    lesson.delete()
    flash("تم حذف الدرس بنجاح.", "success")
    return redirect(url_for("teacher.section_detail", section_id=section_id))


@teacher_bp.route("/lessons/<lesson_id>/toggle-full-lesson-test", methods=["POST"])
@login_required
@role_required("teacher")
def toggle_lesson_full_test(lesson_id):
    lesson = Lesson.objects(id=lesson_id).first()
    if not lesson:
        raise NotFound()
    scope_response = _ensure_scope_for_lesson(lesson)
    if scope_response:
        return scope_response
    if not is_admin(current_user):
        flash("هذا الإجراء متاح للمشرف فقط.", "error")
        return redirect(url_for("teacher.dashboard"))

    lesson.allow_full_lesson_test = not bool(getattr(lesson, "allow_full_lesson_test", False))
    lesson.save()
    cache.clear()

    if lesson.allow_full_lesson_test:
        flash("تم تفعيل Full lesson test لهذا الدرس.", "success")
    else:
        flash("تم تعطيل Full lesson test لهذا الدرس.", "info")

    return redirect(url_for("teacher.dashboard"))


@teacher_bp.route("/lessons/<lesson_id>/resources/new", methods=["GET", "POST"])
@login_required
@role_required("teacher")
def new_lesson_resource(lesson_id):
    lesson = Lesson.objects(id=lesson_id).first()
    if not lesson:
        raise NotFound()
    scope_response = _ensure_scope_for_lesson(lesson)
    if scope_response:
        return scope_response
    
    if request.method == "POST":
        label = request.form.get("label")
        url = request.form.get("url")
        resource_type = request.form.get("resource_type")
        
        # Get max position for this lesson
        max_pos_resource = LessonResource.objects(lesson_id=lesson.id).order_by('-position').first()
        position = (max_pos_resource.position + 1) if max_pos_resource else 0
        
        resource = LessonResource(
            lesson_id=lesson.id,
            label=label,
            url=url,
            resource_type=resource_type,
            position=position,
        )
        resource.save()
        flash("تم إضافة المورد بنجاح.", "success")
        return redirect(url_for("teacher.lesson_detail", lesson_id=lesson.id))
    
    return render_template("teacher/lesson_resource_form.html", lesson=lesson)


@teacher_bp.route("/lesson-resources/<resource_id>/delete", methods=["POST"])
@login_required
@role_required("teacher")
def delete_lesson_resource(resource_id):
    resource = LessonResource.objects(id=resource_id).first()
    if not resource:
        raise NotFound()
    lesson = resource.lesson_id
    scope_response = _ensure_scope_for_lesson(lesson)
    if scope_response:
        return scope_response
    lesson_id = resource.lesson_id
    resource.delete()
    flash("تم حذف المورد بنجاح.", "success")
    return redirect(url_for("teacher.lesson_detail", lesson_id=lesson_id))


# Test CRUD

@teacher_bp.route("/sections/<section_id>/tests/new", methods=["GET", "POST"])
@login_required
@role_required("teacher")
def new_test(section_id):
    section = Section.objects(id=section_id).first()
    if not section:
        raise NotFound()
    scope_response = _ensure_scope_for_section(section)
    if scope_response:
        return scope_response
    form = TestForm()
    
    # Populate lesson choices for dropdown
    lessons = Lesson.objects(section_id=section.id).all()
    form.lesson_id.choices = [(str(l.id), l.title) for l in lessons]
    form.lesson_id.choices.insert(0, ("", "بدون درس (اختبار شامل)"))
    
    if form.validate_on_submit():
        lesson_ref = None
        if form.lesson_id.data:
            lesson_ref = Lesson.objects(id=form.lesson_id.data).first()
        test = Test(
            section_id=section.id,
            lesson_id=lesson_ref,
            title=form.title.data,
            description=form.description.data,
            created_by=current_user.id,
            requires_code=form.requires_code.data,
        )
        test.save()
        flash("تم إنشاء الاختبار بنجاح.", "success")
        return redirect(url_for("teacher.test_detail", test_id=test.id))
    return render_template("teacher/test_form.html", form=form, section=section)


@teacher_bp.route("/sections/<section_id>/tests/bulk-create", methods=["POST"])
@login_required
@role_required("teacher")
def bulk_create_tests(section_id):
    section = Section.objects(id=section_id).first()
    if not section:
        raise NotFound()
    scope_response = _ensure_scope_for_section(section)
    if scope_response:
        return scope_response

    if not is_admin(current_user):
        flash("هذه الخاصية متاحة للمشرف فقط.", "error")
        return redirect(url_for("teacher.section_detail", section_id=section.id))

    raw_titles = (request.form.get("test_titles") or "").strip()
    lesson_ids_raw = [lid.strip() for lid in request.form.getlist("lesson_ids") if ObjectId.is_valid(lid.strip())]
    description = (request.form.get("description") or "").strip() or None
    requires_code = (request.form.get("requires_code") or "").strip().lower() in {"1", "true", "yes", "on"}

    titles = []
    seen_titles = set()
    for line in raw_titles.splitlines():
        title = line.strip()
        if not title:
            continue
        if title in seen_titles:
            continue
        seen_titles.add(title)
        titles.append(title)

    if not lesson_ids_raw:
        flash("اختر درساً واحداً على الأقل.", "error")
        return redirect(url_for("teacher.section_detail", section_id=section.id))
    if not titles:
        flash("أدخل اسم اختبار واحد على الأقل (اسم في كل سطر).", "error")
        return redirect(url_for("teacher.section_detail", section_id=section.id))

    lessons = list(Lesson.objects(id__in=[ObjectId(lid) for lid in lesson_ids_raw], section_id=section.id).all())
    if not lessons:
        flash("لم يتم العثور على دروس صالحة في هذا القسم.", "error")
        return redirect(url_for("teacher.section_detail", section_id=section.id))

    created = 0
    for lesson in lessons:
        for title in titles:
            Test(
                section_id=section.id,
                lesson_id=lesson.id,
                title=title,
                description=description,
                created_by=current_user.id,
                requires_code=requires_code,
            ).save()
            created += 1

    flash(f"تم إنشاء {created} اختباراً عبر {len(lessons)} درس.", "success")
    return redirect(url_for("teacher.section_detail", section_id=section.id))


@teacher_bp.route("/tests/<test_id>")
@login_required
@role_required("teacher", "question_editor")
@cache.cached(timeout=30, key_prefix=lambda: f"test_detail_{request.view_args.get('test_id', '')}")
def test_detail(test_id):
    test = Test.objects(id=test_id).first()
    if not test:
        raise NotFound()
    scope_response = _ensure_scope_for_test(test)
    if scope_response:
        return scope_response
    questions = list(Question.objects(test_id=test.id).order_by('created_at').all())
    interactive_questions = list(TestInteractiveQuestion.objects(test_id=test.id).order_by('created_at').all())
    return render_template(
        "teacher/test_detail.html",
        test=test,
        questions=questions,
        interactive_questions=interactive_questions,
    )


@teacher_bp.route("/tests/<test_id>/edit", methods=["GET", "POST"])
@login_required
@role_required("teacher", "question_editor")
def edit_test(test_id):
    test = Test.objects(id=test_id).first()
    if not test:
        raise NotFound()
    scope_response = _ensure_scope_for_test(test)
    if scope_response:
        return scope_response

    question_editor_allowed_forms = {
        "upsert_question",
        "delete_question",
        "batch_delete_questions",
        "upsert_interactive_question",
        "delete_interactive_question",
        "import_json",
    }
    
    section = test.section_id
    form = TestForm()
    
    # Populate lesson choices
    lessons = Lesson.objects(section_id=section.id).all()
    form.lesson_id.choices = [(str(l.id), l.title) for l in lessons]
    form.lesson_id.choices.insert(0, ("", "بدون درس (اختبار شامل)"))
    active_tab = request.args.get("tab") or "settings"
    if active_tab not in {"settings", "mcq", "interactive", "import"}:
        active_tab = "settings"

    def _edit_redirect(tab_name="settings"):
        return redirect(url_for("teacher.edit_test", test_id=test.id, tab=tab_name))
    
    if request.method == "POST":
        form_name = request.form.get("form_name")

        if is_question_editor(current_user) and form_name not in question_editor_allowed_forms:
            flash("ليس لديك صلاحية لتنفيذ هذا الإجراء.", "error")
            return _edit_redirect(active_tab)

        if form_name == "update_test":
            if form.validate_on_submit():
                test.title = form.title.data
                test.description = form.description.data
                test.requires_code = form.requires_code.data
                if form.lesson_id.data:
                    test.lesson_id = Lesson.objects(id=form.lesson_id.data).first()
                else:
                    test.lesson_id = None
                test.save()
                flash("تم تحديث الاختبار بنجاح.", "success")
                return redirect(url_for("teacher.test_detail", test_id=test.id))

        elif form_name == "upsert_question":
            def _parse_images(raw):
                if not raw:
                    return []
                parts = []
                for line in str(raw).splitlines():
                    for chunk in line.split(","):
                        val = chunk.strip()
                        if val:
                            normalized = _normalize_image_url(val)
                            if normalized:
                                parts.append(normalized)
                return parts

            question_id = request.form.get("question_id")
            text = (request.form.get("question_text") or "").strip()
            question_images = _parse_images(request.form.get("question_images"))
            hint = (request.form.get("question_hint") or "").strip() or None
            difficulty = (request.form.get("difficulty") or "medium").strip().lower()
            if difficulty not in {"easy", "medium", "hard"}:
                difficulty = "medium"
            correct_choice = request.form.get("correct_choice")
            correct_index = int(correct_choice) if (correct_choice and correct_choice.isdigit()) else None

            choices = []
            for i in range(1, 5):
                choice_text = (request.form.get(f"choice_{i}") or "").strip()
                choice_image = _normalize_image_url(request.form.get(f"choice_{i}_image"))
                if choice_text:
                    choices.append(Choice(text=choice_text, image_url=choice_image, is_correct=(correct_index == i)))

            if not text:
                flash("نص السؤال مطلوب.", "error")
                return _edit_redirect("mcq")

            if not choices:
                flash("يجب إضافة خيار واحد على الأقل.", "error")
                return _edit_redirect("mcq")

            if not any(c.is_correct for c in choices):
                choices[0].is_correct = True

            question = None
            if question_id:
                question = Question.objects(id=question_id, test_id=test.id).first()

            if question:
                question.text = text
                question.question_images = question_images
                question.hint = hint
                question.difficulty = difficulty
                question.choices = choices
                correct_choice = next((c for c in choices if c.is_correct), None)
                question.correct_choice_id = correct_choice.choice_id if correct_choice else None
                question.save()
                flash("تم تحديث السؤال.", "success")
            else:
                correct_choice = next((c for c in choices if c.is_correct), None)
                question = Question(
                    test_id=test.id,
                    text=text,
                    question_images=question_images,
                    hint=hint,
                    difficulty=difficulty,
                    choices=choices,
                    correct_choice_id=correct_choice.choice_id if correct_choice else None,
                )
                question.save()
                flash("تمت إضافة السؤال.", "success")

            return _edit_redirect("mcq")

        elif form_name == "delete_question":
            question_id = request.form.get("question_id")
            if question_id:
                q = Question.objects(id=question_id, test_id=test.id).first()
                if q:
                    q.delete()
                    flash("تم حذف السؤال.", "success")
            return _edit_redirect("mcq")

        elif form_name == "batch_delete_questions":
            raw_ids = request.form.get("question_ids") or ""
            question_ids = [qid for qid in raw_ids.split(",") if qid.strip()]
            if question_ids:
                deleted = Question.objects(id__in=question_ids, test_id=test.id).delete()
                flash(f"تم حذف {deleted} سؤال.", "success")
            else:
                flash("لم يتم اختيار أي سؤال.", "warning")
            return _edit_redirect("mcq")

        elif form_name == "upsert_interactive_question":
            interactive_question_id = request.form.get("interactive_question_id")
            q_text = (request.form.get("interactive_question_text") or "").strip() or None
            q_img = _normalize_image_url(request.form.get("interactive_question_image_url"))
            a_text = (request.form.get("interactive_answer_text") or "").strip() or None
            a_img = _normalize_image_url(request.form.get("interactive_answer_image_url"))
            difficulty = (request.form.get("interactive_difficulty") or "medium").strip().lower()
            if difficulty not in {"easy", "medium", "hard"}:
                difficulty = "medium"

            if not q_text and not q_img:
                flash("أدخل نص السؤال التفاعلي أو رابط صورته على الأقل.", "error")
                return _edit_redirect("interactive")
            if not a_text and not a_img:
                flash("أدخل نص الإجابة التفاعلية أو رابط صورتها على الأقل.", "error")
                return _edit_redirect("interactive")

            row = None
            if interactive_question_id:
                row = TestInteractiveQuestion.objects(id=interactive_question_id, test_id=test.id).first()

            if row:
                row.question_text = q_text
                row.question_image_url = q_img
                row.answer_text = a_text
                row.answer_image_url = a_img
                row.difficulty = difficulty
                row.correct_value = True
                row.save()
                flash("تم تحديث السؤال التفاعلي.", "success")
            else:
                TestInteractiveQuestion(
                    test_id=test.id,
                    question_text=q_text,
                    question_image_url=q_img,
                    answer_text=a_text,
                    answer_image_url=a_img,
                    difficulty=difficulty,
                    correct_value=True,
                ).save()
                flash("تمت إضافة سؤال تفاعلي.", "success")

            return _edit_redirect("interactive")

        elif form_name == "delete_interactive_question":
            interactive_question_id = request.form.get("interactive_question_id")
            if interactive_question_id:
                iq = TestInteractiveQuestion.objects(id=interactive_question_id, test_id=test.id).first()
                if iq:
                    iq.delete()
                    flash("تم حذف السؤال التفاعلي.", "success")
            return _edit_redirect("interactive")

        elif form_name == "import_json":
            def _to_bool(val):
                if isinstance(val, bool):
                    return val
                if isinstance(val, (int, float)):
                    return val != 0
                if isinstance(val, str):
                    return val.strip().lower() in {"true", "1", "yes", "on"}
                return False

            def _normalize_images(raw):
                if not raw:
                    return []
                if isinstance(raw, str):
                    raw = [raw]
                if not isinstance(raw, list):
                    return []
                return [str(u).strip() for u in raw if str(u).strip()]

            raw_json = request.form.get("questions_json") or ""
            upload = request.files.get("questions_file")
            include_hints = request.form.get("include_hints") == "1"
            import_level = (request.form.get("import_difficulty") or "from_json").strip().lower()
            if import_level not in {"from_json", "easy", "medium", "hard"}:
                import_level = "from_json"
            if upload and upload.filename:
                raw_json = upload.read().decode("utf-8")

            try:
                payload = json.loads(raw_json) if raw_json.strip() else None
            except Exception as exc:
                flash(f"JSON غير صحيح: {exc}", "error")
                return _edit_redirect("import")

            if not payload:
                flash("لا يوجد JSON صالح للاستيراد.", "error")
                return _edit_redirect("import")

            items = payload
            if isinstance(payload, dict):
                items = payload.get("quiz") or payload.get("questions") or payload.get("items") or []

            if not isinstance(items, list):
                flash("تنسيق JSON غير مدعوم.", "error")
                return _edit_redirect("import")

            imported = 0
            for item in items:
                if not isinstance(item, dict):
                    continue
                q_text = (item.get("question") or item.get("text") or "").strip()
                question_images = _normalize_images(
                    item.get("questionImages")
                    or item.get("question_images")
                    or item.get("images")
                )
                q_hint = (item.get("hint") or "").strip() if include_hints else None
                if import_level == "from_json":
                    difficulty = (item.get("difficulty") or item.get("level") or "medium").strip().lower()
                    if difficulty not in {"easy", "medium", "hard"}:
                        difficulty = "medium"
                else:
                    difficulty = import_level
                answer_options = item.get("answerOptions") or item.get("answer_options") or None
                choices_list = item.get("choices") or item.get("options") or []
                if not q_text:
                    continue
                if answer_options is None and (not isinstance(choices_list, list) or len(choices_list) == 0):
                    continue
                if answer_options is not None and (not isinstance(answer_options, list) or len(answer_options) == 0):
                    continue

                correct = item.get("answer") if "answer" in item else item.get("correct")
                correct_indices = set()

                if isinstance(correct, int):
                    correct_indices.add(correct if correct >= 1 else correct + 1)
                elif isinstance(correct, list):
                    for c in correct:
                        if isinstance(c, int):
                            correct_indices.add(c if c >= 1 else c + 1)
                elif isinstance(correct, str):
                    if correct.strip().isdigit():
                        correct_indices.add(int(correct.strip()))
                    else:
                        for idx, opt in enumerate(choices_list, start=1):
                            if str(opt).strip() == correct.strip():
                                correct_indices.add(idx)

                choices = []
                has_correct = False
                if answer_options is not None:
                    for idx, opt in enumerate(answer_options, start=1):
                        if not isinstance(opt, dict):
                            continue
                        opt_text = str(opt.get("text", "")).strip()
                        if not opt_text:
                            continue
                        opt_image = (
                            opt.get("image")
                            or opt.get("imageUrl")
                            or opt.get("image_url")
                            or opt.get("imageUri")
                            or opt.get("image_uri")
                        )
                        opt_image = _normalize_image_url(opt_image)
                        is_correct = _to_bool(opt.get("isCorrect") if "isCorrect" in opt else opt.get("is_correct"))
                        if not is_correct and correct_indices:
                            if idx in correct_indices:
                                is_correct = True
                        if not is_correct and isinstance(correct, str) and correct.strip():
                            if opt_text == correct.strip():
                                is_correct = True
                        if is_correct:
                            has_correct = True
                        choices.append(Choice(text=opt_text, image_url=opt_image, is_correct=is_correct))
                else:
                    for idx, opt in enumerate(choices_list, start=1):
                        if opt is None:
                            continue
                        if isinstance(opt, dict):
                            opt_text = str(opt.get("text", "")).strip()
                            opt_image = (
                                opt.get("image")
                                or opt.get("imageUrl")
                                or opt.get("image_url")
                                or opt.get("imageUri")
                                or opt.get("image_uri")
                            )
                            opt_image = _normalize_image_url(opt_image)
                        else:
                            opt_text = str(opt).strip()
                            opt_image = None
                        if not opt_text:
                            continue
                        is_correct = (idx in correct_indices)
                        if is_correct:
                            has_correct = True
                        choices.append(Choice(text=opt_text, image_url=opt_image, is_correct=is_correct))

                if choices and not has_correct:
                    choices[0].is_correct = True
                    has_correct = True

                correct_choice = next((c for c in choices if c.is_correct), None)

                q = Question(
                    test_id=test.id,
                    text=q_text,
                    question_images=question_images,
                    hint=q_hint,
                    difficulty=difficulty,
                    choices=choices,
                    correct_choice_id=correct_choice.choice_id if correct_choice else None,
                )
                q.save()
                imported += 1

            flash(f"تم استيراد {imported} سؤال.", "success")
            return _edit_redirect("import")
    elif request.method == "GET":
        form.title.data = test.title
        form.description.data = test.description
        form.requires_code.data = test.requires_code
        form.lesson_id.data = str(test.lesson_id.id) if test.lesson_id else ""
    
    # Backfill correct_choice_id for existing questions if missing
    for q in Question.objects(test_id=test.id).all():
        if not q.correct_choice_id:
            correct_choice = next((c for c in q.choices if c.is_correct), None)
            if correct_choice:
                q.correct_choice_id = correct_choice.choice_id
                q.save()

    interactive_questions = list(TestInteractiveQuestion.objects(test_id=test.id).order_by('created_at').all())
    return render_template(
        "teacher/test_edit.html",
        form=form,
        meta_form=form,
        test=test,
        interactive_questions=interactive_questions,
        active_tab=active_tab,
    )


@teacher_bp.route("/tests/<test_id>/delete", methods=["POST"])
@login_required
@role_required("teacher")
def delete_test(test_id):
    test = Test.objects(id=test_id).first()
    if not test:
        raise NotFound()
    scope_response = _ensure_scope_for_test(test)
    if scope_response:
        return scope_response
    section_id = test.section_id.id if test.section_id else None
    lesson_id = test.lesson_id.id if test.lesson_id else None
    test.delete()
    flash("تم حذف الاختبار بنجاح.", "success")
    if lesson_id:
        return redirect(url_for("teacher.lesson_detail", lesson_id=lesson_id))
    return redirect(url_for("teacher.section_detail", section_id=section_id))


@teacher_bp.route("/questions/<question_id>/delete", methods=["POST"])
@login_required
@role_required("teacher", "question_editor")
def delete_question(question_id):
    question = Question.objects(id=question_id).first()
    if not question:
        raise NotFound()
    scope_response = _ensure_scope_for_test(question.test_id)
    if scope_response:
        return scope_response
    test_id = str(question.test_id.id)
    question.delete()
    cache.delete(f"test_detail_{test_id}")  # Clear test detail cache
    flash("تم حذف السؤال بنجاح.", "success")
    return redirect(url_for("teacher.test_detail", test_id=test_id))


# Helpers

def revoke_lesson_activation(lesson_id, student_id):
    """Mark lesson activation as inactive for a student"""
    for activation in LessonActivation.objects(lesson_id=lesson_id, student_id=student_id, active=True).all():
        activation.active = False
        activation.save()
