"""Base class for tests that need a real (in-memory) MongoDB.

Uses mongomock, so no database server is needed. Each test gets an empty
database and a Flask test client with CSRF and rate limits switched off
(tests that check rate limits switch them back on).
"""
import re
import unittest
from unittest.mock import patch

import mongomock
from mongoengine import connect, disconnect

from study_platform.app import create_app
from study_platform.config import Config
from study_platform.extensions import cache, limiter
from study_platform.models import Section, Subject, Test, User


class DBTestCase(unittest.TestCase):
    rate_limits = False

    def setUp(self):
        disconnect()
        connect("edupath_test", host="mongodb://localhost", mongo_client_class=mongomock.MongoClient)
        with patch("study_platform.app.init_mongo"), \
             patch("study_platform.app._migrate_legacy_teacher_role_once"), \
             patch.object(Config, "SECRET_KEY", "test-secret-key"):
            self.app = create_app()
        self.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self.app.config["RATELIMIT_ENABLED"] = self.rate_limits
        limiter.enabled = self.rate_limits
        limiter.reset()
        self.client = self.app.test_client()
        with self.app.app_context():
            cache.clear()

    def tearDown(self):
        disconnect()

    # --- fixtures -----------------------------------------------------
    _counter = 0

    def make_user(self, role="student", password="secret123"):
        DBTestCase._counter += 1
        n = DBTestCase._counter
        user = User(
            first_name="Test",
            last_name=f"User{n}",
            username=f"{role}{n}",
            phone=f"09{n:08d}",
            role=role,
        )
        user.set_password(password)
        user.save()
        return user

    def make_open_test(self, owner=None):
        """A subject/section/test that needs no activation code."""
        owner = owner or self.make_user("admin")
        subject = Subject(name="Math", created_by=owner, requires_code=False).save()
        section = Section(subject_id=subject, title="Algebra", requires_code=False).save()
        test = Test(section_id=section, title="Quiz", created_by=owner, requires_code=False).save()
        return subject, section, test

    def login(self, user, password="secret123"):
        response = self.client.post(
            "/login",
            data={"username_or_phone": user.username, "password": password},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302, "login failed")
        return response

    @staticmethod
    def hidden_value(html, name):
        match = re.search(rf'name="{re.escape(name)}" value="([^"]*)"', html)
        return match.group(1) if match else None
