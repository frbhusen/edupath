from bson import ObjectId

from .models import StaffSubjectAccess


ADMIN_ROLES = {"admin"}
TEACHER_ROLES = {"teacher"}
QUESTION_EDITOR_ROLES = {"question_editor"}


def normalize_role(role_value):
    return (role_value or "").strip().lower()


def is_admin(user):
    if not user:
        return False
    return normalize_role(getattr(user, "role", "")) in ADMIN_ROLES


def is_teacher(user):
    if not user:
        return False
    return normalize_role(getattr(user, "role", "")) in TEACHER_ROLES


def is_question_editor(user):
    if not user:
        return False
    return normalize_role(getattr(user, "role", "")) in QUESTION_EDITOR_ROLES


def is_staff_with_subject_scope(user):
    return is_teacher(user) or is_question_editor(user)


def get_staff_subject_ids(user_id):
    if not user_id:
        return []
    rows = list(StaffSubjectAccess.objects(staff_user_id=user_id, active=True).only("subject_id").all())
    out = []
    for row in rows:
        if row.subject_id:
            out.append(row.subject_id.id)
    return out


def has_subject_access(user, subject_id):
    if not user or not subject_id:
        return False
    if is_admin(user):
        return True
    if not is_staff_with_subject_scope(user):
        return False

    sid = subject_id
    if isinstance(subject_id, str):
        if not ObjectId.is_valid(subject_id):
            return False
        sid = ObjectId(subject_id)

    return bool(StaffSubjectAccess.objects(staff_user_id=user.id, subject_id=sid, active=True).first())


def can_manage_tests(user):
    return is_admin(user) or is_teacher(user)


def can_manage_resources(user):
    return is_admin(user) or is_teacher(user)


def can_manage_assignments(user):
    return is_admin(user) or is_teacher(user)


def can_manage_certificates(user):
    return is_admin(user) or is_teacher(user)


def can_manage_pinned_qna(user):
    return is_admin(user) or is_teacher(user)


def can_edit_questions_only(user):
    return is_question_editor(user)
