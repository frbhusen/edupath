import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bson import ObjectId

from study_platform.app import create_app


class _Query:
    def __init__(self, value):
        self._value = value

    def first(self):
        return self._value


class AdminAccountDeletionRouteTests(unittest.TestCase):
    def setUp(self):
        with patch("study_platform.app.init_mongo"), patch("study_platform.app._migrate_legacy_teacher_role_once"):
            self.app = create_app()
        self.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self.app.before_request_funcs[None] = []
        self.client = self.app.test_client()

    def _current_admin(self, user_id=None):
        return SimpleNamespace(
            id=user_id or ObjectId(),
            role="admin",
            username="admin_user",
            is_authenticated=True,
        )

    def test_admin_cannot_delete_own_account(self):
        current_id = ObjectId()
        current = self._current_admin(current_id)
        target = SimpleNamespace(id=current_id, role="admin")

        with patch("flask_login.utils._get_user", return_value=current), \
             patch("study_platform.admin.User.objects", return_value=_Query(target)), \
             patch("study_platform.admin.delete_user_with_related_data") as mock_delete:
            response = self.client.post(f"/admin/users/{current_id}/delete", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/staff", response.location)
        mock_delete.assert_not_called()

    def test_admin_cannot_delete_last_admin(self):
        current = self._current_admin()
        target_id = ObjectId()
        target = SimpleNamespace(id=target_id, role="admin")

        with patch("flask_login.utils._get_user", return_value=current), \
             patch("study_platform.admin.User.objects", side_effect=[_Query(target), _Query(None)]), \
             patch("study_platform.admin.delete_user_with_related_data") as mock_delete:
            response = self.client.post(f"/admin/users/{target_id}/delete", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/staff", response.location)
        mock_delete.assert_not_called()

    def test_admin_can_delete_non_admin_user(self):
        current = self._current_admin()
        target_id = ObjectId()
        target = SimpleNamespace(id=target_id, role="teacher")

        with patch("flask_login.utils._get_user", return_value=current), \
             patch("study_platform.admin.User.objects", return_value=_Query(target)), \
             patch("study_platform.admin.delete_user_with_related_data") as mock_delete:
            response = self.client.post(f"/admin/users/{target_id}/delete", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/staff", response.location)
        mock_delete.assert_called_once_with(target)


if __name__ == "__main__":
    unittest.main()
