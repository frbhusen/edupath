import json
from datetime import datetime
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
)

admin_bp = Blueprint("admin", __name__, url_prefix="/admin", template_folder="templates")


ALLOWED_MODELS = {
    "user": User,
    "subject": Subject,
    "section": Section,
    "lesson": Lesson,
    "lesson_resource": LessonResource,
    "test": Test,
    "question": Question,
    "choice": Choice,
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
    # Allow teacher role to access admin editor (admin role disabled for now)
    if not current_user.is_authenticated:
        abort(403)
    role = (current_user.role or "").lower()
    if role not in {"teacher", "admin"}:
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
            counts[name] = model.objects().count()
        except Exception:
            counts[name] = "?"
    return render_template("admin/dashboard.html", counts=counts)


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
    if not model:
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
    if not model:
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
    if not model:
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
    if not model:
        abort(404)
    obj = model.objects(id=ObjectId(row_id)).first()
    if not obj:
        abort(404)
    obj.delete()
    flash(f"تم حذف سجل {table_name} {row_id}", "info")
    return redirect(url_for("admin.table_view", table_name=table_name))

