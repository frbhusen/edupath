import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bson import ObjectId
from mongoengine.errors import DoesNotExist

from study_platform.app import create_app


class _ChainQuery:
    def __init__(self, items=None):
        self._items = items or []

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def only(self, *args, **kwargs):
        return self

    def count(self):
        return len(self._items)

    def all(self):
        return self._items

    def first(self):
        return self._items[0] if self._items else None


class TeacherRoutePermissionTests(unittest.TestCase):
    def setUp(self):
        with patch("study_platform.app.init_mongo"), patch("study_platform.app._migrate_legacy_teacher_role_once"):
            self.app = create_app()
        self.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        # Disable global before_request hooks (especially single-device DB token check) for unit route tests.
        self.app.before_request_funcs[None] = []
        self.client = self.app.test_client()

    def _user(self, role):
        return SimpleNamespace(
            id=ObjectId(),
            role=role,
            username=f"{role}_user",
            is_authenticated=True,
        )

    def _get(self, path, role):
        with patch("flask_login.utils._get_user", return_value=self._user(role)):
            return self.client.get(path, follow_redirects=False)

    def test_teacher_root_redirects_editor_to_editor_dashboard(self):
        response = self._get("/teacher/", "question_editor")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/teacher/question-editor", response.location)

    def test_teacher_root_redirects_teacher_to_teacher_dashboard(self):
        response = self._get("/teacher/", "teacher")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/teacher/dashboard", response.location)

    def test_question_editor_dashboard_denies_teacher_role(self):
        response = self._get("/teacher/question-editor", "teacher")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.location)

    def test_question_editor_dashboard_allows_question_editor(self):
        section_id = ObjectId()
        subject_id = ObjectId()
        tests = [
            SimpleNamespace(
                id=ObjectId(),
                title="Scoped Test 1",
                section_id=SimpleNamespace(id=section_id, title="Section A", subject_id=SimpleNamespace(id=subject_id)),
                lesson_id=SimpleNamespace(title="Lesson A"),
            )
        ]

        with patch("flask_login.utils._get_user", return_value=self._user("question_editor")), \
             patch("study_platform.teacher._allowed_subject_ids_for_current_user", return_value={subject_id}), \
               patch("study_platform.teacher.Section.objects", return_value=_ChainQuery([SimpleNamespace(id=section_id, subject_id=SimpleNamespace(id=subject_id))])), \
             patch("study_platform.teacher.Subject.objects", return_value=_ChainQuery([SimpleNamespace(id=subject_id, name="Subject A", description="", requires_code=False)])), \
             patch("study_platform.teacher.Test.objects", return_value=_ChainQuery(tests)):
            response = self.client.get("/teacher/question-editor", follow_redirects=False)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Scoped Test 1", response.get_data(as_text=True))

    def test_students_route_allows_admin_and_denies_teacher(self):
        students = [SimpleNamespace(id=ObjectId(), username="s1", full_name="Student One")]

        with patch("flask_login.utils._get_user", return_value=self._user("admin")), \
             patch("study_platform.teacher.User.objects", return_value=_ChainQuery(students)):
            allowed = self.client.get("/teacher/students", follow_redirects=False)

        denied = self._get("/teacher/students", "teacher")

        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(denied.status_code, 302)
        self.assertIn("/login", denied.location)

    def test_analytics_dashboard_ignores_orphaned_attempt_test_refs(self):
        class _OrphanAttempt:
            score = 9
            total = 10
            submitted_at = None

            @property
            def test_id(self):
                raise DoesNotExist("test missing")

        orphan_attempt = _OrphanAttempt()
        student_id = ObjectId()
        student_user = SimpleNamespace(id=student_id, full_name="Student One")
        gamification_profile = SimpleNamespace(student_id=SimpleNamespace(id=student_id), xp_total=120, level=2)
        empty_query = _ChainQuery([])

        with patch("flask_login.utils._get_user", return_value=self._user("admin")), \
             patch("study_platform.teacher.Attempt.objects", return_value=_ChainQuery([orphan_attempt])), \
             patch("study_platform.teacher.LessonCompletion.objects", return_value=empty_query), \
             patch("study_platform.teacher.Assignment.objects", return_value=empty_query), \
             patch("study_platform.teacher.AssignmentSubmission.objects", return_value=empty_query), \
             patch("study_platform.teacher.AssignmentAttempt.objects", return_value=empty_query), \
               patch("study_platform.teacher.StudyPlan.objects", return_value=empty_query), \
             patch("study_platform.teacher.User.objects", return_value=_ChainQuery([student_user])), \
             patch("study_platform.teacher.Subject.objects", return_value=empty_query), \
             patch("study_platform.teacher.Section.objects", return_value=empty_query), \
             patch("study_platform.teacher.Lesson.objects", return_value=empty_query), \
             patch("study_platform.teacher.Test.objects", return_value=empty_query), \
             patch("study_platform.teacher.Question.objects", return_value=empty_query), \
             patch("study_platform.teacher.TestInteractiveQuestion.objects", return_value=empty_query), \
             patch("study_platform.teacher.StudentGamification.objects", return_value=_ChainQuery([gamification_profile])):

            response = self.client.get("/teacher/analytics", follow_redirects=False)

        self.assertEqual(response.status_code, 200)
        self.assertIn("لوحة التحليلات", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
