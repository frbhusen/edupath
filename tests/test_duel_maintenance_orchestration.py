import unittest
from unittest.mock import patch

from study_platform import student as duel_module


class _FakeDuel:
    def __init__(self, status):
        self.status = status


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def order_by(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def all(self):
        return list(self._rows)


class DuelMaintenanceOrchestrationTests(unittest.TestCase):
    def test_dispatches_each_status_to_correct_handler(self):
        d_pending = _FakeDuel("pending")
        d_waiting = _FakeDuel("accepted_waiting")
        d_live = _FakeDuel("live")
        d_done = _FakeDuel("completed")

        rows = [d_pending, d_waiting, d_live, d_done]

        with patch.object(duel_module.Duel, "objects", return_value=_FakeQuery(rows)) as objects_mock, patch.object(
            duel_module, "_duel_expire_if_needed"
        ) as expire_mock, patch.object(
            duel_module, "_duel_expire_waiting_if_needed"
        ) as waiting_expire_mock, patch.object(
            duel_module, "_duel_autosubmit_timeout"
        ) as autosubmit_mock, patch.object(
            duel_module, "_duel_try_settle"
        ) as settle_mock:
            duel_module._duel_maintenance_tick_for_student("student-1")

            objects_mock.assert_called_once()
            expire_mock.assert_called_once_with(d_pending)
            waiting_expire_mock.assert_called_once_with(d_waiting)
            autosubmit_mock.assert_called_once_with(d_live)
            settle_mock.assert_called_once_with(d_done)

    def test_no_query_when_missing_student_id(self):
        with patch.object(duel_module.Duel, "objects") as objects_mock:
            duel_module._duel_maintenance_tick_for_student(None)
            objects_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
