import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bson import ObjectId

from study_platform.app import create_app


class _ChainQuery:
    def __init__(self, items=None):
        self._items = items or []

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def only(self, *args, **kwargs):
        return self

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
        tests = [
            SimpleNamespace(
                id=ObjectId(),
                title="Scoped Test 1",
                section_id=SimpleNamespace(id=section_id, title="Section A"),
                lesson_id=SimpleNamespace(title="Lesson A"),
            )
        ]

        with patch("flask_login.utils._get_user", return_value=self._user("question_editor")), \
             patch("study_platform.teacher._allowed_subject_ids_for_current_user", return_value={ObjectId()}), \
             patch("study_platform.teacher.Section.objects", return_value=_ChainQuery([SimpleNamespace(id=section_id)])), \
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


if __name__ == "__main__":
    unittest.main()
