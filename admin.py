from flask import render_template, session, make_response
from weasyprint import HTML
import json
from datetime import datetime, timedelta
from bson import ObjectId
from mongoengine.errors import DoesNotExist

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort
from flask_login import login_required, current_user

from .models import (
    User,
    Subject,
    Section,
    Lesson,
    LessonResource,
    Test,
    Question,
    Choice,
    Attempt,
    AttemptAnswer,
    AttemptTextAnswer,
    CustomTestAttempt,
    CustomTestAnswer,
    SectionActivation,
    ActivationCode,
    LessonActivation,
    LessonActivationCode,
    SubjectActivation,
    SubjectActivationCode,
    StaffSubjectAccess,
    StaffSubjectAccessAudit,
    StaffActivityLog,
    Notification,
    NotificationRecipient,
)
from .permissions import is_admin
from .account_cleanup import delete_user_with_related_data

admin_bp = Blueprint("admin", __name__, url_prefix="/admin", template_folder="templates")

TEMPLATE_TITLES = {
    "note": "ملاحظة",
    "info": "معلومة",
    "success": "نجاح",
    "warning": "تحذير",
    "urgent": "عاجل",
}


ALLOWED_MODELS = {
    "user": User,
    "subject": Subject,
    "section": Section,
    "lesson": Lesson,
    "lesson_resource": LessonResource,
    "test": Test,
    "question": Question,
    "attempt": Attempt,
    "attempt_answer": AttemptAnswer,
    "section_activation": SectionActivation,
    "activation_code": ActivationCode,
    "lesson_activation": LessonActivation,
    "lesson_activation_code": LessonActivationCode,
    "subject_activation": SubjectActivation,
    "subject_activation_code": SubjectActivationCode,
}


def admin_required():
    if not current_user.is_authenticated:
        abort(403)
    if not is_admin(current_user):
        abort(403)


def serialize_instance(obj):
    """Serialize a MongoEngine document to a JSON-compatible dict"""
    data = {}
    for field_name in obj._fields.keys():
        val = getattr(obj, field_name, None)
        if isinstance(val, datetime):
            data[field_name] = val.isoformat()
        elif isinstance(val, ObjectId):
            data[field_name] = str(val)
        else:
            data[field_name] = val
    return data


def apply_payload(obj, payload):
    """Apply payload dict to MongoEngine document fields"""
    for field_name, field_obj in obj._fields.items():
        if field_name not in payload:
            continue
        if field_name == "id":  # Skip primary key
            continue
        raw_val = payload[field_name]
        if raw_val is None:
            setattr(obj, field_name, None)
            continue
        # Basic type handling
        field_type = field_obj.__class__.__name__
        if field_type == "BooleanField":
            if isinstance(raw_val, str):
                raw_val = raw_val.lower() in {"true", "1", "yes", "on"}
            else:
                raw_val = bool(raw_val)
        elif field_type == "IntField":
            raw_val = int(raw_val)
        elif field_type == "FloatField":
            raw_val = float(raw_val)
        elif field_type == "DateTimeField":
            if isinstance(raw_val, str):
                raw_val = datetime.fromisoformat(raw_val)
        setattr(obj, field_name, raw_val)

@admin_bp.route("/")
@login_required
def dashboard():
    admin_required()
    counts = {}
    for name, model in ALLOWED_MODELS.items():
        try:
            if not hasattr(model, "objects"):
                counts[name] = "-"
                continue
            counts[name] = model.objects().count()
        except Exception:
            counts[name] = "?"

    subjects = list(Subject.objects().order_by("created_at").all())
    sections_count = {}
    if subjects:
        subject_ids = [s.id for s in subjects]
        for section in Section.objects(subject_id__in=subject_ids).only("subject_id").all():
            sid = section.subject_id.id if section.subject_id else None
            if sid:
                sections_count[sid] = sections_count.get(sid, 0) + 1

    for subject in subjects:
        subject._sections_count = sections_count.get(subject.id, 0)

    return render_template("admin/dashboard.html", counts=counts, subjects=subjects)


@admin_bp.route("/notifications", methods=["GET", "POST"])
@login_required
def notifications_manage():
    admin_required()

    if request.method == "POST":
        title = (request.form.get("title") or "").strip()
        body = (request.form.get("body") or "").strip()
        template_type = (request.form.get("template_type") or "note").strip().lower()
        audience = (request.form.get("audience") or "").strip().lower()

        if audience not in {"all", "students", "staff", "specific"}:
            flash("فئة الاستهداف غير صالحة.", "error")
            return redirect(url_for("admin.notifications_manage"))
        if template_type not in {"note", "info", "success", "warning", "urgent"}:
            flash("قالب الإشعار غير صالح.", "error")
            return redirect(url_for("admin.notifications_manage"))
        if not title:
            title = TEMPLATE_TITLES.get(template_type, TEMPLATE_TITLES["note"])
        if not body:
            flash("نص الإشعار مطلوب.", "error")
            return redirect(url_for("admin.notifications_manage"))

        if audience == "all":
            recipients = list(User.objects().only("id").all())
        elif audience == "students":
            recipients = list(User.objects(role="student").only("id").all())
        elif audience == "staff":
            recipients = list(User.objects(role__in=["admin", "teacher", "question_editor"]).only("id").all())
        else:
            selected_ids = [sid for sid in request.form.getlist("specific_user_ids") if ObjectId.is_valid(sid)]
            if not selected_ids:
                flash("اختر مستخدماً واحداً على الأقل عند الاستهداف المحدد.", "error")
                return redirect(url_for("admin.notifications_manage"))
            recipients = list(User.objects(id__in=[ObjectId(sid) for sid in selected_ids]).only("id").all())

        unique_recipient_ids = list({u.id for u in recipients})
        if not unique_recipient_ids:
            flash("لا يوجد مستلمون لهذه الرسالة.", "warning")
            return redirect(url_for("admin.notifications_manage"))

        notification = Notification(
            title=title,
            body=body,
            template_type=template_type,
            audience=audience,
            created_by=current_user.id,
        )
        notification.save()

        for uid in unique_recipient_ids:
            NotificationRecipient(
                notification_id=notification.id,
                user_id=uid,
                is_read=False,
            ).save()

        flash(f"تم إرسال الإشعار إلى {len(unique_recipient_ids)} مستخدم.", "success")
        return redirect(url_for("admin.notifications_manage"))

    users = list(User.objects().order_by("first_name", "last_name", "username").all())
    recent_notifications = list(Notification.objects().order_by("-created_at").limit(50).all())
    recipient_counts = {}
    if recent_notifications:
        nids = [n.id for n in recent_notifications]
        for row in NotificationRecipient.objects(notification_id__in=nids).only("notification_id").all():
            nid = row.notification_id.id if row.notification_id else None
            if nid:
                recipient_counts[nid] = recipient_counts.get(nid, 0) + 1

    return render_template(
        "admin/notifications_manage.html",
        users=users,
        recent_notifications=recent_notifications,
        recipient_counts=recipient_counts,
    )


@admin_bp.route("/notifications/<notification_id>/delete", methods=["POST"])
@login_required
def notifications_delete(notification_id):
    admin_required()

    row = Notification.objects(id=notification_id).first() if ObjectId.is_valid(notification_id) else None
    if not row:
        flash("الإشعار غير موجود.", "error")
        return redirect(url_for("admin.notifications_manage"))

    NotificationRecipient.objects(notification_id=row.id).delete()
    row.delete()
    flash("تم حذف الإشعار.", "success")
    return redirect(url_for("admin.notifications_manage"))


@admin_bp.route("/notifications/delete-old", methods=["POST"])
@login_required
def notifications_delete_old():
    admin_required()

    days_raw = (request.form.get("days") or "30").strip()
    try:
        days = max(1, int(days_raw))
    except Exception:
        flash("عدد الأيام غير صالح.", "error")
        return redirect(url_for("admin.notifications_manage"))

    cutoff = datetime.utcnow() - timedelta(days=days)

    def _effective_created_at(notification_row):
        # Backward compatibility: legacy rows may miss created_at.
        dt = notification_row.created_at
        if not dt and notification_row.id:
            dt = notification_row.id.generation_time
        if dt and getattr(dt, "tzinfo", None) is not None:
            dt = dt.replace(tzinfo=None)
        return dt

    old_ids = []
    for row in Notification.objects().only("id", "created_at").all():
        created_at = _effective_created_at(row)
        if created_at and created_at < cutoff:
            old_ids.append(row.id)

    if not old_ids:
        flash("لا توجد إشعارات قديمة ضمن المدة المحددة.", "info")
        return redirect(url_for("admin.notifications_manage"))

    NotificationRecipient.objects(notification_id__in=old_ids).delete()
    deleted_count = Notification.objects(id__in=old_ids).delete()
    flash(f"تم حذف {deleted_count} إشعار قديم.", "success")
    return redirect(url_for("admin.notifications_manage"))


@admin_bp.route("/staff", methods=["GET"])
@login_required
def staff_accounts():
    admin_required()

    subjects = list(Subject.objects().order_by("created_at").all())
    staff_users = list(User.objects(role__in=["admin", "teacher", "question_editor"]).order_by("first_name", "last_name").all())

    access_rows = list(StaffSubjectAccess.objects(active=True, staff_user_id__in=[u.id for u in staff_users]).all()) if staff_users else []
    by_user = {}
    for row in access_rows:
        if not row.staff_user_id or not row.subject_id:
            continue
        by_user.setdefault(row.staff_user_id.id, []).append(row.subject_id)

    return render_template(
        "admin/staff_accounts.html",
        subjects=subjects,
        staff_users=staff_users,
        subjects_by_user=by_user,
    )


@admin_bp.route("/staff/<user_id>/logs", methods=["GET"])
@login_required
def staff_activity_logs(user_id):
    admin_required()

    staff_user = User.objects(id=user_id).first() if ObjectId.is_valid(user_id) else None
    if not staff_user:
        flash("المستخدم غير موجود.", "error")
        return redirect(url_for("admin.staff_accounts"))

    role = (staff_user.role or "").lower()
    if role not in {"admin", "teacher", "question_editor"}:
        flash("السجل متاح فقط لحسابات الطاقم.", "warning")
        return redirect(url_for("admin.staff_accounts"))

    action_filter = (request.args.get("action") or "").strip().lower()
    query = StaffActivityLog.objects(staff_user_id=staff_user.id)
    if action_filter:
        query = query.filter(action__icontains=action_filter)

    logs = list(query.order_by("-created_at").limit(300).all())

    return render_template(
        "admin/staff_activity_logs.html",
        staff_user=staff_user,
        logs=logs,
        action_filter=action_filter,
    )


@admin_bp.route("/staff/create", methods=["POST"])
@login_required
def staff_create_account():
    admin_required()

    role = (request.form.get("role") or "").strip().lower()
    if role not in {"admin", "teacher", "question_editor"}:
        flash("دور غير صالح.", "error")
        return redirect(url_for("admin.staff_accounts"))

    first_name = (request.form.get("first_name") or "").strip()
    last_name = (request.form.get("last_name") or "").strip()
    username = (request.form.get("username") or "").strip()
    phone = (request.form.get("phone") or "").strip()
    password = (request.form.get("password") or "").strip()

    if not all([first_name, last_name, username, phone, password]):
        flash("يرجى تعبئة جميع الحقول.", "error")
        return redirect(url_for("admin.staff_accounts"))

    if User.objects(username=username).first() or User.objects(phone=phone).first():
        flash("اسم المستخدم أو رقم الهاتف مستخدم مسبقاً.", "error")
        return redirect(url_for("admin.staff_accounts"))

    user = User(
        first_name=first_name,
        last_name=last_name,
        username=username,
        phone=phone,
        role=role,
    )
    user.set_password(password)
    user.save()

    flash("تم إنشاء الحساب بنجاح.", "success")
    return redirect(url_for("admin.staff_accounts"))


@admin_bp.route("/staff/<user_id>/subjects", methods=["POST"])
@login_required
def staff_assign_subjects(user_id):
    admin_required()

    staff_user = User.objects(id=user_id).first() if ObjectId.is_valid(user_id) else None
    if not staff_user:
        flash("المستخدم غير موجود.", "error")
        return redirect(url_for("admin.staff_accounts"))

    role = (staff_user.role or "").lower()
    if role not in {"teacher", "question_editor"}:
        flash("تخصيص المواد متاح فقط للمعلم أو محرر الأسئلة.", "warning")
        return redirect(url_for("admin.staff_accounts"))

    selected_ids = [sid for sid in request.form.getlist("subject_ids") if ObjectId.is_valid(sid)]
    selected_obj_ids = [ObjectId(sid) for sid in selected_ids]

    existing_rows = list(StaffSubjectAccess.objects(staff_user_id=staff_user.id).all())
    before_ids = [row.subject_id.id for row in existing_rows if row.subject_id and row.active]

    for row in existing_rows:
        should_be_active = bool(row.subject_id and row.subject_id.id in selected_obj_ids)
        if row.active != should_be_active:
            row.active = should_be_active
            row.updated_at = datetime.utcnow()
            row.save()

    existing_subject_ids = {row.subject_id.id for row in existing_rows if row.subject_id}
    for sid in selected_obj_ids:
        if sid in existing_subject_ids:
            continue
        StaffSubjectAccess(
            staff_user_id=staff_user.id,
            subject_id=sid,
            assigned_by=current_user.id,
            active=True,
            assigned_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        ).save()

    after_ids = [sid for sid in selected_obj_ids]
    StaffSubjectAccessAudit(
        staff_user_id=staff_user.id,
        changed_by=current_user.id,
        before_subject_ids=before_ids,
        after_subject_ids=after_ids,
        note="admin_subject_assignment_update",
    ).save()

    flash("تم تحديث المواد المخصصة للمستخدم.", "success")
    return redirect(url_for("admin.staff_accounts"))


@admin_bp.route("/staff/migrate-legacy-teachers", methods=["POST"])
@login_required
def migrate_legacy_teachers_to_admin():
    admin_required()

    legacy_rows = list(User.objects(role="teacher").all())
    migrated = 0
    skipped = 0
    for user in legacy_rows:
        has_scope = bool(StaffSubjectAccess.objects(staff_user_id=user.id, active=True).first())
        if has_scope:
            skipped += 1
            continue
        user.role = "admin"
        user.save()
        migrated += 1

    flash(f"تم تحويل {migrated} حساباً من teacher إلى admin. تم تخطي {skipped} حسابات لديها تخصيص مواد.", "info")
    return redirect(url_for("admin.staff_accounts"))


@admin_bp.route("/users/<user_id>/delete", methods=["POST"])
@login_required
def delete_user_account(user_id):
    admin_required()

    user = User.objects(id=user_id).first() if ObjectId.is_valid(user_id) else None
    if not user:
        flash("الحساب غير موجود.", "error")
        return redirect(url_for("admin.staff_accounts"))

    if str(user.id) == str(current_user.id):
        flash("لا يمكنك حذف حسابك الحالي.", "error")
        return redirect(url_for("admin.staff_accounts"))

    role = (user.role or "").lower()
    if role == "admin":
        other_admin_exists = User.objects(role="admin", id__ne=user.id).first()
        if not other_admin_exists:
            flash("لا يمكن حذف آخر حساب admin.", "error")
            return redirect(url_for("admin.staff_accounts"))

    delete_user_with_related_data(user)
    flash("تم حذف الحساب وجميع بياناته المرتبطة.", "success")
    return redirect(url_for("admin.staff_accounts"))


@admin_bp.route("/results")
@login_required
def results_manage():
    admin_required()

    selected_student_id = (request.args.get("student_id") or "").strip()
    selected_student = None
    if selected_student_id and ObjectId.is_valid(selected_student_id):
        selected_student = User.objects(id=selected_student_id, role="student").first()

    students = list(User.objects(role="student").order_by("first_name", "last_name").all())

    regular_q = Attempt.objects().order_by("-started_at")
    custom_q = CustomTestAttempt.objects(status="submitted").order_by("-created_at")

    if selected_student:
        regular_q = regular_q.filter(student_id=selected_student.id)
        custom_q = custom_q.filter(student_id=selected_student.id)

    regular_attempts = list(regular_q.limit(300).all())
    custom_attempts = list(custom_q.limit(300).all())

    text_answers = list(
        AttemptTextAnswer.objects(attempt_id__in=[a.id for a in regular_attempts]).all()
    ) if regular_attempts else []
    text_by_attempt = {}
    for ta in text_answers:
        if not ta.attempt_id:
            continue
        aid = ta.attempt_id.id
        text_by_attempt.setdefault(aid, []).append(ta)

    for attempt in regular_attempts:
        tas = text_by_attempt.get(attempt.id, [])
        pending = bool(tas) and any(getattr(ta, "score_awarded", None) is None for ta in tas)
        attempt._pending_text_grading = pending

    return render_template(
        "admin/results.html",
        students=students,
        selected_student=selected_student,
        regular_attempts=regular_attempts,
        custom_attempts=custom_attempts,
    )


@admin_bp.route("/results/<attempt_id>/delete", methods=["POST"])
@login_required
def delete_regular_result(attempt_id):
    admin_required()
    attempt = Attempt.objects(id=attempt_id).first() if ObjectId.is_valid(attempt_id) else None
    if not attempt:
        flash("محاولة الاختبار غير موجودة.", "error")
        return redirect(url_for("admin.results_manage"))

    AttemptAnswer.objects(attempt_id=attempt.id).delete()
    AttemptTextAnswer.objects(attempt_id=attempt.id).delete()
    attempt.delete()
    flash("تم حذف نتيجة الاختبار العادي.", "success")
    student_id = (request.form.get("student_id") or "").strip()
    return redirect(url_for("admin.results_manage", student_id=student_id) if student_id else url_for("admin.results_manage"))


@admin_bp.route("/custom-results/<attempt_id>/delete", methods=["POST"])
@login_required
def delete_custom_result(attempt_id):
    admin_required()
    attempt = CustomTestAttempt.objects(id=attempt_id).first() if ObjectId.is_valid(attempt_id) else None
    if not attempt:
        flash("محاولة الاختبار المخصص غير موجودة.", "error")
        return redirect(url_for("admin.results_manage"))

    CustomTestAnswer.objects(attempt_id=attempt.id).delete()
    attempt.delete()
    flash("تم حذف نتيجة الاختبار المخصص.", "success")
    student_id = (request.form.get("student_id") or "").strip()
    return redirect(url_for("admin.results_manage", student_id=student_id) if student_id else url_for("admin.results_manage"))


@admin_bp.route("/table/<table_name>")
@login_required
def table_view(table_name):
    admin_required()
    model = ALLOWED_MODELS.get(table_name)
    if not model or not hasattr(model, "objects"):
        abort(404)
    rows = model.objects().limit(100).all()
    # Get field names from model
    columns = list(model._fields.keys()) if hasattr(model, '_fields') else []

    def _safe_cell_value(row, col):
        try:
            val = getattr(row, col, None)
        except DoesNotExist:
            return "[مرجع مفقود]"
        except Exception:
            return "[خطأ في القراءة]"

        if isinstance(val, datetime):
            return val.strftime("%Y-%m-%d %H:%M:%S")
        if isinstance(val, ObjectId):
            return str(val)
        if isinstance(val, list):
            return ", ".join(str(v) for v in val)
        if isinstance(val, dict):
            return json.dumps(val, ensure_ascii=False)
        if hasattr(val, "id"):
            try:
                label = getattr(val, "username", None) or getattr(val, "title", None) or getattr(val, "name", None)
                if label:
                    return f"{label} ({val.id})"
                return str(val.id)
            except Exception:
                return "[مرجع غير صالح]"
        return str(val) if val is not None else ""

    safe_rows = []
    for row in rows:
        safe_rows.append(
            {
                "id": row.id,
                "cells": {col: _safe_cell_value(row, col) for col in columns},
            }
        )

    return render_template(
        "admin/table.html",
        table_name=table_name,
        columns=columns,
        rows=safe_rows,
    )


@admin_bp.route("/table/<table_name>/new", methods=["GET", "POST"])
@login_required
def table_new(table_name):
    admin_required()
    model = ALLOWED_MODELS.get(table_name)
    if not model or not hasattr(model, "objects"):
        abort(404)
    if request.method == "POST":
        payload_raw = request.form.get("payload", "{}")
        try:
            payload = json.loads(payload_raw)
            if not isinstance(payload, dict):
                raise ValueError("Payload must be a JSON object")
        except Exception as exc:
            flash(f"JSON غير صحيح: {exc}", "error")
            return redirect(request.url)
        obj = model()
        apply_payload(obj, payload)
        obj.save()
        flash(f"تم إنشاء سجل {table_name}", "success")
        return redirect(url_for("admin.table_view", table_name=table_name))
    example = json.dumps({"field": "value"}, indent=2)
    return render_template("admin/edit.html", table_name=table_name, payload=example, is_new=True)


@admin_bp.route("/table/<table_name>/<row_id>/edit", methods=["GET", "POST"])
@login_required
def table_edit(table_name, row_id):
    admin_required()
    model = ALLOWED_MODELS.get(table_name)
    if not model or not hasattr(model, "objects"):
        abort(404)
    obj = model.objects(id=ObjectId(row_id)).first()
    if not obj:
        abort(404)
    if request.method == "POST":
        payload_raw = request.form.get("payload", "{}")
        try:
            payload = json.loads(payload_raw)
            if not isinstance(payload, dict):
                raise ValueError("Payload must be a JSON object")
        except Exception as exc:
            flash(f"JSON غير صحيح: {exc}", "error")
            return redirect(request.url)
        apply_payload(obj, payload)
        obj.save()
        flash(f"تم تحديث سجل {table_name} {row_id}", "success")
        return redirect(url_for("admin.table_view", table_name=table_name))
    payload = json.dumps(serialize_instance(obj), indent=2, default=str)
    return render_template("admin/edit.html", table_name=table_name, payload=payload, is_new=False, row_id=row_id)


@admin_bp.route("/table/<table_name>/<row_id>/delete", methods=["POST"])
@login_required
def table_delete(table_name, row_id):
    admin_required()
    model = ALLOWED_MODELS.get(table_name)
    if not model or not hasattr(model, "objects"):
        abort(404)
    obj = model.objects(id=ObjectId(row_id)).first()
    if not obj:
        abort(404)
    obj.delete()
    flash(f"تم حذف سجل {table_name} {row_id}", "info")
    return redirect(url_for("admin.table_view", table_name=table_name))

