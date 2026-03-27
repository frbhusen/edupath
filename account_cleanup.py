# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

from .models import (
    User,
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
    LessonCompletion,
    Assignment,
    AssignmentSubmission,
    AssignmentAttempt,
    StudyPlan,
    StudyPlanItem,
    DiscussionQuestion,
    DiscussionAnswer,
    Certificate,
    XPEvent,
    StudentGamification,
    Duel,
    DuelAnswer,
    DuelStats,
    StaffSubjectAccess,
    StaffSubjectAccessAudit,
    StaffActivityLog,
)


def delete_user_with_related_data(user: User) -> dict:
    """Delete a user and most related platform records.

    Returns a summary dict with rough counters for logging/UX.
    """
    if not user:
        return {"deleted_user": False}

    uid = user.id
    summary = {
        "deleted_user": False,
        "regular_attempts": 0,
        "custom_attempts": 0,
        "assignments": 0,
        "duels": 0,
    }

    # 1) Attempts and answers
    regular_attempt_ids = [a.id for a in Attempt.objects(student_id=uid).only("id").all()]
    if regular_attempt_ids:
        AttemptAnswer.objects(attempt_id__in=regular_attempt_ids).delete()
        AttemptTextAnswer.objects(attempt_id__in=regular_attempt_ids).delete()
        Attempt.objects(id__in=regular_attempt_ids).delete()
        summary["regular_attempts"] = len(regular_attempt_ids)

    custom_attempt_ids = [a.id for a in CustomTestAttempt.objects(student_id=uid).only("id").all()]
    if custom_attempt_ids:
        CustomTestAnswer.objects(attempt_id__in=custom_attempt_ids).delete()
        CustomTestAttempt.objects(id__in=custom_attempt_ids).delete()
        summary["custom_attempts"] = len(custom_attempt_ids)

    # 2) Activations and progress
    SectionActivation.objects(student_id=uid).delete()
    ActivationCode.objects(student_id=uid).delete()
    LessonActivation.objects(student_id=uid).delete()
    LessonActivationCode.objects(student_id=uid).delete()
    SubjectActivation.objects(student_id=uid).delete()
    SubjectActivationCode.objects(student_id=uid).delete()
    LessonCompletion.objects(student_id=uid).delete()

    # 3) Assignment related rows.
    AssignmentSubmission.objects(student_id=uid).delete()
    AssignmentAttempt.objects(student_id=uid).delete()

    assignment_ids = set()
    assignment_ids.update([a.id for a in Assignment.objects(target_student_id=uid).only("id").all()])
    assignment_ids.update([a.id for a in Assignment.objects(created_by=uid).only("id").all()])
    if assignment_ids:
        AssignmentSubmission.objects(assignment_id__in=list(assignment_ids)).delete()
        AssignmentAttempt.objects(assignment_id__in=list(assignment_ids)).delete()
        Assignment.objects(id__in=list(assignment_ids)).delete()
        summary["assignments"] = len(assignment_ids)

    # 4) Study plans where user is owner or creator.
    plan_ids = set([p.id for p in StudyPlan.objects(student_id=uid).only("id").all()])
    plan_ids.update([p.id for p in StudyPlan.objects(created_by=uid).only("id").all()])
    if plan_ids:
        StudyPlanItem.objects(plan_id__in=list(plan_ids)).delete()
        StudyPlan.objects(id__in=list(plan_ids)).delete()

    # 5) Discussion content (including pinned questions).
    question_ids = [q.id for q in DiscussionQuestion.objects(author_id=uid).only("id").all()]
    if question_ids:
        DiscussionAnswer.objects(question_id__in=question_ids).delete()
        DiscussionQuestion.objects(id__in=question_ids).delete()
    DiscussionAnswer.objects(author_id=uid).delete()

    # 6) Certificates and gamification
    Certificate.objects(student_id=uid).delete()
    Certificate.objects(verified_by=uid).delete()
    XPEvent.objects(student_id=uid).delete()
    StudentGamification.objects(student_id=uid).delete()

    # 7) Duels and duel stats.
    duel_ids = [d.id for d in Duel.objects(__raw__={"$or": [{"challenger_id": uid}, {"opponent_id": uid}]}).only("id").all()]
    if duel_ids:
        DuelAnswer.objects(duel_id__in=duel_ids).delete()
        Duel.objects(id__in=duel_ids).delete()
        summary["duels"] = len(duel_ids)
    DuelStats.objects(student_id=uid).delete()

    # 8) Staff assignment / audit references.
    StaffSubjectAccess.objects(staff_user_id=uid).delete()
    StaffSubjectAccess.objects(assigned_by=uid).delete()
    StaffSubjectAccessAudit.objects(staff_user_id=uid).delete()
    StaffSubjectAccessAudit.objects(changed_by=uid).delete()
    StaffActivityLog.objects(staff_user_id=uid).delete()

    # 9) Finally delete the user.
    user.delete()
    summary["deleted_user"] = True
    return summary
