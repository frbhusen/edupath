import json
from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort
from flask_login import login_required, current_user

from .extensions import db
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
    data = {}
    for col in obj.__table__.columns:
        val = getattr(obj, col.key)
        if isinstance(val, datetime):
            data[col.key] = val.isoformat()
        else:
            data[col.key] = val
    return data


def apply_payload(obj, payload):
    for col in obj.__table__.columns:
        if col.primary_key:
            continue
        if col.key not in payload:
            continue
        raw_val = payload[col.key]
        if raw_val is None:
            setattr(obj, col.key, None)
            continue
        # Basic type handling
        if col.type.python_type is bool:
            if isinstance(raw_val, str):
                raw_val = raw_val.lower() in {"true", "1", "yes", "on"}
            else:
                raw_val = bool(raw_val)
        elif col.type.python_type is int:
            raw_val = int(raw_val)
        elif col.type.python_type is float:
            raw_val = float(raw_val)
        elif col.type.python_type is datetime:
            if isinstance(raw_val, str):
                raw_val = datetime.fromisoformat(raw_val)
        setattr(obj, col.key, raw_val)


@admin_bp.route("/")
@login_required
def dashboard():
    admin_required()
    counts = {}
    for name, model in ALLOWED_MODELS.items():
        try:
            counts[name] = model.query.count()
        except Exception:
            counts[name] = "?"
    return render_template("admin/dashboard.html", counts=counts)


@admin_bp.route("/table/<table_name>")
@login_required
def table_view(table_name):
    admin_required()
    model = ALLOWED_MODELS.get(table_name)
    if not model:
        abort(404)
    rows = model.query.limit(100).all()
    columns = [c.key for c in model.__table__.columns]
    return render_template("admin/table.html", table_name=table_name, columns=columns, rows=rows)


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
        db.session.add(obj)
        db.session.commit()
        flash(f"تم إنشاء سجل {table_name}", "success")
        return redirect(url_for("admin.table_view", table_name=table_name))
    example = json.dumps({"field": "value"}, indent=2)
    return render_template("admin/edit.html", table_name=table_name, payload=example, is_new=True)


@admin_bp.route("/table/<table_name>/<int:row_id>/edit", methods=["GET", "POST"])
@login_required
def table_edit(table_name, row_id):
    admin_required()
    model = ALLOWED_MODELS.get(table_name)
    if not model:
        abort(404)
    obj = model.query.get_or_404(row_id)
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
        db.session.commit()
        flash(f"تم تحديث سجل {table_name} {row_id}", "success")
        return redirect(url_for("admin.table_view", table_name=table_name))
    payload = json.dumps(serialize_instance(obj), indent=2, default=str)
    return render_template("admin/edit.html", table_name=table_name, payload=payload, is_new=False, row_id=row_id)


@admin_bp.route("/table/<table_name>/<int:row_id>/delete", methods=["POST"])
@login_required
def table_delete(table_name, row_id):
    admin_required()
    model = ALLOWED_MODELS.get(table_name)
    if not model:
        abort(404)
    obj = model.query.get_or_404(row_id)
    db.session.delete(obj)
    db.session.commit()
    flash(f"تم حذف سجل {table_name} {row_id}", "info")
    return redirect(url_for("admin.table_view", table_name=table_name))
