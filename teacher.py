from flask import Blueprint, render_template, redirect, url_for, flash, request
import json
from flask_login import login_required, current_user
from werkzeug.exceptions import NotFound

from .models import (
    User, Subject, Section, Lesson, LessonResource, Test, Question, Choice, 
    ActivationCode, SectionActivation, LessonActivation, LessonActivationCode,
    SubjectActivation, SubjectActivationCode, Attempt, AttemptAnswer
)
from .activation_utils import (
    cascade_subject_activation, cascade_section_activation, cascade_lesson_activation,
    revoke_subject_activation, revoke_section_activation, lock_subject_access_for_all, lock_section_access_for_all
)
from .forms import SubjectForm, SectionForm, LessonForm, TestForm, StudentEditForm
from .extensions import cache

teacher_bp = Blueprint("teacher", __name__, template_folder="templates")


def _generate_unique_code(model_cls, length: int = 6) -> str:
    """Generate a unique activation code for the given model class."""
    import random
    import string

    code_value = ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))
    while model_cls.objects(code=code_value).first():
        code_value = ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))
    return code_value

# Role guard decorator

def role_required(role):
    def decorator(fn):
        from functools import wraps
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated:
                flash("ليس لديك صلاحية للوصول إلى هذه الصفحة.", "error")
                return redirect(url_for("auth.login"))
            # Allow admins to access teacher routes as well
            allowed_roles = {role, "admin"}
            if (current_user.role or "").lower() not in {r.lower() for r in allowed_roles}:
                flash("ليس لديك صلاحية للوصول إلى هذه الصفحة.", "error")
                return redirect(url_for("auth.login"))
            return fn(*args, **kwargs)
        return wrapper
    return decorator

@teacher_bp.route("/dashboard")
@login_required
@role_required("teacher")
@cache.cached(timeout=60, key_prefix=lambda: f"teacher_dashboard_{current_user.id}_{request.args.get('page', 1)}")
def dashboard():
    # Pagination for better performance - reduced to 10 for faster loading
    page = request.args.get('page', 1, type=int)
    per_page = 10
    
    subjects_query = Subject.objects().order_by('-created_at')
    total_subjects = subjects_query.count()
    subjects = list(subjects_query.skip((page - 1) * per_page).limit(per_page))
    
    # Bulk load sections, lessons, and tests to avoid N+1 in templates
    if subjects:
        subject_ids = [s.id for s in subjects]
        sections = list(Section.objects(subject_id__in=subject_ids).all())
        
        # Group sections by subject
        sections_by_subject = {}
        for section in sections:
            subject_id = section.subject_id.id
            if subject_id not in sections_by_subject:
                sections_by_subject[subject_id] = []
            sections_by_subject[subject_id].append(section)
        
        # Bulk load lessons and tests
        section_ids = [s.id for s in sections]
        lessons = list(Lesson.objects(section_id__in=section_ids).all())
        tests = list(Test.objects(section_id__in=section_ids).all())
        
        # Group by section
        lessons_by_section = {}
        tests_by_section = {}
        tests_by_lesson = {}
        
        for lesson in lessons:
            section_id = lesson.section_id.id
            if section_id not in lessons_by_section:
                lessons_by_section[section_id] = []
            lessons_by_section[section_id].append(lesson)
        
        for test in tests:
            section_id = test.section_id.id
            if section_id not in tests_by_section:
                tests_by_section[section_id] = []
            tests_by_section[section_id].append(test)
            
            # Also group by lesson if test is linked to lesson
            if test.lesson_id:
                lesson_id = test.lesson_id.id
                if lesson_id not in tests_by_lesson:
                    tests_by_lesson[lesson_id] = []
                tests_by_lesson[lesson_id].append(test)
        
        # Attach to subjects for template use
        for subject in subjects:
            subject._cached_sections = sections_by_subject.get(subject.id, [])
            for section in subject._cached_sections:
                section._cached_lessons = lessons_by_section.get(section.id, [])
                section._cached_tests = tests_by_section.get(section.id, [])
                # Also attach test counts to lessons
                for lesson in section._cached_lessons:
                    lesson._cached_test_count = len(tests_by_lesson.get(lesson.id, []))
    
    # Calculate pagination info
    total_pages = (total_subjects + per_page - 1) // per_page
    has_prev = page > 1
    has_next = page < total_pages
    
    return render_template(
        "teacher/dashboard.html", 
        subjects=subjects,
        page=page,
        total_pages=total_pages,
        has_prev=has_prev,
        has_next=has_next,
        total_subjects=total_subjects
    )


# Redirect teacher base to dashboard for Up navigation
@teacher_bp.route("/")
@login_required
@role_required("teacher")
def root():
    return redirect(url_for("teacher.dashboard"))


@teacher_bp.route("/results")
@login_required
@role_required("teacher")
def results_overview():
    # Pagination for performance
    page = request.args.get('page', 1, type=int)
    per_page = 50
    
    attempts_query = Attempt.objects().order_by('-started_at')
    total_attempts = attempts_query.count()
    attempts = attempts_query.skip((page - 1) * per_page).limit(per_page)
    
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
    
    attempts_query = Attempt.objects(student_id=student.id).order_by('-started_at')
    total_attempts = attempts_query.count()
    attempts = attempts_query.skip((page - 1) * per_page).limit(per_page)
    
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
    return render_template("teacher/student_results.html", student=student, attempts=attempts)


@teacher_bp.route("/attempts/<attempt_id>", methods=["GET", "POST"])
@login_required
@role_required("teacher")
def manage_attempt(attempt_id):
    attempt = Attempt.objects(id=attempt_id).first()
    if not attempt:
        raise NotFound()
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

            attempt.score = sum(1 for aa in AttemptAnswer.objects(attempt_id=attempt.id) if aa.is_correct)
            attempt.save()
            flash("تم حفظ الدرجات بنجاح.", "success")

    answers = {aa.question_id: aa for aa in AttemptAnswer.objects(attempt_id=attempt.id).all()}
    return render_template("teacher/attempt_manage.html", attempt=attempt, student=student, test=test, questions=questions, answers=answers)


@teacher_bp.route("/students", methods=["GET"])
@login_required
@role_required("teacher")
def students():
    students = User.objects(role="student").order_by('-created_at').all()
    return render_template("teacher/students.html", students=students)


@teacher_bp.route("/students/<user_id>/edit", methods=["GET", "POST"])
@login_required
@role_required("teacher")
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
    activated_students = {
        sa.student_id.id: sa for sa in SubjectActivation.objects(subject_id=subject.id, active=True).all()
    }
    codes = SubjectActivationCode.objects(subject_id=subject.id).order_by('-created_at').all()
    codes_by_student = {}
    for code in codes:
        key = code.student_id.id if code.student_id else None
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
    activations = {sa.student_id.id: sa for sa in SectionActivation.objects(section_id=section.id, active=True).all()}
    codes = ActivationCode.objects(section_id=section.id).order_by('-created_at').all()
    codes_by_student = {}
    for code in codes:
        key = code.student_id.id if code.student_id else None
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
    activations = {la.student_id.id: la for la in LessonActivation.objects(lesson_id=lesson.id, active=True).all()}
    codes = LessonActivationCode.objects(lesson_id=lesson.id).order_by('-created_at').all()
    codes_by_student = {}
    for code in codes:
        key = code.student_id.id if code.student_id else None
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
@role_required("teacher")
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
    sections = Section.objects(subject_id=subject.id).all()
    return render_template("teacher/subject_detail.html", subject=subject, sections=sections)


@teacher_bp.route("/subjects/<subject_id>/edit", methods=["GET", "POST"])
@login_required
@role_required("teacher")
def edit_subject(subject_id):
    subject = Subject.objects(id=subject_id).first()
    if not subject:
        raise NotFound()
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


@teacher_bp.route("/sections/<section_id>")
@login_required
@role_required("teacher")
def section_detail(section_id):
    section = Section.objects(id=section_id).first()
    if not section:
        raise NotFound()
    lessons = Lesson.objects(section_id=section.id).all()
    tests = Test.objects(section_id=section.id, lesson_id=None).all()  # section-wide tests
    return render_template("teacher/section_detail.html", section=section, lessons=lessons, tests=tests)


@teacher_bp.route("/sections/<section_id>/edit", methods=["GET", "POST"])
@login_required
@role_required("teacher")
def edit_section(section_id):
    section = Section.objects(id=section_id).first()
    if not section:
        raise NotFound()
    subject = section.subject_id
    form = SectionForm()
    if form.validate_on_submit():
        section.title = form.title.data
        section.description = form.description.data
        section.requires_code = form.requires_code.data
        section.save()
        flash("تم تحديث القسم بنجاح.", "success")
        return redirect(url_for("teacher.section_detail", section_id=section.id))
    elif request.method == "GET":
        form.title.data = section.title
        form.description.data = section.description
        form.requires_code.data = section.requires_code
    return render_template("teacher/section_form.html", form=form, section=section, subject=subject)


@teacher_bp.route("/sections/<section_id>/delete", methods=["POST"])
@login_required
@role_required("teacher")
def delete_section(section_id):
    section = Section.objects(id=section_id).first()
    if not section:
        raise NotFound()
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
        return redirect(url_for("teacher.lesson_detail", lesson_id=lesson.id))
    return render_template("teacher/lesson_form.html", form=form, section=section)


@teacher_bp.route("/lessons/<lesson_id>")
@login_required
@role_required("teacher")
def lesson_detail(lesson_id):
    lesson = Lesson.objects(id=lesson_id).first()
    if not lesson:
        raise NotFound()
    return redirect(url_for("teacher.edit_lesson", lesson_id=lesson.id))


@teacher_bp.route("/lessons/<lesson_id>/edit", methods=["GET", "POST"])
@login_required
@role_required("teacher")
def edit_lesson(lesson_id):
    lesson = Lesson.objects(id=lesson_id).first()
    if not lesson:
        raise NotFound()
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
    section_id = lesson.section_id.id if lesson.section_id else None
    lesson.delete()
    flash("تم حذف الدرس بنجاح.", "success")
    return redirect(url_for("teacher.section_detail", section_id=section_id))


@teacher_bp.route("/lessons/<lesson_id>/resources/new", methods=["GET", "POST"])
@login_required
@role_required("teacher")
def new_lesson_resource(lesson_id):
    lesson = Lesson.objects(id=lesson_id).first()
    if not lesson:
        raise NotFound()
    
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


@teacher_bp.route("/tests/<test_id>")
@login_required
@role_required("teacher")
@cache.cached(timeout=30, key_prefix=lambda: f"test_detail_{test_id}")
def test_detail(test_id):
    test = Test.objects(id=test_id).first()
    if not test:
        raise NotFound()
    questions = list(Question.objects(test_id=test.id).order_by('created_at').all())
    return render_template("teacher/test_detail.html", test=test, questions=questions)


@teacher_bp.route("/tests/<test_id>/edit", methods=["GET", "POST"])
@login_required
@role_required("teacher")
def edit_test(test_id):
    test = Test.objects(id=test_id).first()
    if not test:
        raise NotFound()
    
    section = test.section_id
    form = TestForm()
    
    # Populate lesson choices
    lessons = Lesson.objects(section_id=section.id).all()
    form.lesson_id.choices = [(str(l.id), l.title) for l in lessons]
    form.lesson_id.choices.insert(0, ("", "بدون درس (اختبار شامل)"))
    
    if request.method == "POST":
        form_name = request.form.get("form_name")

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
                            parts.append(val)
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
                choice_image = (request.form.get(f"choice_{i}_image") or "").strip() or None
                if choice_text:
                    choices.append(Choice(text=choice_text, image_url=choice_image, is_correct=(correct_index == i)))

            if not text:
                flash("نص السؤال مطلوب.", "error")
                return redirect(url_for("teacher.edit_test", test_id=test.id))

            if not choices:
                flash("يجب إضافة خيار واحد على الأقل.", "error")
                return redirect(url_for("teacher.edit_test", test_id=test.id))

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

            return redirect(url_for("teacher.edit_test", test_id=test.id))

        elif form_name == "delete_question":
            question_id = request.form.get("question_id")
            if question_id:
                q = Question.objects(id=question_id, test_id=test.id).first()
                if q:
                    q.delete()
                    flash("تم حذف السؤال.", "success")
            return redirect(url_for("teacher.edit_test", test_id=test.id))

        elif form_name == "batch_delete_questions":
            raw_ids = request.form.get("question_ids") or ""
            question_ids = [qid for qid in raw_ids.split(",") if qid.strip()]
            if question_ids:
                deleted = Question.objects(id__in=question_ids, test_id=test.id).delete()
                flash(f"تم حذف {deleted} سؤال.", "success")
            else:
                flash("لم يتم اختيار أي سؤال.", "warning")
            return redirect(url_for("teacher.edit_test", test_id=test.id))

        elif form_name == "save_test_questions":
            raw_questions = request.form.get("draft_questions") or "[]"
            try:
                items = json.loads(raw_questions)
            except Exception as exc:
                flash(f"فشل حفظ الأسئلة: {exc}", "error")
                return redirect(url_for("teacher.edit_test", test_id=test.id))

            if not isinstance(items, list):
                flash("تنسيق الأسئلة غير صالح.", "error")
                return redirect(url_for("teacher.edit_test", test_id=test.id))

            sanitized = []
            for idx, item in enumerate(items, start=1):
                if not isinstance(item, dict):
                    continue
                text = (item.get("text") or "").strip()
                if not text:
                    flash(f"نص السؤال مطلوب (سؤال {idx}).", "error")
                    return redirect(url_for("teacher.edit_test", test_id=test.id))

                question_images = item.get("question_images") or []
                if not isinstance(question_images, list):
                    question_images = [question_images]
                question_images = [str(u).strip() for u in question_images if str(u).strip()]

                hint = (item.get("hint") or "").strip() or None
                difficulty = (item.get("difficulty") or "medium").strip().lower()
                if difficulty not in {"easy", "medium", "hard"}:
                    difficulty = "medium"

                choices_in = item.get("choices") or []
                if not isinstance(choices_in, list):
                    choices_in = []
                choices = []
                for choice in choices_in:
                    if not isinstance(choice, dict):
                        continue
                    choice_text = str(choice.get("text") or "").strip()
                    if not choice_text:
                        continue
                    choice_image = choice.get("image_url")
                    choice_image = str(choice_image).strip() if choice_image else None
                    is_correct = bool(choice.get("is_correct"))
                    choices.append({
                        "text": choice_text,
                        "image_url": choice_image,
                        "is_correct": is_correct,
                    })

                if len(choices) < 2:
                    flash(f"يجب أن يحتوي كل سؤال على خيارين على الأقل (سؤال {idx}).", "error")
                    return redirect(url_for("teacher.edit_test", test_id=test.id))

                if not any(c["is_correct"] for c in choices):
                    choices[0]["is_correct"] = True

                sanitized.append({
                    "id": item.get("id"),
                    "text": text,
                    "question_images": question_images,
                    "hint": hint,
                    "difficulty": difficulty,
                    "choices": choices,
                })

            existing = {str(q.id): q for q in Question.objects(test_id=test.id).all()}
            keep_ids = set()
            for item in sanitized:
                q_id = str(item.get("id") or "")
                question = existing.get(q_id)
                choices_docs = [
                    Choice(
                        text=c["text"],
                        image_url=c["image_url"],
                        is_correct=c["is_correct"],
                    )
                    for c in item["choices"]
                ]
                correct_choice = next((c for c in choices_docs if c.is_correct), None)
                if question:
                    question.text = item["text"]
                    question.question_images = item["question_images"]
                    question.hint = item["hint"]
                    question.difficulty = item["difficulty"]
                    question.choices = choices_docs
                    question.correct_choice_id = correct_choice.choice_id if correct_choice else None
                    question.save()
                    keep_ids.add(str(question.id))
                else:
                    question = Question(
                        test_id=test.id,
                        text=item["text"],
                        question_images=item["question_images"],
                        hint=item["hint"],
                        difficulty=item["difficulty"],
                        choices=choices_docs,
                        correct_choice_id=correct_choice.choice_id if correct_choice else None,
                    )
                    question.save()
                    keep_ids.add(str(question.id))

            for q in existing.values():
                if str(q.id) not in keep_ids:
                    q.delete()

            flash("تم حفظ الأسئلة بنجاح.", "success")
            return redirect(url_for("teacher.edit_test", test_id=test.id))

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
                return redirect(url_for("teacher.edit_test", test_id=test.id))

            if not payload:
                flash("لا يوجد JSON صالح للاستيراد.", "error")
                return redirect(url_for("teacher.edit_test", test_id=test.id))

            items = payload
            if isinstance(payload, dict):
                items = payload.get("quiz") or payload.get("questions") or payload.get("items") or []

            if not isinstance(items, list):
                flash("تنسيق JSON غير مدعوم.", "error")
                return redirect(url_for("teacher.edit_test", test_id=test.id))

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
                        opt_image = str(opt_image).strip() if opt_image else None
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
                            opt_image = str(opt_image).strip() if opt_image else None
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
            return redirect(url_for("teacher.edit_test", test_id=test.id))
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

    return render_template("teacher/test_edit.html", form=form, meta_form=form, test=test)


@teacher_bp.route("/tests/<test_id>/delete", methods=["POST"])
@login_required
@role_required("teacher")
def delete_test(test_id):
    test = Test.objects(id=test_id).first()
    if not test:
        raise NotFound()
    section_id = test.section_id.id if test.section_id else None
    lesson_id = test.lesson_id.id if test.lesson_id else None
    test.delete()
    flash("تم حذف الاختبار بنجاح.", "success")
    if lesson_id:
        return redirect(url_for("teacher.lesson_detail", lesson_id=lesson_id))
    return redirect(url_for("teacher.section_detail", section_id=section_id))


@teacher_bp.route("/questions/<question_id>/delete", methods=["POST"])
@login_required
@role_required("teacher")
def delete_question(question_id):
    question = Question.objects(id=question_id).first()
    if not question:
        raise NotFound()
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
