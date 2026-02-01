from .extensions import db
from .models import Section, LessonActivation, TestActivation, SectionActivation


def cascade_section_activation(section: Section, student_id: int) -> None:
    """Activate all lessons and tests in a section for the given student."""
    if not section:
        return

    # Activate every lesson and its tests
    for lesson in section.lessons:
        if not LessonActivation.query.filter_by(lesson_id=lesson.id, student_id=student_id, active=True).first():
            db.session.add(LessonActivation(lesson_id=lesson.id, student_id=student_id))
        for test in lesson.tests:
            if not TestActivation.query.filter_by(test_id=test.id, student_id=student_id, active=True).first():
                db.session.add(TestActivation(test_id=test.id, student_id=student_id))

    # Activate section-wide tests
    for test in section.tests:
        if test.lesson_id is None:
            if not TestActivation.query.filter_by(test_id=test.id, student_id=student_id, active=True).first():
                db.session.add(TestActivation(test_id=test.id, student_id=student_id))


def cascade_lesson_activation(lesson, student_id: int) -> None:
    """Activate a lesson and all of its tests for the given student."""
    if not lesson:
        return

    if not LessonActivation.query.filter_by(lesson_id=lesson.id, student_id=student_id, active=True).first():
        db.session.add(LessonActivation(lesson_id=lesson.id, student_id=student_id))

    for test in lesson.tests:
        if not TestActivation.query.filter_by(test_id=test.id, student_id=student_id, active=True).first():
            db.session.add(TestActivation(test_id=test.id, student_id=student_id))


def revoke_section_activation(section: Section, student_id: int) -> None:
    """Deactivate section and all related lesson/test activations for a student."""
    SectionActivation.query.filter_by(section_id=section.id, student_id=student_id, active=True).update({"active": False})

    lesson_ids = [l.id for l in section.lessons]
    if lesson_ids:
        LessonActivation.query.filter(
            LessonActivation.lesson_id.in_(lesson_ids),
            LessonActivation.student_id == student_id,
            LessonActivation.active.is_(True),
        ).update({"active": False}, synchronize_session=False)

    test_ids = [t.id for t in section.tests]
    if lesson_ids:
        test_ids += [t.id for l in section.lessons for t in l.tests]
    if test_ids:
        TestActivation.query.filter(
            TestActivation.test_id.in_(test_ids),
            TestActivation.student_id == student_id,
            TestActivation.active.is_(True),
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

    test_ids = [t.id for t in section.tests]
    if lesson_ids:
        test_ids += [t.id for l in section.lessons for t in l.tests]
    if test_ids:
        TestActivation.query.filter(
            TestActivation.test_id.in_(test_ids),
            TestActivation.active.is_(True),
        ).update({"active": False}, synchronize_session=False)
