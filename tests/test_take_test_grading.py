from study_platform.models import Attempt, Choice, Question, ServedTest, StudentGamification, XPEvent
from study_platform.xp import DAILY_XP_SUBMITS_PER_ITEM

from .db_case import DBTestCase


class TakeTestGradingTests(DBTestCase):
    def setUp(self):
        super().setUp()
        _, _, self.test = self.make_open_test()
        self.questions = []
        for i in range(12):
            right = Choice(text="right", is_correct=True)
            wrong = Choice(text="wrong", is_correct=False)
            q = Question(test_id=self.test, text=f"Q{i}", choices=[right, wrong], correct_choice_id=right.choice_id)
            q.save()
            self.questions.append(q)
        self.student = self.make_user("student")
        self.login(self.student)

    def _serve(self, **params):
        query = {"count": 10, **params}
        response = self.client.get(f"/tests/{self.test.id}", query_string=query)
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        token = self.hidden_value(html, "serve_token")
        self.assertTrue(token)
        served = ServedTest.objects(token=token).first()
        return token, served

    def _right_answers(self, question_ids):
        by_id = {str(q.id): q for q in self.questions}
        return {f"question_{qid}": str(by_id[qid].correct_choice_id) for qid in question_ids}

    def _submit(self, token, data=None):
        form = {"serve_token": token, **(data or {})}
        return self.client.post(f"/tests/{self.test.id}", data=form, follow_redirects=False)

    def test_grades_the_served_questions_not_the_submitted_list(self):
        token, served = self._serve()
        self.assertEqual(len(served.question_ids), 10)
        first = served.question_ids[0]

        # Tampered form: claims only one question was asked and answers it right.
        response = self._submit(token, {"question_ids": first, **self._right_answers([first])})
        self.assertEqual(response.status_code, 302)

        attempt = Attempt.objects(student_id=self.student.id).first()
        self.assertEqual(attempt.total, 10)
        self.assertEqual(attempt.score, 1)

    def test_all_right_answers_score_full_marks(self):
        token, served = self._serve()
        self._submit(token, self._right_answers(served.question_ids))
        attempt = Attempt.objects(student_id=self.student.id).first()
        self.assertEqual((attempt.score, attempt.total), (10, 10))
        self.assertGreater(attempt.xp_earned, 0)

    def test_a_sitting_can_only_be_submitted_once(self):
        token, served = self._serve()
        self._submit(token, self._right_answers(served.question_ids))
        second = self._submit(token, self._right_answers(served.question_ids))
        self.assertEqual(second.status_code, 302)
        self.assertIn(f"/tests/{self.test.id}", second.headers["Location"])
        self.assertEqual(Attempt.objects(student_id=self.student.id).count(), 1)

    def test_submit_without_a_served_sitting_is_rejected(self):
        response = self._submit("not-a-real-token", self._right_answers([str(self.questions[0].id)]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Attempt.objects(student_id=self.student.id).count(), 0)

    def test_another_students_sitting_cannot_be_submitted(self):
        token, served = self._serve()
        other = self.make_user("student")
        self.client.get("/logout")
        self.login(other)
        self._submit(token, self._right_answers(served.question_ids))
        self.assertEqual(Attempt.objects(student_id=other.id).count(), 0)

    def test_preset_question_ids_need_the_students_own_attempt(self):
        one = str(self.questions[0].id)
        _, served = self._serve(question_ids=one)
        # Not a retake of their own attempt: the preset is ignored, a normal 10-question set is served.
        self.assertEqual(len(served.question_ids), 10)

    def test_retake_same_questions_keeps_the_original_set(self):
        token, served = self._serve()
        self._submit(token, self._right_answers(served.question_ids))
        attempt = Attempt.objects(student_id=self.student.id).first()

        response = self.client.post(f"/results/{attempt.id}/retake/same")
        self.assertEqual(response.status_code, 302)
        retake_page = self.client.get(response.headers["Location"])
        retake_token = self.hidden_value(retake_page.get_data(as_text=True), "serve_token")
        retake = ServedTest.objects(token=retake_token).first()
        self.assertEqual(sorted(retake.question_ids), sorted(served.question_ids))
        self.assertTrue(retake.is_retake)

    def test_xp_stops_after_the_daily_limit_for_one_test(self):
        for _ in range(DAILY_XP_SUBMITS_PER_ITEM + 2):
            token, served = self._serve()
            self._submit(token, self._right_answers(served.question_ids))

        attempts = list(Attempt.objects(student_id=self.student.id).order_by("submitted_at"))
        rewarded = [a for a in attempts if a.xp_earned > 0]
        self.assertEqual(len(attempts), DAILY_XP_SUBMITS_PER_ITEM + 2)
        self.assertEqual(len(rewarded), DAILY_XP_SUBMITS_PER_ITEM)
        profile = StudentGamification.objects(student_id=self.student.id).first()
        self.assertEqual(profile.xp_total, sum(e.xp for e in XPEvent.objects(student_id=self.student.id)))
