from .extensions import db
from .models import Subject, Section, LessonActivation, SectionActivation, SubjectActivation


def cascade_subject_activation(subject: Subject, student_id: int) -> None:
    """Activate entire subject: all sections, all lessons, all tests."""
    if not subject:
        return

    # Activate all sections in the subject
    for section in subject.sections:
        if not SectionActivation.query.filter_by(section_id=section.id, student_id=student_id, active=True).first():
            db.session.add(SectionActivation(section_id=section.id, student_id=student_id))
        
        # Activate all lessons in each section
        for lesson in section.lessons:
            if not LessonActivation.query.filter_by(lesson_id=lesson.id, student_id=student_id, active=True).first():
                db.session.add(LessonActivation(lesson_id=lesson.id, student_id=student_id))


def cascade_section_activation(section: Section, student_id: int) -> None:
    """Activate section: all lessons in section (section-wide tests inherit from section activation)."""
    if not section:
        return

    # Activate all lessons in the section
    for lesson in section.lessons:
        if not LessonActivation.query.filter_by(lesson_id=lesson.id, student_id=student_id, active=True).first():
            db.session.add(LessonActivation(lesson_id=lesson.id, student_id=student_id))


def cascade_lesson_activation(lesson, student_id: int) -> None:
    """Activate a lesson only (tests linked to lesson inherit from lesson activation, not section-wide tests)."""
    if not lesson:
        return

    if not LessonActivation.query.filter_by(lesson_id=lesson.id, student_id=student_id, active=True).first():
        db.session.add(LessonActivation(lesson_id=lesson.id, student_id=student_id))


def revoke_subject_activation(subject: Subject, student_id: int) -> None:
    """Deactivate subject and all related section/lesson activations for a student."""
    SubjectActivation.query.filter_by(subject_id=subject.id, student_id=student_id, active=True).update({"active": False})

    section_ids = [s.id for s in subject.sections]
    if section_ids:
        SectionActivation.query.filter(
            SectionActivation.section_id.in_(section_ids),
            SectionActivation.student_id == student_id,
            SectionActivation.active.is_(True),
        ).update({"active": False}, synchronize_session=False)

        # Get all lesson IDs from all sections
        lesson_ids = []
        for section in subject.sections:
            lesson_ids.extend([l.id for l in section.lessons])
        
        if lesson_ids:
            LessonActivation.query.filter(
                LessonActivation.lesson_id.in_(lesson_ids),
                LessonActivation.student_id == student_id,
                LessonActivation.active.is_(True),
            ).update({"active": False}, synchronize_session=False)


def revoke_section_activation(section: Section, student_id: int) -> None:
    """Deactivate section and all related lesson activations for a student."""
    SectionActivation.query.filter_by(section_id=section.id, student_id=student_id, active=True).update({"active": False})

    lesson_ids = [l.id for l in section.lessons]
    if lesson_ids:
        LessonActivation.query.filter(
            LessonActivation.lesson_id.in_(lesson_ids),
            LessonActivation.student_id == student_id,
            LessonActivation.active.is_(True),
        ).update({"active": False}, synchronize_session=False)


def lock_subject_access_for_all(subject: Subject) -> None:
    """When a subject is (re)locked, deactivate all activations so only freebies stay open."""
    SubjectActivation.query.filter_by(subject_id=subject.id, active=True).update({"active": False})

    section_ids = [s.id for s in subject.sections]
    if section_ids:
        SectionActivation.query.filter(
            SectionActivation.section_id.in_(section_ids),
            SectionActivation.active.is_(True),
        ).update({"active": False}, synchronize_session=False)

        # Get all lesson IDs from all sections
        lesson_ids = []
        for section in subject.sections:
            lesson_ids.extend([l.id for l in section.lessons])
        
        if lesson_ids:
            LessonActivation.query.filter(
                LessonActivation.lesson_id.in_(lesson_ids),
                LessonActivation.active.is_(True),
            ).update({"active": False}, synchronize_session=False)


def lock_section_access_for_all(section: Section) -> None:
    """When a section is (re)locked, deactivate all activations so only freebies stay open."""
    SectionActivation.query.filter_by(section_id=section.id, active=True).update({"active": False})

    lesson_ids = [l.id for l in section.lessons]
    if lesson_ids:
        LessonActivation.query.filter(
            LessonActivation.lesson_id.in_(lesson_ids),
            LessonActivation.active.is_(True),
        ).update({"active": False}, synchronize_session=False)
