from flask import Blueprint, render_template, redirect, url_for, flash, request
import json
from flask_login import login_required, current_user

from .extensions import db
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

teacher_bp = Blueprint("teacher", __name__, template_folder="templates")

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
def dashboard():
    subjects = Subject.query.filter_by(created_by=current_user.id).all()
    # Precompute first freebies per section for accurate status display
    sections_meta = {}
    for subj in subjects:
        for sec in subj.sections:
            first_lesson_id = min([l.id for l in sec.lessons], default=None)
            first_section_test_id = min([t.id for t in sec.tests if t.lesson_id is None], default=None)
            sections_meta[sec.id] = {
                "first_lesson_id": first_lesson_id,
                "first_section_test_id": first_section_test_id,
            }
    return render_template("teacher/dashboard.html", subjects=subjects, sections_meta=sections_meta)


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
    attempts = (
        Attempt.query
        .order_by(Attempt.started_at.desc())
        .all()
    )
    return render_template("teacher/results.html", attempts=attempts)


@teacher_bp.route("/students/<int:student_id>/results")
@login_required
@role_required("teacher")
def student_results(student_id):
    student = User.query.get_or_404(student_id)
    attempts = Attempt.query.filter_by(student_id=student.id).order_by(Attempt.started_at.desc()).all()
    return render_template("teacher/student_results.html", student=student, attempts=attempts)


@teacher_bp.route("/attempts/<int:attempt_id>", methods=["GET", "POST"])
@login_required
@role_required("teacher")
def manage_attempt(attempt_id):
    attempt = Attempt.query.get_or_404(attempt_id)
    student = attempt.student
    test = attempt.test
    questions = test.questions

    if request.method == "POST":
        action = request.form.get("action")
        if action == "delete":
            # Delete answers then attempt
            AttemptAnswer.query.filter_by(attempt_id=attempt.id).delete()
            db.session.delete(attempt)
            db.session.commit()
            flash("تم حذف المحاولة", "info")
            return redirect(url_for("teacher.student_results", student_id=student.id))

        # Update answers
        total = len(questions)
        score = 0
        for q in questions:
            choice_id_val = request.form.get(f"question_{q.id}")
            choice = Choice.query.get(int(choice_id_val)) if choice_id_val else None
            is_correct = bool(choice and choice.is_correct)
            ans = AttemptAnswer.query.filter_by(attempt_id=attempt.id, question_id=q.id).first()
            if not ans:
                ans = AttemptAnswer(attempt_id=attempt.id, question_id=q.id)
                db.session.add(ans)
            ans.choice_id = choice.id if choice else None
            ans.is_correct = is_correct
            if is_correct:
                score += 1
        attempt.score = score
        attempt.total = total
        db.session.commit()
        flash("Attempt updated", "success")
        return redirect(url_for("teacher.manage_attempt", attempt_id=attempt.id))

    answers = {a.question_id: a for a in attempt.answers}
    return render_template("teacher/attempt_manage.html", attempt=attempt, student=student, test=test, questions=questions, answers=answers)


@teacher_bp.route("/students")
@login_required
@role_required("teacher")
def students():
    students = User.query.filter_by(role="student").order_by(User.created_at.desc()).all()
    return render_template("teacher/students.html", students=students)


@teacher_bp.route("/students/<int:user_id>/edit", methods=["GET", "POST"])
@login_required
@role_required("teacher")
def edit_student(user_id):
    student = User.query.get_or_404(user_id)
    if student.role != "student":
        flash("يمكن تعديل حسابات الطلاب فقط هنا.", "error")
        return redirect(url_for("teacher.students"))

    form = StudentEditForm(obj=student)
    if form.validate_on_submit():
        student.username = form.username.data
        student.email = form.email.data
        student.role = form.role.data
        if form.password.data:
            student.set_password(form.password.data)
        db.session.commit()
        flash("تم تحديث الطالب", "success")
        return redirect(url_for("teacher.students"))

    return render_template("teacher/student_form.html", form=form, student=student)


@teacher_bp.route("/subjects/<int:subject_id>/access", methods=["GET", "POST"])
@login_required
@role_required("teacher")
def manage_subject_access(subject_id):
    subject = Subject.query.get_or_404(subject_id)
    if subject.created_by != current_user.id:
        flash("غير مسموح", "error")
        return redirect(url_for("teacher.dashboard"))

    if request.method == "POST":
        action = request.form.get("action")
        student_id = int(request.form.get("student_id"))
        student = User.query.get_or_404(student_id)

        if action == "generate_code":
            # Allow only one unused code at a time
            existing_unused = SubjectActivationCode.query.filter_by(
                subject_id=subject.id, student_id=student.id, is_used=False
            ).first()
            if existing_unused:
                flash("الطالب لديه رمز غير مستخدم لهذه المادة. قم بإزالته أو استخدامه قبل إنشاء آخر.", "error")
                return redirect(url_for("teacher.manage_subject_access", subject_id=subject.id))
            import random, string
            def gen():
                return ''.join(random.choice(string.ascii_uppercase + string.digits) for _ in range(6))
            code_value = gen()
            while SubjectActivationCode.query.filter_by(code=code_value).first():
                code_value = gen()
            ac = SubjectActivationCode(subject_id=subject.id, student_id=student.id, code=code_value)
            db.session.add(ac)
            db.session.commit()
            flash(f"تم إنشاء رمز لـ {student.username}: {code_value}", "success")
        elif action == "activate":
            existing = SubjectActivation.query.filter_by(
                subject_id=subject.id, student_id=student.id, active=True
            ).first()
            if not existing:
                db.session.add(SubjectActivation(subject_id=subject.id, student_id=student.id))
            cascade_subject_activation(subject, student.id)
            db.session.commit()
            flash(f"تم تفعيل المادة لـ {student.username}", "success")
        elif action == "revoke":
            revoke_subject_activation(subject, student.id)
            db.session.commit()
            flash(f"تم إلغاء الوصول لـ {student.username}", "info")
        elif action == "delete_code":
            code_id = int(request.form.get("code_id", 0))
            ac = SubjectActivationCode.query.get_or_404(code_id)
            if ac.subject_id != subject.id or ac.student_id != student.id:
                flash("رمز غير صحيح", "error")
            else:
                db.session.delete(ac)
                db.session.commit()
                flash("تم إزالة الرمز", "info")
        return redirect(url_for("teacher.manage_subject_access", subject_id=subject.id))

    students = User.query.filter_by(role="student").order_by(User.created_at.desc()).all()
    activations = {
        sa.student_id: sa 
        for sa in SubjectActivation.query.filter_by(subject_id=subject.id, active=True).all()
    }
    codes_by_student = {}
    for ac in SubjectActivationCode.query.filter_by(subject_id=subject.id).order_by(
        SubjectActivationCode.created_at.desc()
    ).all():
        lst = codes_by_student.setdefault(ac.student_id, [])
        lst.append(ac)
    return render_template(
        "teacher/subject_access.html", 
        subject=subject, 
        students=students, 
        activations=activations, 
        codes_by_student=codes_by_student
    )


@teacher_bp.route("/sections/<int:section_id>/access", methods=["GET", "POST"])
@login_required
@role_required("teacher")
def manage_section_access(section_id):
    section = Section.query.get_or_404(section_id)
    subject = section.subject
    if subject.created_by != current_user.id:
        flash("Not allowed", "error")
        return redirect(url_for("teacher.dashboard"))

    # Generate code for a specific student
    if request.method == "POST":
        action = request.form.get("action")

        if action == "toggle_requires_code":
            section.requires_code = not section.requires_code
            # If we just locked the section, clear activations so only freebies stay open
            if section.requires_code:
                lock_section_access_for_all(section)
            db.session.commit()
            flash("تم تحديث متطلبات تفعيل القسم", "success")
            return redirect(url_for("teacher.manage_section_access", section_id=section.id))

        student_id = int(request.form.get("student_id"))
        student = User.query.get_or_404(student_id)

        if action == "generate_code":
            # Allow only one unused code at a time
            existing_unused = ActivationCode.query.filter_by(section_id=section.id, student_id=student.id, is_used=False).first()
            if existing_unused:
                flash("Student already has an unused code for this section. Remove it or use it before generating another.", "error")
                return redirect(url_for("teacher.manage_section_access", section_id=section.id))
            import random, string
            # Generate unique 6-char alphanumeric code
            def gen():
                return ''.join(random.choice(string.ascii_uppercase + string.digits) for _ in range(6))
            code_value = gen()
            while ActivationCode.query.filter_by(code=code_value).first():
                code_value = gen()
            ac = ActivationCode(section_id=section.id, student_id=student.id, code=code_value)
            db.session.add(ac)
            db.session.commit()
            flash(f"Code generated for {student.username}: {code_value}", "success")
        elif action == "activate":
            existing = SectionActivation.query.filter_by(section_id=section.id, student_id=student.id, active=True).first()
            if not existing:
                db.session.add(SectionActivation(section_id=section.id, student_id=student.id))
            cascade_section_activation(section, student.id)
            db.session.commit()
            flash(f"Section activated for {student.username}", "success")
        elif action == "revoke":
            revoke_section_activation(section, student.id)
            db.session.commit()
            flash(f"تم إلغاء الوصول لـ {student.username}", "info")
        elif action == "delete_code":
            code_id = int(request.form.get("code_id", 0))
            ac = ActivationCode.query.get_or_404(code_id)
            if ac.section_id != section.id or ac.student_id != student.id:
                flash("رمز غير صحيح", "error")
            else:
                db.session.delete(ac)
                db.session.commit()
                flash("تم إزالة الرمز", "info")
        return redirect(url_for("teacher.manage_section_access", section_id=section.id))

    students = User.query.filter_by(role="student").order_by(User.created_at.desc()).all()
    activations = { (sa.student_id): sa for sa in SectionActivation.query.filter_by(section_id=section.id, active=True).all() }
    codes_by_student = {}
    for ac in ActivationCode.query.filter_by(section_id=section.id).order_by(ActivationCode.created_at.desc()).all():
        lst = codes_by_student.setdefault(ac.student_id, [])
        lst.append(ac)
    return render_template("teacher/section_access.html", section=section, students=students, activations=activations, codes_by_student=codes_by_student)

@teacher_bp.route("/subjects/new", methods=["GET", "POST"])
@login_required
@role_required("teacher")
def new_subject():
    form = SubjectForm()
    if form.validate_on_submit():
        subject = Subject(name=form.name.data, description=form.description.data, created_by=current_user.id)
        db.session.add(subject)
        db.session.commit()
        flash("تم إنشاء المادة", "success")
        return redirect(url_for("teacher.dashboard"))
    return render_template("teacher/subject_form.html", form=form)

@teacher_bp.route("/subjects/<int:subject_id>/edit", methods=["GET", "POST"])
@login_required
@role_required("teacher")
def edit_subject(subject_id):
    subject = Subject.query.get_or_404(subject_id)
    if subject.created_by != current_user.id:
        flash("غير مسموح", "error")
        return redirect(url_for("teacher.dashboard"))
    form = SubjectForm(obj=subject)
    if form.validate_on_submit():
        subject.name = form.name.data
        subject.description = form.description.data
        db.session.commit()
        flash("تم تحديث المادة", "success")
        return redirect(url_for("teacher.dashboard"))
    return render_template("teacher/subject_form.html", form=form)

@teacher_bp.route("/subjects/<int:subject_id>/delete", methods=["POST"]) 
@login_required
@role_required("teacher")
def delete_subject(subject_id):
    subject = Subject.query.get_or_404(subject_id)
    if subject.created_by != current_user.id:
        flash("غير مسموح", "error")
        return redirect(url_for("teacher.dashboard"))
    db.session.delete(subject)
    db.session.commit()
    flash("تم حذف المادة", "info")
    return redirect(url_for("teacher.dashboard"))

@teacher_bp.route("/subjects/<int:subject_id>/sections/new", methods=["GET", "POST"])
@login_required
@role_required("teacher")
def new_section(subject_id):
    subject = Subject.query.get_or_404(subject_id)
    if subject.created_by != current_user.id:
        flash("غير مسموح", "error")
        return redirect(url_for("teacher.dashboard"))
    form = SectionForm()
    if form.validate_on_submit():
        section = Section(
            subject_id=subject.id,
            title=form.title.data,
            description=form.description.data,
            requires_code=False,
        )
        db.session.add(section)
        db.session.commit()
        flash("تم إنشاء القسم", "success")
        return redirect(url_for("teacher.dashboard"))
    return render_template("teacher/section_form.html", form=form, subject=subject)


@teacher_bp.route("/sections/<int:section_id>/edit", methods=["GET", "POST"])
@login_required
@role_required("teacher")
def edit_section(section_id):
    section = Section.query.get_or_404(section_id)
    subject = section.subject
    if subject.created_by != current_user.id:
        flash("غير مسموح", "error")
        return redirect(url_for("teacher.dashboard"))
    form = SectionForm(obj=section)
    if form.validate_on_submit():
        section.title = form.title.data
        section.description = form.description.data
        db.session.commit()
        flash("تم تحديث القسم", "success")
        return redirect(url_for("teacher.dashboard"))
    return render_template("teacher/section_form.html", form=form, subject=subject)

@teacher_bp.route("/sections/<int:section_id>/lessons/new", methods=["GET", "POST"])
@login_required
@role_required("teacher")
def new_lesson(section_id):
    section = Section.query.get_or_404(section_id)
    subject = section.subject
    if subject.created_by != current_user.id:
        flash("غير مسموح", "error")
        return redirect(url_for("teacher.dashboard"))
    form = LessonForm()
    if form.validate_on_submit():
        resource_labels = request.form.getlist("resource_label[]")
        resource_urls = request.form.getlist("resource_url[]")
        resource_types = request.form.getlist("resource_type[]")
        resources = [
            (lbl.strip(), url.strip(), rtype.strip().lower() if rtype else None)
            for lbl, url, rtype in zip(resource_labels, resource_urls, resource_types)
            if lbl.strip() and url.strip()
        ]
        # First lesson in a section is open by default
        requires_code = bool(form.requires_code.data)
        if len(section.lessons) == 0:
            requires_code = False
        lesson = Lesson(
            section_id=section.id,
            title=form.title.data,
            content=form.content.data or "",
            requires_code=requires_code,
            link_label=form.link_label.data or None,
            link_url=form.link_url.data or None,
        )
        db.session.add(lesson)
        db.session.flush()
        for idx, (lbl, url, rtype) in enumerate(resources):
            db.session.add(LessonResource(lesson_id=lesson.id, label=lbl, url=url, resource_type=rtype, position=idx))
        db.session.commit()
        flash("تم إنشاء الدرس", "success")
        return redirect(url_for("teacher.dashboard"))
    return render_template("teacher/lesson_form.html", form=form, section=section)

@teacher_bp.route("/lessons/<int:lesson_id>/edit", methods=["GET", "POST"])
@login_required
@role_required("teacher")
def edit_lesson(lesson_id):
    lesson = Lesson.query.get_or_404(lesson_id)
    subject = lesson.section.subject
    if subject.created_by != current_user.id:
        flash("غير مسموح", "error")
        return redirect(url_for("teacher.dashboard"))
    form = LessonForm(obj=lesson)
    if form.validate_on_submit():
        resource_labels = request.form.getlist("resource_label[]")
        resource_urls = request.form.getlist("resource_url[]")
        resource_types = request.form.getlist("resource_type[]")
        resources = [
            (lbl.strip(), url.strip(), rtype.strip().lower() if rtype else None)
            for lbl, url, rtype in zip(resource_labels, resource_urls, resource_types)
            if lbl.strip() and url.strip()
        ]
        lesson.title = form.title.data
        lesson.content = form.content.data or ""
        lesson.requires_code = bool(form.requires_code.data)
        lesson.link_label = form.link_label.data or None
        lesson.link_url = form.link_url.data or None
        lesson.resources.clear()
        for idx, (lbl, url, rtype) in enumerate(resources):
            lesson.resources.append(LessonResource(label=lbl, url=url, resource_type=rtype, position=idx))
        db.session.commit()
        flash("تم تحديث الدرس", "success")
        return redirect(url_for("teacher.dashboard"))
    return render_template("teacher/lesson_form.html", form=form, section=lesson.section, lesson=lesson)


@teacher_bp.route("/lessons/<int:lesson_id>/delete", methods=["POST"])
@login_required
@role_required("teacher")
def delete_lesson(lesson_id):
    lesson = Lesson.query.get_or_404(lesson_id)
    subject = lesson.section.subject
    if subject.created_by != current_user.id:
        flash("غير مسموح", "error")
        return redirect(url_for("teacher.dashboard"))
    
    # Delete associated lesson resources
    LessonResource.query.filter_by(lesson_id=lesson.id).delete()
    
    # Delete associated tests (and their attempts will be cascaded)
    from .models import Test, TestAttempt
    tests = Test.query.filter_by(lesson_id=lesson.id).all()
    for test in tests:
        TestAttempt.query.filter_by(test_id=test.id).delete()
    Test.query.filter_by(lesson_id=lesson.id).delete()
    
    # Delete the lesson
    db.session.delete(lesson)
    db.session.commit()
    flash("تم حذف الدرس بنجاح", "success")
    return redirect(url_for("teacher.dashboard"))

@teacher_bp.route("/tests/<int:test_id>/delete", methods=["POST"])
@login_required
@role_required("teacher")
def delete_test(test_id):
    test = Test.query.get_or_404(test_id)
    subject = test.section.subject
    if subject.created_by != current_user.id:
        flash("غير مسموح", "error")
        return redirect(url_for("teacher.dashboard"))

    db.session.delete(test)
    db.session.commit()
    flash("تم حذف الاختبار بنجاح", "success")
    return redirect(url_for("teacher.dashboard"))



@teacher_bp.route("/lessons/<int:lesson_id>/access", methods=["GET", "POST"])
@login_required
@role_required("teacher")
def manage_lesson_access(lesson_id):
    lesson = Lesson.query.get_or_404(lesson_id)
    section = lesson.section
    subject = section.subject
    if subject.created_by != current_user.id:
        flash("غير مسموح", "error")
        return redirect(url_for("teacher.dashboard"))

    if request.method == "POST":
        action = request.form.get("action")
        student_id = int(request.form.get("student_id"))
        student = User.query.get_or_404(student_id)

        if action == "generate_code":
            import random, string
            def gen():
                return ''.join(random.choice(string.ascii_uppercase + string.digits) for _ in range(6))
            # Allow only one unused code at a time
            from .models import LessonActivationCode
            existing_unused = LessonActivationCode.query.filter_by(lesson_id=lesson.id, student_id=student.id, is_used=False).first()
            if existing_unused:
                flash("الطالب لديه رمز غير مستخدم لهذا الدرس. قم بإزالته أو استخدامه قبل إنشاء آخر.", "error")
                return redirect(url_for("teacher.manage_lesson_access", lesson_id=lesson.id))
            code_value = gen()
            while LessonActivationCode.query.filter_by(code=code_value).first():
                code_value = gen()
            lac = LessonActivationCode(lesson_id=lesson.id, student_id=student.id, code=code_value)
            db.session.add(lac)
            db.session.commit()
            flash(f"تم إنشاء رمز لـ {student.username}: {code_value}", "success")
        elif action == "activate":
            from .models import LessonActivation
            existing = LessonActivation.query.filter_by(lesson_id=lesson.id, student_id=student.id, active=True).first()
            if not existing:
                db.session.add(LessonActivation(lesson_id=lesson.id, student_id=student.id))
                db.session.commit()
            flash(f"تم تفعيل الدرس لـ {student.username}", "success")
        elif action == "revoke":
            from .models import LessonActivation
            existing = LessonActivation.query.filter_by(lesson_id=lesson.id, student_id=student.id, active=True).first()
            if existing:
                existing.active = False
                db.session.commit()
            flash(f"Access revoked for {student.username}", "info")
        elif action == "delete_code":
            from .models import LessonActivationCode
            code_id = int(request.form.get("code_id", 0))
            ac = LessonActivationCode.query.get_or_404(code_id)
            if ac.lesson_id != lesson.id or ac.student_id != student.id:
                flash("رمز غير صحيح", "error")
            else:
                db.session.delete(ac)
                db.session.commit()
                flash("تم إزالة الرمز", "info")
        return redirect(url_for("teacher.manage_lesson_access", lesson_id=lesson.id))

    students = User.query.filter_by(role="student").order_by(User.created_at.desc()).all()
    from .models import LessonActivation, LessonActivationCode
    activations = { sa.student_id: sa for sa in LessonActivation.query.filter_by(lesson_id=lesson.id, active=True).all() }
    codes_by_student = {}
    for ac in LessonActivationCode.query.filter_by(lesson_id=lesson.id).order_by(LessonActivationCode.created_at.desc()).all():
        lst = codes_by_student.setdefault(ac.student_id, [])
        lst.append(ac)
    return render_template("teacher/lesson_access.html", lesson=lesson, section=section, students=students, activations=activations, codes_by_student=codes_by_student)

@teacher_bp.route("/sections/<int:section_id>/tests/new", methods=["GET", "POST"])
@login_required
@role_required("teacher")
def new_test(section_id):
    section = Section.query.get_or_404(section_id)
    subject = section.subject
    if subject.created_by != current_user.id:
        flash("غير مسموح", "error")
        return redirect(url_for("teacher.dashboard"))
    form = TestForm()
    lesson_choices = [(0, "Section-wide test")]
    lesson_choices += [(lesson.id, lesson.title) for lesson in section.lessons]
    form.lesson_id.choices = lesson_choices
    if form.validate_on_submit():
        linked = form.lesson_id.data if form.lesson_id.data else None
        if linked == 0:
            linked = None
        # First section-wide test is open by default
        requires_code = bool(form.requires_code.data)
        if linked is None:
            section_wide_tests = Test.query.filter_by(section_id=section.id, lesson_id=None).all()
            if len(section_wide_tests) == 0:
                requires_code = False
        test = Test(
            section_id=section.id,
            lesson_id=linked,
            title=form.title.data,
            description=form.description.data,
            requires_code=requires_code,
            created_by=current_user.id,
        )
        db.session.add(test)
        db.session.commit()
        flash("تم إنشاء الاختبار", "success")
        return redirect(url_for("teacher.edit_test", test_id=test.id))
    return render_template("teacher/test_form.html", form=form, section=section)

@teacher_bp.route("/tests/<int:test_id>/edit", methods=["GET", "POST"])
@login_required
@role_required("teacher")
def edit_test(test_id):
    test = Test.query.get_or_404(test_id)
    subject = test.section.subject
    if subject.created_by != current_user.id:
        flash("غير مسموح", "error")
        return redirect(url_for("teacher.dashboard"))

    meta_form = TestForm(obj=test)
    lesson_choices = [(0, "Section-wide test")]
    lesson_choices += [(lesson.id, lesson.title) for lesson in test.section.lessons]
    meta_form.lesson_id.choices = lesson_choices
    meta_form.lesson_id.data = test.lesson_id or 0

    if request.method == "POST":
        action = request.form.get("form_name")

        if action == "update_test":
            if meta_form.validate_on_submit():
                linked = meta_form.lesson_id.data if meta_form.lesson_id.data else None
                if linked == 0:
                    linked = None
                requires_code = bool(meta_form.requires_code.data)
                # Preserve the rule: first section-wide test stays open if already the first
                if linked is None:
                    section_wide_tests = Test.query.filter_by(section_id=test.section_id, lesson_id=None).order_by(Test.id).all()
                    if section_wide_tests and section_wide_tests[0].id == test.id:
                        requires_code = False
                test.title = meta_form.title.data
                test.description = meta_form.description.data
                test.lesson_id = linked
                test.requires_code = requires_code
                db.session.commit()
                flash("تم تحديث تفاصيل الاختبار", "success")
                return redirect(url_for("teacher.edit_test", test_id=test.id))
            else:
                flash("يرجى إصلاح الأخطاء في نموذج الاختبار", "error")
                return redirect(url_for("teacher.edit_test", test_id=test.id))

        if action == "upsert_question":
            question_text = request.form.get("question_text", "").strip()
            question_hint = request.form.get("question_hint", "").strip() or None
            choices = [request.form.get(f"choice_{i}", "").strip() for i in range(1, 5)]
            correct_choice = request.form.get("correct_choice")
            question_id = request.form.get("question_id")

            if not question_text:
                flash("نص السؤال مطلوب", "error")
                return redirect(url_for("teacher.edit_test", test_id=test.id))

            filtered = [(idx, text) for idx, text in enumerate(choices, start=1) if text]
            if len(filtered) < 2:
                flash("يرجى توفير خيارين على الأقل للإجابة", "error")
                return redirect(url_for("teacher.edit_test", test_id=test.id))

            if not correct_choice:
                flash("اختر الإجابة الصحيحة", "error")
                return redirect(url_for("teacher.edit_test", test_id=test.id))

            correct_idx = int(correct_choice)
            if correct_idx < 1 or correct_idx > 4 or not choices[correct_idx - 1]:
                flash("يجب أن تتوافق الإجابة الصحيحة مع خيار غير فارغ", "error")
                return redirect(url_for("teacher.edit_test", test_id=test.id))

            if question_id:
                q = Question.query.get_or_404(int(question_id))
                if q.test_id != test.id:
                    flash("سؤال غير صحيح", "error")
                    return redirect(url_for("teacher.edit_test", test_id=test.id))
                q.text = question_text
                q.hint = question_hint

                existing = sorted(q.choices, key=lambda c: c.id)
                non_empty_count = 0
                for idx in range(1, 5):
                    text = choices[idx - 1]
                    is_correct = (idx == correct_idx)
                    if idx <= len(existing):
                        choice_obj = existing[idx - 1]
                        choice_obj.text = text
                        choice_obj.is_correct = is_correct
                        if not text:
                            db.session.delete(choice_obj)
                        else:
                            non_empty_count += 1
                    else:
                        if text:
                            db.session.add(Choice(question_id=q.id, text=text, is_correct=is_correct))
                            non_empty_count += 1

                if non_empty_count < 2:
                    db.session.rollback()
                    flash("يرجى الاحتفاظ بخيارين غير فارغين على الأقل", "error")
                    return redirect(url_for("teacher.edit_test", test_id=test.id))

                db.session.commit()
                flash("تم تحديث السؤال", "success")
            else:
                q = Question(test_id=test.id, text=question_text, hint=question_hint)
                db.session.add(q)
                db.session.flush()
                for idx, text in filtered:
                    is_correct = (idx == correct_idx)
                    db.session.add(Choice(question_id=q.id, text=text, is_correct=is_correct))
                db.session.commit()
                flash("تمت إضافة السؤال", "success")
            return redirect(url_for("teacher.edit_test", test_id=test.id))

        if action == "delete_question":
            q_id = int(request.form.get("question_id", 0))
            q = Question.query.get_or_404(q_id)
            if q.test_id != test.id:
                flash("سؤال غير صحيح", "error")
                return redirect(url_for("teacher.edit_test", test_id=test.id))
            db.session.delete(q)
            db.session.commit()
            flash("تم حذف السؤال", "info")
            return redirect(url_for("teacher.edit_test", test_id=test.id))

        if action == "import_json":
            payload = None
            uploaded = request.files.get("questions_file")
            raw_text = request.form.get("questions_json", "").strip()
            include_hints = request.form.get("include_hints") == "1"

            try:
                if uploaded and uploaded.filename:
                    payload = json.load(uploaded)
                elif raw_text:
                    payload = json.loads(raw_text)
                else:
                    flash("يرجى تحميل ملف JSON أو لصق محتوى JSON.", "error")
                    return redirect(url_for("teacher.edit_test", test_id=test.id))
            except json.JSONDecodeError:
                flash("تنسيق JSON غير صحيح.", "error")
                return redirect(url_for("teacher.edit_test", test_id=test.id))

            items = payload.get("quiz") if isinstance(payload, dict) else None
            if not items or not isinstance(items, list):
                flash("يجب أن يتضمن JSON مصفوفة 'quiz'.", "error")
                return redirect(url_for("teacher.edit_test", test_id=test.id))

            added = 0
            skipped = 0
            for item in items:
                question_text = (item.get("question") or "").strip()
                if not question_text:
                    skipped += 1
                    continue
                answer_options = item.get("answerOptions") or []
                if not isinstance(answer_options, list) or len(answer_options) < 2:
                    skipped += 1
                    continue

                options = answer_options[:4]
                correct_index = None
                for idx, opt in enumerate(options):
                    if opt.get("isCorrect"):
                        correct_index = idx
                        break
                if correct_index is None:
                    skipped += 1
                    continue

                q_hint = (item.get("hint") or "").strip() if include_hints else None
                q = Question(test_id=test.id, text=question_text, hint=q_hint or None)
                db.session.add(q)
                db.session.flush()

                for idx, opt in enumerate(options):
                    text = (opt.get("text") or "").strip()
                    if not text:
                        continue
                    is_correct = idx == correct_index
                    db.session.add(Choice(question_id=q.id, text=text, is_correct=is_correct))
                added += 1

            db.session.commit()
            flash(f"تم استيراد {added} أسئلة. تم تخطي {skipped}.", "success")
            return redirect(url_for("teacher.edit_test", test_id=test.id))

    return render_template("teacher/test_edit.html", test=test, meta_form=meta_form)
