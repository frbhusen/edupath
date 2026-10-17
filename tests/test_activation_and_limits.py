from unittest.mock import patch

from study_platform import teacher
from study_platform.models import (
    CourseAttempt,
    CourseQuestion,
    CourseSet,
    Subject,
    SubjectActivation,
    SubjectActivationCode,
)
from study_platform.student import DAILY_XP_SUBMITS_PER_ITEM

from .db_case import DBTestCase


class ActivationCodeTests(DBTestCase):
    def setUp(self):
        super().setUp()
        admin = self.make_user("admin")
        self.subject = Subject(name="Physics", created_by=admin, requires_code=True).save()
        self.code = SubjectActivationCode(subject_id=self.subject, code="ABCD2345").save()

    def _activate(self, code):
        return self.client.post(f"/subjects/{self.subject.id}/activate", data={"code": code})

    def test_a_code_activates_once(self):
        first = self.make_user("student")
        self.login(first)
        response = self._activate("abcd2345")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(SubjectActivation.objects(subject_id=self.subject.id, student_id=first.id).count(), 1)

        self.client.get("/logout")
        second = self.make_user("student")
        self.login(second)
        self._activate("ABCD2345")
        self.assertEqual(SubjectActivation.objects(subject_id=self.subject.id, student_id=second.id).count(), 0)

    def test_code_claimed_between_check_and_save_is_not_reused(self):
        # Simulates the race: another request claims the code right after this
        # request has read it as unused.
        student = self.make_user("student")
        self.login(student)
        real_objects = SubjectActivationCode.objects

        def claim_first(*args, **kwargs):
            result = real_objects(*args, **kwargs)
            if "code" in kwargs:
                SubjectActivationCode._get_collection().update_one({"_id": self.code.id}, {"$set": {"is_used": True}})
            return result

        with patch.object(SubjectActivationCode, "objects", side_effect=claim_first):
            self._activate("ABCD2345")
        self.assertEqual(SubjectActivation.objects(student_id=student.id).count(), 0)

    def test_old_six_character_codes_still_work(self):
        SubjectActivationCode(subject_id=self.subject, code="A1B2C3").save()
        student = self.make_user("student")
        self.login(student)
        self._activate("a1b2c3")
        self.assertEqual(SubjectActivation.objects(student_id=student.id).count(), 1)

    def test_generated_codes_are_long_and_unambiguous(self):
        codes = {teacher._generate_unique_code(SubjectActivationCode) for _ in range(50)}
        self.assertEqual(len(codes), 50)
        for code in codes:
            self.assertEqual(len(code), 8)
            self.assertFalse(set(code) & set("01OI"))


class RateLimitTests(DBTestCase):
    rate_limits = True

    def test_activation_attempts_are_limited_per_account(self):
        admin = self.make_user("admin")
        subject = Subject(name="Chem", created_by=admin, requires_code=True).save()
        student = self.make_user("student")
        self.login(student)
        statuses = [
            self.client.post(f"/subjects/{subject.id}/activate", data={"code": f"WRONG{i:03d}"}).status_code
            for i in range(11)
        ]
        self.assertEqual(statuses[:10], [200] * 10)
        self.assertEqual(statuses[10], 303)

    def test_login_attempts_are_limited_per_account(self):
        user = self.make_user("student")
        statuses = [
            self.client.post("/login", data={"username_or_phone": user.username, "password": "bad"}).status_code
            for _ in range(11)
        ]
        self.assertEqual(statuses[:10], [200] * 10)
        self.assertEqual(statuses[10], 303)

        # Other accounts are not affected.
        other = self.make_user("student")
        self.login(other)


class CourseXPTests(DBTestCase):
    def test_course_xp_stops_after_the_daily_limit(self):
        admin = self.make_user("admin")
        subject = Subject(name="Bio", created_by=admin, requires_code=False).save()
        course = CourseSet(subject_id=subject, title="Ministry 2025", created_by=admin, xp_per_question=2).save()
        q = CourseQuestion(course_set_id=course, question_type="interactive", question_text="?").save()
        student = self.make_user("student")
        self.login(student)

        with patch("study_platform.student._course_set_open_for_student", return_value=True):
            for _ in range(DAILY_XP_SUBMITS_PER_ITEM + 1):
                self.client.post(f"/courses/{course.id}/submit", data={f"question_{q.id}": "true"})

        rewarded = [a.xp_earned for a in CourseAttempt.objects(student_id=student.id)]
        self.assertEqual(len(rewarded), DAILY_XP_SUBMITS_PER_ITEM + 1)
        self.assertEqual(sum(1 for xp in rewarded if xp > 0), DAILY_XP_SUBMITS_PER_ITEM)


class LeaderboardStreamTests(DBTestCase):
    def test_stream_returns_one_snapshot_and_closes(self):
        student = self.make_user("student")
        self.login(student)
        response = self.client.get("/leaderboard/stream")
        body = response.get_data(as_text=True)  # would hang forever with the old endless stream
        self.assertTrue(body.startswith("retry: 15000"))
        self.assertIn("event: leaderboard", body)
