"""Open every page that takes no URL arguments and check none of them crash."""
from .db_case import DBTestCase


class PageSmokeTests(DBTestCase):
    def _pages(self, prefixes):
        return sorted(
            rule.rule
            for rule in self.app.url_map.iter_rules()
            if "GET" in rule.methods and not rule.arguments and rule.endpoint.startswith(prefixes)
        )

    def _check(self, user, prefixes):
        self.make_open_test()
        self.login(user)
        pages = self._pages(prefixes)
        self.assertTrue(pages)
        for path in pages:
            with self.subTest(path=path):
                self.assertLess(self.client.get(path).status_code, 400)

    def test_student_pages(self):
        self._check(self.make_user("student"), ("student.",))

    def test_staff_pages(self):
        self._check(self.make_user("admin"), ("teacher.", "admin."))

    def test_guest_pages(self):
        self.make_open_test()
        for path in ("/", "/subjects", "/login", "/register"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)
