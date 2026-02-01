from .models import Subject, Section, Lesson, LessonActivation, SectionActivation, SubjectActivation


def cascade_subject_activation(subject: Subject, student_id: int) -> None:
    """Activate entire subject: all sections, all lessons, all tests."""
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
    """Activate section: all lessons in section (section-wide tests inherit from section activation)."""
    if not section:
        return

    # Activate all lessons in the section
    for lesson in Lesson.objects(section_id=section.id).all():
        if not LessonActivation.objects(lesson_id=lesson.id, student_id=student_id, active=True).first():
            la = LessonActivation(lesson_id=lesson.id, student_id=student_id)
            la.save()


def cascade_lesson_activation(lesson, student_id: int) -> None:
    """Activate a lesson only (tests linked to lesson inherit from lesson activation, not section-wide tests)."""
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

        # Get all lesson IDs from all sections
        lesson_ids = []
        for section in sections:
            lessons = LessonActivation.objects(section_id=section.id).all()
            lesson_ids.extend([l.id for l in lessons])
        
        if lesson_ids:
            for les_activation in LessonActivation.objects(lesson_id__in=lesson_ids, student_id=student_id, active=True).all():
                les_activation.active = False
                les_activation.save()


def revoke_section_activation(section_id, student_id: int) -> None:
    """Deactivate section and all related lesson activations for a student."""
    for sa in SectionActivation.objects(section_id=section_id, student_id=student_id, active=True).all():
        sa.active = False
        sa.save()

    # Get all lesson IDs in section
    lessons = LessonActivation.objects(section_id=section_id).all()
    lesson_ids = [l.id for l in lessons]
    
    if lesson_ids:
        for les_activation in LessonActivation.objects(lesson_id__in=lesson_ids, student_id=student_id, active=True).all():
            les_activation.active = False
            les_activation.save()


def lock_subject_access_for_all(subject_id) -> None:
    """When a subject is (re)locked, deactivate all activations so only freebies stay open."""
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

        # Get all lesson IDs from all sections
        lesson_ids = []
        for section in sections:
            lessons = LessonActivation.objects(section_id=section.id).all()
            lesson_ids.extend([l.id for l in lessons])
        
        if lesson_ids:
            for les_activation in LessonActivation.objects(lesson_id__in=lesson_ids, active=True).all():
                les_activation.active = False
                les_activation.save()


def lock_section_access_for_all(section_id) -> None:
    """When a section is (re)locked, deactivate all activations so only freebies stay open."""
    for sa in SectionActivation.objects(section_id=section_id, active=True).all():
        sa.active = False
        sa.save()

    # Get all lesson IDs in section
    lessons = LessonActivation.objects(section_id=section_id).all()
    lesson_ids = [l.id for l in lessons]
    
    if lesson_ids:
        for les_activation in LessonActivation.objects(lesson_id__in=lesson_ids, active=True).all():
            les_activation.active = False
            les_activation.save()

