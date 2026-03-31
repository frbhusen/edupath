from __future__ import annotations

from flask import request
from flask_login import current_user

from .models import StaffActivityLog


_STAFF_ROLES = {"admin", "teacher", "question_editor"}
_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_TARGET_ID_KEYS = (
    "user_id",
    "subject_id",
    "section_id",
    "lesson_id",
    "course_set_id",
    "course_id",
    "resource_id",
    "test_id",
    "question_id",
    "attempt_id",
    "assignment_id",
    "certificate_id",
)
_SENSITIVE_KEYS = {
    "password",
    "password_hash",
    "current_session_token",
    "csrf_token",
}
_IGNORED_KEYS = {
    "action",
    "submit",
    "next",
}


def _safe_str(value, max_len: int = 300) -> str:
    if value is None:
        return ""
    return str(value)[:max_len]


def _format_value(value: str) -> str:
    compact = " ".join((value or "").split())
    if len(compact) > 70:
        compact = compact[:67] + "..."
    return compact


def _form_changes_summary() -> str:
    if not request.form:
        return ""

    parts = []
    for key in request.form.keys():
        key_l = key.lower()
        if key_l in _SENSITIVE_KEYS or key_l in _IGNORED_KEYS:
            continue

        values = [v for v in request.form.getlist(key) if v not in (None, "")]
        if not values:
            continue

        if len(values) == 1:
            parts.append(f"{key} -> {_format_value(values[0])}")
        else:
            compact_values = ", ".join(_format_value(v) for v in values[:4])
            if len(values) > 4:
                compact_values += f", +{len(values) - 4} more"
            parts.append(f"{key} -> [{compact_values}]")

    if not parts:
        return ""
    summary = " | ".join(parts)
    return _safe_str(summary, 500)


def _build_details(endpoint: str, target_type: str | None, target_id: str | None) -> str:
    summary = _form_changes_summary()
    if summary:
        return summary

    action_name = endpoint.split(".", 1)[-1] if "." in endpoint else endpoint
    if "delete" in action_name and target_type and target_id:
        return _safe_str(f"deleted {target_type} id={target_id}", 500)
    if target_type and target_id:
        return _safe_str(f"updated {target_type} id={target_id}", 500)
    return "-"


def _extract_target(view_args: dict | None) -> tuple[str | None, str | None]:
    if not view_args:
        return None, None

    for key in _TARGET_ID_KEYS:
        if key in view_args and view_args[key] is not None:
            entity = key.replace("_id", "")
            return entity, _safe_str(view_args[key], 80)

    return None, None


def log_staff_activity_from_request(response) -> None:
    """Persist a compact activity row for staff mutating requests."""
    try:
        if not getattr(current_user, "is_authenticated", False):
            return

        role = ((getattr(current_user, "role", "") or "").strip().lower())
        if role not in _STAFF_ROLES:
            return

        method = (request.method or "").upper()
        if method not in _MUTATING_METHODS:
            return

        endpoint = request.endpoint or ""
        if not endpoint.startswith("teacher.") and not endpoint.startswith("admin."):
            return

        target_type, target_id = _extract_target(getattr(request, "view_args", None))
        action_name = endpoint.split(".", 1)[-1] if "." in endpoint else endpoint
        details = _build_details(endpoint, target_type, target_id)

        StaffActivityLog(
            staff_user_id=current_user.id,
            staff_role=role,
            endpoint=_safe_str(endpoint, 120),
            action=_safe_str(action_name, 120),
            http_method=method,
            path=_safe_str(request.path, 300),
            target_type=target_type,
            target_id=target_id,
            details=_safe_str(details, 500),
            status_code=int(getattr(response, "status_code", 200) or 200),
            success=bool(getattr(response, "status_code", 200) < 400),
        ).save()
    except Exception:
        # Never break user requests because of audit logging.
        return
