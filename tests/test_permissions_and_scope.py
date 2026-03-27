import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bson import ObjectId
from flask import Blueprint, Flask

from study_platform import permissions
from study_platform import teacher


class PermissionsUnitTests(unittest.TestCase):
    def test_role_normalization_and_flags(self):
        admin = SimpleNamespace(role=" Admin ")
        teacher_user = SimpleNamespace(role="teacher")
        editor = SimpleNamespace(role="question_editor")
        student = SimpleNamespace(role="student")

        self.assertEqual(permissions.normalize_role(" Admin "), "admin")
        self.assertTrue(permissions.is_admin(admin))
        self.assertTrue(permissions.is_teacher(teacher_user))
        self.assertTrue(permissions.is_question_editor(editor))
        self.assertFalse(permissions.is_admin(student))

    @patch("study_platform.permissions.StaffSubjectAccess.objects")
    def test_get_staff_subject_ids(self, mock_objects):
        sid1 = ObjectId()
        sid2 = ObjectId()
        rows = [
            SimpleNamespace(subject_id=SimpleNamespace(id=sid1)),
            SimpleNamespace(subject_id=SimpleNamespace(id=sid2)),
            SimpleNamespace(subject_id=None),
        ]

        query = SimpleNamespace(all=lambda: rows)
        mock_objects.return_value = SimpleNamespace(only=lambda *_: query)

        result = permissions.get_staff_subject_ids(ObjectId())
        self.assertEqual(result, [sid1, sid2])

    @patch("study_platform.permissions.StaffSubjectAccess.objects")
    def test_has_subject_access_for_scoped_staff(self, mock_objects):
        user = SimpleNamespace(id=ObjectId(), role="teacher")
        sid = ObjectId()

        mock_objects.return_value = SimpleNamespace(first=lambda: SimpleNamespace(id=ObjectId()))
        self.assertTrue(permissions.has_subject_access(user, sid))

        mock_objects.return_value = SimpleNamespace(first=lambda: None)
        self.assertFalse(permissions.has_subject_access(user, sid))

    @patch("study_platform.permissions.StaffSubjectAccess.objects")
    def test_has_subject_access_for_admin_and_invalid_id(self, mock_objects):
        admin = SimpleNamespace(id=ObjectId(), role="admin")
        self.assertTrue(permissions.has_subject_access(admin, ObjectId()))

        scoped = SimpleNamespace(id=ObjectId(), role="teacher")
        self.assertFalse(permissions.has_subject_access(scoped, "not-an-object-id"))
        mock_objects.assert_not_called()


class TeacherScopeHelperUnitTests(unittest.TestCase):
    def test_subject_resolution_helpers(self):
        sid = ObjectId()
        section = SimpleNamespace(subject_id=SimpleNamespace(id=sid))
        lesson = SimpleNamespace(section_id=section)
        test = SimpleNamespace(section_id=section)

        self.assertEqual(teacher._subject_id_for_section(section), sid)
        self.assertEqual(teacher._subject_id_for_lesson(lesson), sid)
        self.assertEqual(teacher._subject_id_for_test(test), sid)

    def test_subject_id_for_assignment_priority(self):
        sid_direct = ObjectId()
        sid_section = ObjectId()
        sid_lesson = ObjectId()

        assignment_direct = SimpleNamespace(
            subject_id=SimpleNamespace(id=sid_direct),
            section_id=None,
            lesson_id=None,
        )
        self.assertEqual(teacher._subject_id_for_assignment(assignment_direct), sid_direct)

        assignment_section = SimpleNamespace(
            subject_id=None,
            section_id=SimpleNamespace(subject_id=SimpleNamespace(id=sid_section)),
            lesson_id=None,
        )
        self.assertEqual(teacher._subject_id_for_assignment(assignment_section), sid_section)

        assignment_lesson = SimpleNamespace(
            subject_id=None,
            section_id=None,
            lesson_id=SimpleNamespace(section_id=SimpleNamespace(subject_id=SimpleNamespace(id=sid_lesson))),
        )
        self.assertEqual(teacher._subject_id_for_assignment(assignment_lesson), sid_lesson)

    @patch("study_platform.teacher.Question.objects")
    def test_custom_attempt_subject_resolution(self, mock_question_objects):
        sid = ObjectId()
        test = SimpleNamespace(section_id=SimpleNamespace(subject_id=SimpleNamespace(id=sid)))
        question = SimpleNamespace(test_id=test)

        mock_question_objects.return_value = SimpleNamespace(first=lambda: question)
        attempt = SimpleNamespace(selections_json='["%s"]' % ObjectId())

        result = teacher._custom_attempt_subject_id(attempt)
        self.assertEqual(result, sid)

    @patch("study_platform.teacher.get_staff_subject_ids")
    @patch("study_platform.teacher.is_admin")
    def test_allowed_subject_ids_for_current_user(self, mock_is_admin, mock_get_subjects):
        with patch("study_platform.teacher.current_user", SimpleNamespace(id=ObjectId(), role="teacher")):
            mock_is_admin.return_value = False
            expected = [ObjectId(), ObjectId()]
            mock_get_subjects.return_value = expected
            self.assertEqual(teacher._allowed_subject_ids_for_current_user(), set(expected))

            mock_is_admin.return_value = True
            self.assertIsNone(teacher._allowed_subject_ids_for_current_user())


class RoleDecoratorUnitTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.secret_key = "test-secret"

        auth_bp = Blueprint("auth", __name__)

        @auth_bp.route("/login")
        def login():
            return "login"

        self.app.register_blueprint(auth_bp)

    def test_role_required_allows_permitted_role(self):
        @teacher.role_required("teacher", "question_editor")
        def protected():
            return "ok"

        with self.app.test_request_context("/"):
            with patch(
                "study_platform.teacher.current_user",
                SimpleNamespace(is_authenticated=True, role="question_editor"),
            ):
                self.assertEqual(protected(), "ok")

    def test_role_required_denies_unpermitted_role(self):
        @teacher.role_required("teacher")
        def protected():
            return "ok"

        with self.app.test_request_context("/"):
            with patch(
                "study_platform.teacher.current_user",
                SimpleNamespace(is_authenticated=True, role="student"),
            ):
                response = protected()
                self.assertEqual(getattr(response, "status_code", None), 302)
                self.assertIn("/login", getattr(response, "location", ""))

    def test_role_required_denies_unauthenticated(self):
        @teacher.role_required("teacher")
        def protected():
            return "ok"

        with self.app.test_request_context("/"):
            with patch(
                "study_platform.teacher.current_user",
                SimpleNamespace(is_authenticated=False, role="teacher"),
            ):
                response = protected()
                self.assertEqual(getattr(response, "status_code", None), 302)
                self.assertIn("/login", getattr(response, "location", ""))


if __name__ == "__main__":
    unittest.main()
