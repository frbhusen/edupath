from flask import Blueprint, render_template, redirect, url_for, flash, request, session
import json
import random
from datetime import datetime
from flask_login import login_required, current_user
from bson import ObjectId

from .models import Subject, Section, Lesson, Test, Question, Choice, Attempt, AttemptAnswer, ActivationCode, SectionActivation, LessonActivationCode, LessonActivation, SubjectActivation, SubjectActivationCode, CustomTestAttempt, CustomTestAnswer
from .forms import ActivationForm, LessonActivationForm
from .activation_utils import cascade_subject_activation, cascade_section_activation, cascade_lesson_activation

student_bp = Blueprint("student", __name__, template_folder="templates")


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
            self.lesson_activation_ids = {
                la.lesson_id
                for la in LessonActivation.objects(
                    lesson_id__in=lesson_ids,
                    student_id=student_id,
                    active=True,
                ).all()
            }
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


def get_unlocked_lessons(student_id: int):
    """Collect all lessons the student can access across all sections."""
    lessons = []
    sections = Section.objects().all()
    for section in sections:
        access = AccessContext(section, student_id)
        for lesson in section.lessons:
            if access.lesson_open(lesson):
                lessons.append(lesson)
    return lessons

@student_bp.route("/subjects")
@login_required
def subjects():
    subs = Subject.objects().all()
    
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
def subject_detail(subject_id):
    subject = Subject.objects(id=subject_id).first()
    if not subject:
        return "404", 404
    
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
        for section in subject.sections:
            access = AccessContext(section, current_user.id)
            sections_data.append({
                "section": section,
                "is_open": access.section_open,
                "requires_code": access.section_requires_code,
            })
    else:
        for section in subject.sections:
            sections_data.append({
                "section": section,
                "is_open": True,
                "requires_code": False,
            })

    return render_template(
        "student/subject_detail.html",
        subject=subject,
        subject_activation=subject_activation,
        subject_requires_code=subject_requires_code,
        subject_open=subject_open,
        sections_data=sections_data,
    )

@student_bp.route("/sections/<section_id>")
@login_required
def section_detail(section_id):
    section = Section.objects(id=section_id).first()
    if not section:
        return "404", 404
    if current_user.role == "student":
        access = AccessContext(section, current_user.id)
        lessons_data = [
            {"lesson": lesson, "is_open": access.lesson_open(lesson)}
            for lesson in sorted(section.lessons, key=lambda l: l.id)
        ]
        tests_data = [
            {"test": test, "is_open": access.test_open(test)}
            for test in sorted([t for t in section.tests if t.lesson_id is None], key=lambda t: t.id)
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
    lessons_data = [{"lesson": l, "is_open": True} for l in sorted(section.lessons, key=lambda l: l.id)]
    tests_data = [{"test": t, "is_open": True} for t in sorted([t for t in section.tests if t.lesson_id is None], key=lambda t: t.id)]
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

    if current_user.role == "student":
        access = AccessContext(section, current_user.id)
        tests_data = [
            {"test": test, "is_open": access.test_open(test)}
            for test in sorted(lesson.tests, key=lambda t: t.id)
        ]
    else:
        tests_data = [{"test": t, "is_open": True} for t in sorted(lesson.tests, key=lambda t: t.id)]

    return render_template(
        "student/lesson_detail.html",
        lesson=lesson,
        section=section,
        resources=resources,
        tests_data=tests_data,
    )

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
    min_select = 10 if total_questions_available >= 10 else total_questions_available
    max_select = min(50, total_questions_available)
    selected_count = request.args.get("count")
    try:
        selected_count = int(selected_count) if selected_count else None
    except ValueError:
        selected_count = None
    if selected_count:
        lower_bound = 10 if total_questions_available >= 10 else 1
        selected_count = max(lower_bound, min(selected_count, max_select))

    if request.method == "POST":
        # Evaluate answers
        question_ids_raw = request.form.get("question_ids", "")
        if question_ids_raw:
            question_ids = [qid.strip() for qid in question_ids_raw.split(",") if ObjectId.is_valid(qid.strip())]
            questions = Question.objects(id__in=question_ids).all()
            questions_by_id = {str(q.id): q for q in questions}
            ordered_questions = [questions_by_id[qid] for qid in question_ids if qid in questions_by_id]
        else:
            ordered_questions = list(test.questions)
        total = len(ordered_questions)
        score = 0
        attempt = Attempt(test_id=test.id, student_id=current_user.id, score=0, total=total)
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
        attempt.score = score
        attempt.save()
        flash(f"حصلت على {score}/{total}", "success")
        return redirect(url_for("student.test_result", attempt_id=attempt.id))

    ordered_questions = []
    question_ids_str = ""
    time_limit_seconds = None
    if selected_count:
        questions = list(test.questions)
        if selected_count < len(questions):
            questions = random.sample(questions, selected_count)
        random.shuffle(questions)
        question_ids = [q.id for q in questions]
        question_ids_str = ",".join(str(qid) for qid in question_ids)
        for q in questions:
            choices = list(q.choices)
            random.shuffle(choices)
            ordered_questions.append({"question": q, "choices": choices})
        time_limit_seconds = (len(question_ids) * 75) + 15

    return render_template(
        "student/take_test.html",
        test=test,
        total_questions=total_questions_available,
        min_select=min_select,
        max_select=max_select,
        selected_count=selected_count,
        ordered_questions=ordered_questions,
        question_ids_str=question_ids_str,
        time_limit_seconds=time_limit_seconds,
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


@student_bp.route("/results")
@login_required
def results():
    # Split own attempts and others for clearer presentation; answers still gated in test_result
    def _filter_missing_tests(attempts):
        filtered = []
        for attempt in attempts:
            try:
                _ = attempt.test
                filtered.append(attempt)
            except Exception:
                continue
        return filtered

    own_attempts = _filter_missing_tests(
        Attempt.objects(student_id=current_user.id)
        .order_by("-started_at")
        .all()
    )
    other_attempts = _filter_missing_tests(
        Attempt.objects(student_id__ne=current_user.id)
        .order_by("-started_at")
        .all()
    )
    return render_template(
        "student/results.html",
        own_attempts=own_attempts,
        other_attempts=other_attempts,
    )

@student_bp.route("/results/<attempt_id>")
@login_required
def test_result(attempt_id):
    attempt = Attempt.objects(id=attempt_id).first()
    if not attempt:
        return "404", 404
    if str(attempt.student_id.id) != str(current_user.id) and current_user.role != "teacher":
        flash("غير مسموح", "error")
        return redirect(url_for("student.subjects"))
    questions = attempt.test.questions
    answers = AttemptAnswer.objects(attempt_id=attempt.id).all()
    answers_map = {str(a.question_id.id): a for a in answers if a.question_id}

    review = []
    for q in questions:
        ans = answers_map.get(str(q.id))
        selected_choice = None
        if ans and ans.choice_id:
            selected_choice = next((c for c in q.choices if c.choice_id == ans.choice_id), None)
        correct_choice = next((c for c in q.choices if c.is_correct), None)
        review.append({
            "question": q,
            "selected_choice": selected_choice,
            "correct_choice": correct_choice,
            "is_correct": ans.is_correct if ans else False,
        })

    return render_template("student/test_result.html", attempt=attempt, review=review)


@student_bp.route("/custom-tests/new", methods=["GET", "POST"])
@login_required
def custom_test_new():
    subjects = Subject.objects().all()
    selected_subject_id = request.args.get("subject_id") or request.form.get("subject_id")
    if selected_subject_id and not ObjectId.is_valid(str(selected_subject_id)):
        selected_subject_id = None

    if current_user.role == "student":
        unlocked_lessons = get_unlocked_lessons(current_user.id)
    else:
        unlocked_lessons = Lesson.objects().all()
    subject_filter = None
    if selected_subject_id:
        subject_filter = Subject.objects(id=selected_subject_id).first()
        if subject_filter:
            unlocked_lessons = [
                lesson for lesson in unlocked_lessons
                if lesson.section and lesson.section.subject_id == subject_filter
            ]

    lesson_question_counts = {}
    for lesson in unlocked_lessons:
        tests = Test.objects(lesson_id=lesson.id).all()
        test_ids = [t.id for t in tests]
        lesson_question_counts[lesson.id] = Question.objects(test_id__in=test_ids).count() if test_ids else 0
    total_available_questions = sum(lesson_question_counts.values())

    if request.method == "POST":
        if not selected_subject_id:
            flash("اختر مادة قبل إنشاء اختبار مخصص.", "error")
            return redirect(url_for("student.custom_test_new"))

        selections = []
        total_questions = 0
        for lesson in unlocked_lessons:
            raw = request.form.get(f"lesson_{lesson.id}")
            if not raw:
                continue
            try:
                count = int(raw)
            except ValueError:
                count = 0
            if count <= 0:
                continue
            max_available = lesson_question_counts.get(lesson.id, 0)
            if count > max_available:
                flash(f"تم طلب {count} أسئلة لـ {lesson.title}، ولكن {max_available} فقط متاحة.", "error")
                return redirect(url_for("student.custom_test_new", subject_id=selected_subject_id))
            selections.append({"lesson_id": str(lesson.id), "count": count})
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
            lesson_questions = list(Question.objects(test_id__in=test_ids)) if test_ids else []
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
        total_available_questions=total_available_questions,
    )


@student_bp.route("/custom-tests/<attempt_id>")
@login_required
def custom_test_take(attempt_id):
    attempt = CustomTestAttempt.objects(id=attempt_id).first()
    if not attempt:
        return "404", 404
    if attempt.student_id != current_user.id:
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
    if attempt.student_id != current_user.id:
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
    attempt.save()
    return redirect(url_for("student.custom_test_result", attempt_id=attempt.id))


@student_bp.route("/custom-tests/<attempt_id>/result")
@login_required
def custom_test_result(attempt_id):
    attempt = CustomTestAttempt.objects(id=attempt_id).first()
    if not attempt:
        return "404", 404
    if attempt.student_id != current_user.id:
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

    return render_template("student/custom_test_result.html", attempt=attempt, review=review)


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
