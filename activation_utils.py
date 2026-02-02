from .models import Subject, Section, Lesson, LessonActivation, SectionActivation, SubjectActivation


def cascade_subject_activation(subject: Subject, student_id: int) -> None:
    """Activate entire subject: all sections and lessons for a student."""
    if not subject:
        return

    # Activate all sections in the subject
    for section in Section.objects(subject_id=subject.id).all():
        if not SectionActivation.objects(section_id=section.id, student_id=student_id, active=True).first():
            sa = SectionActivation(section_id=section.id, student_id=student_id)
            sa.save()
        
        # Activate all lessons in each section
        for lesson in Lesson.objects(section_id=section.id).all():
            if not LessonActivation.objects(lesson_id=lesson.id, student_id=student_id, active=True).first():
                la = LessonActivation(lesson_id=lesson.id, student_id=student_id)
                la.save()


def cascade_section_activation(section: Section, student_id: int) -> None:
    """Activate section: all lessons in section."""
    if not section:
        return

    # Activate all lessons in the section
    for lesson in Lesson.objects(section_id=section.id).all():
        if not LessonActivation.objects(lesson_id=lesson.id, student_id=student_id, active=True).first():
            la = LessonActivation(lesson_id=lesson.id, student_id=student_id)
            la.save()


def cascade_lesson_activation(lesson, student_id: int) -> None:
    """Activate a lesson only (tests linked to lesson inherit from lesson activation)."""
    if not lesson:
        return

    if not LessonActivation.objects(lesson_id=lesson.id, student_id=student_id, active=True).first():
        la = LessonActivation(lesson_id=lesson.id, student_id=student_id)
        la.save()


def revoke_subject_activation(subject_id, student_id: int) -> None:
    """Deactivate subject and all related section/lesson activations for a student."""
    for sa in SubjectActivation.objects(subject_id=subject_id, student_id=student_id, active=True).all():
        sa.active = False
        sa.save()

    # Get all section IDs from subject
    sections = Section.objects(subject_id=subject_id).all()
    section_ids = [s.id for s in sections]
    
    if section_ids:
        for sec_activation in SectionActivation.objects(section_id__in=section_ids, student_id=student_id, active=True).all():
            sec_activation.active = False
            sec_activation.save()

        # Use lesson IDs from the subject's sections to deactivate lesson activations
        lesson_ids = [l.id for l in Lesson.objects(section_id__in=section_ids).all()]
        if lesson_ids:
            for les_activation in LessonActivation.objects(lesson_id__in=lesson_ids, student_id=student_id, active=True).all():
                les_activation.active = False
                les_activation.save()


def revoke_section_activation(section_id, student_id: int) -> None:
    """Deactivate section and all related lesson activations for a student."""
    for sa in SectionActivation.objects(section_id=section_id, student_id=student_id, active=True).all():
        sa.active = False
        sa.save()

    # Pull lesson IDs from the section to deactivate their activations
    lesson_ids = [l.id for l in Lesson.objects(section_id=section_id).all()]
    
    if lesson_ids:
        for les_activation in LessonActivation.objects(lesson_id__in=lesson_ids, student_id=student_id, active=True).all():
            les_activation.active = False
            les_activation.save()


def lock_subject_access_for_all(subject_id) -> None:
    """When a subject is (re)locked, deactivate all activations for all students."""
    for sa in SubjectActivation.objects(subject_id=subject_id, active=True).all():
        sa.active = False
        sa.save()

    # Get all sections and their lessons
    sections = Section.objects(subject_id=subject_id).all()
    section_ids = [s.id for s in sections]
    
    if section_ids:
        for sec_activation in SectionActivation.objects(section_id__in=section_ids, active=True).all():
            sec_activation.active = False
            sec_activation.save()

        # Deactivate lesson activations for all lessons in the subject
        lesson_ids = [l.id for l in Lesson.objects(section_id__in=section_ids).all()]
        if lesson_ids:
            for les_activation in LessonActivation.objects(lesson_id__in=lesson_ids, active=True).all():
                les_activation.active = False
                les_activation.save()


def lock_section_access_for_all(section_id) -> None:
    """When a section is (re)locked, deactivate all activations for all students."""
    for sa in SectionActivation.objects(section_id=section_id, active=True).all():
        sa.active = False
        sa.save()

    # Deactivate lesson activations for lessons within this section
    lesson_ids = [l.id for l in Lesson.objects(section_id=section_id).all()]
    
    if lesson_ids:
        for les_activation in LessonActivation.objects(lesson_id__in=lesson_ids, active=True).all():
            les_activation.active = False
            les_activation.save()

