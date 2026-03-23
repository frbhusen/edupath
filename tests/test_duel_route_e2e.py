import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from datetime import datetime

from flask import Flask

from study_platform import student as duel_module


class _RefUser:
    def __init__(self, user_id):
        self.id = user_id


class _FakeDuel:
    def __init__(self, duel_id, challenger_id, opponent_id):
        self.id = duel_id
        self.challenger_id = _RefUser(challenger_id)
        self.opponent_id = _RefUser(opponent_id)

        self.status = "pending"
        self.ended_at = None
        self.fee_applied = False
        self.invite_consumed = False
        self.entry_fee_xp = 20

        self.question_ids_json = "[]"
        self.challenger_submitted = False
        self.opponent_submitted = False
        self.challenger_joined_at: datetime | None = None
        self.opponent_joined_at: datetime | None = None
        self.challenger_finished_at = None
        self.opponent_finished_at = None
        self.challenger_score = 0
        self.opponent_score = 0
        self.first_submitter_slot = None
        self.first_submitter_perfect = False
        self.second_submitter_perfect = False
        self.challenger_penalty_seconds = 0
        self.opponent_penalty_seconds = 0

        self.saved = 0

    def save(self):
        self.saved += 1


class _SingleResultQuery:
    def __init__(self, result):
        self._result = result

    def first(self):
        return self._result


class _AllQuery:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _DeleteQuery:
    def __init__(self):
        self.deleted = 0

    def delete(self):
        self.deleted += 1


class _FakeQuestion:
    def __init__(self, qid, correct_choice_id):
        self.id = qid
        self.correct_choice_id = correct_choice_id


class _FakeDuelAnswer:
    saved_rows = []
    delete_query = _DeleteQuery()

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def save(self):
        _FakeDuelAnswer.saved_rows.append(self.kwargs)

    @classmethod
    def objects(cls, **_kwargs):
        return cls.delete_query


class DuelRouteE2ETests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask("duel-route-tests")
        cls.app.config["SECRET_KEY"] = "test-secret"
        cls.app.register_blueprint(duel_module.student_bp)

    def setUp(self):
        _FakeDuelAnswer.saved_rows = []
        _FakeDuelAnswer.delete_query = _DeleteQuery()

    @staticmethod
    def _raw_view(fn):
        return getattr(fn, "__wrapped__", fn)

    def test_duel_respond_decline_flow(self):
        duel_id = "64b64b64b64b64b64b64b64b"
        duel = _FakeDuel(duel_id, "challenger-1", "student-1")
        duel.status = "pending"

        with self.app.test_request_context(f"/duels/{duel_id}/respond", method="POST", data={"action": "decline"}), patch.object(
            duel_module, "current_user", SimpleNamespace(id="student-1", role="student", is_authenticated=True)
        ), patch.object(duel_module.Duel, "objects", return_value=_SingleResultQuery(duel)), patch.object(
            duel_module, "_duel_expire_if_needed", return_value=False
        ), patch.object(duel_module, "_duel_apply_xp_delta_once") as xp_mock:
            resp = self._raw_view(duel_module.duel_respond)(duel_id)

            self.assertEqual(resp.status_code, 302)
            self.assertEqual(duel.status, "declined")
            xp_mock.assert_not_called()

    def test_duel_respond_accept_flow_deducts_fee(self):
        duel_id = "74b64b64b64b64b64b64b64b"
        duel = _FakeDuel(duel_id, "challenger-2", "student-2")
        duel.status = "pending"

        profile = SimpleNamespace(xp_total=120)

        with self.app.test_request_context(f"/duels/{duel_id}/respond", method="POST", data={"action": "accept"}), patch.object(
            duel_module, "current_user", SimpleNamespace(id="student-2", role="student", is_authenticated=True)
        ), patch.object(duel_module.Duel, "objects", return_value=_SingleResultQuery(duel)), patch.object(
            duel_module, "_duel_expire_if_needed", return_value=False
        ), patch.object(duel_module, "_get_or_create_gamification_profile", return_value=profile), patch.object(
            duel_module, "_duel_apply_xp_delta_once"
        ) as xp_mock:
            resp = self._raw_view(duel_module.duel_respond)(duel_id)

            self.assertEqual(resp.status_code, 302)
            self.assertEqual(duel.status, "accepted_waiting")
            self.assertTrue(duel.fee_applied)
            self.assertTrue(duel.invite_consumed)
            self.assertEqual(xp_mock.call_count, 2)
            self.assertTrue(all(call.args[3] == -20 for call in xp_mock.call_args_list))

    def test_duel_submit_applies_15_second_penalty_when_not_perfect(self):
        duel_id = "84b64b64b64b64b64b64b64b"
        qid_1 = "54b64b64b64b64b64b64b64b"
        qid_2 = "56b64b64b64b64b64b64b64b"
        cid_1 = "64a64b64b64b64b64b64b64b"
        cid_2 = "66a64b64b64b64b64b64b64b"

        duel = _FakeDuel(duel_id, "student-3", "opponent-3")
        duel.status = "live"
        duel.challenger_joined_at = datetime.utcnow()
        duel.question_ids_json = json.dumps([qid_1, qid_2])
        duel.opponent_submitted = False

        q1 = _FakeQuestion(qid=qid_1, correct_choice_id=cid_1)
        q2 = _FakeQuestion(qid=qid_2, correct_choice_id=cid_2)

        with self.app.test_request_context(f"/duels/{duel_id}/submit", method="POST", data={f"q_{qid_1}": cid_1}), patch.object(
            duel_module, "current_user", SimpleNamespace(id="student-3", role="student", is_authenticated=True)
        ), patch.object(duel_module.Duel, "objects", return_value=_SingleResultQuery(duel)), patch.object(
            duel_module, "_duel_autosubmit_timeout"
        ), patch.object(duel_module.Question, "objects", return_value=_AllQuery([q1, q2])), patch.object(
            duel_module, "DuelAnswer", _FakeDuelAnswer
        ), patch.object(duel_module, "_duel_time_left_seconds", side_effect=[120, 25]), patch.object(
            duel_module, "_duel_try_settle"
        ) as settle_mock:
            resp = self._raw_view(duel_module.duel_submit)(duel_id)

            self.assertEqual(resp.status_code, 302)
            self.assertTrue(duel.challenger_submitted)
            self.assertEqual(duel.challenger_score, 1)
            self.assertEqual(duel.opponent_penalty_seconds, 15)
            self.assertFalse(duel.first_submitter_perfect)
            self.assertEqual(duel.status, "live")
            settle_mock.assert_not_called()

    def test_duel_submit_perfect_first_applies_60_second_penalty(self):
        duel_id = "85b64b64b64b64b64b64b64b"
        qid = "57b64b64b64b64b64b64b64b"
        cid = "67a64b64b64b64b64b64b64b"

        duel = _FakeDuel(duel_id, "student-3b", "opponent-3b")
        duel.status = "live"
        duel.challenger_joined_at = datetime.utcnow()
        duel.question_ids_json = json.dumps([qid])
        duel.opponent_submitted = False

        q = _FakeQuestion(qid=qid, correct_choice_id=cid)

        with self.app.test_request_context(f"/duels/{duel_id}/submit", method="POST", data={f"q_{qid}": cid}), patch.object(
            duel_module, "current_user", SimpleNamespace(id="student-3b", role="student", is_authenticated=True)
        ), patch.object(duel_module.Duel, "objects", return_value=_SingleResultQuery(duel)), patch.object(
            duel_module, "_duel_autosubmit_timeout"
        ), patch.object(duel_module.Question, "objects", return_value=_AllQuery([q])), patch.object(
            duel_module, "DuelAnswer", _FakeDuelAnswer
        ), patch.object(duel_module, "_duel_time_left_seconds", return_value=120), patch.object(
            duel_module, "_duel_try_settle"
        ) as settle_mock:
            resp = self._raw_view(duel_module.duel_submit)(duel_id)

            self.assertEqual(resp.status_code, 302)
            self.assertTrue(duel.challenger_submitted)
            self.assertEqual(duel.challenger_score, 1)
            self.assertEqual(duel.opponent_penalty_seconds, 60)
            self.assertEqual(duel.first_submitter_slot, "challenger")
            self.assertTrue(duel.first_submitter_perfect)
            self.assertEqual(duel.status, "live")
            settle_mock.assert_not_called()

    def test_duel_submit_completes_and_settles_when_both_submitted(self):
        duel_id = "94b64b64b64b64b64b64b64b"
        qid = "65b64b64b64b64b64b64b64b"
        cid = "75a64b64b64b64b64b64b64b"

        duel = _FakeDuel(duel_id, "student-4", "opponent-4")
        duel.status = "live"
        duel.challenger_joined_at = datetime.utcnow()
        duel.question_ids_json = json.dumps([qid])
        duel.opponent_submitted = True

        question = _FakeQuestion(qid=qid, correct_choice_id=cid)

        with self.app.test_request_context(f"/duels/{duel_id}/submit", method="POST", data={f"q_{qid}": cid}), patch.object(
            duel_module, "current_user", SimpleNamespace(id="student-4", role="student", is_authenticated=True)
        ), patch.object(duel_module.Duel, "objects", return_value=_SingleResultQuery(duel)), patch.object(
            duel_module, "_duel_autosubmit_timeout"
        ), patch.object(duel_module.Question, "objects", return_value=_AllQuery([question])), patch.object(
            duel_module, "DuelAnswer", _FakeDuelAnswer
        ), patch.object(duel_module, "_duel_time_left_seconds", return_value=100), patch.object(
            duel_module, "_duel_try_settle"
        ) as settle_mock:
            resp = self._raw_view(duel_module.duel_submit)(duel_id)

            self.assertEqual(resp.status_code, 302)
            self.assertEqual(duel.status, "completed")
            settle_mock.assert_called_once_with(duel)


if __name__ == "__main__":
    unittest.main()
