import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
import warnings

from study_platform import student as duel_module

warnings.filterwarnings("ignore", category=DeprecationWarning, message=r"datetime\.datetime\.utcnow\(\) is deprecated.*")


class _DummyDuel:
    def __init__(self, **kwargs):
        self.status = kwargs.get("status", "pending")
        self.expires_at = kwargs.get("expires_at")
        self.ended_at = kwargs.get("ended_at")
        self.started_at = kwargs.get("started_at")
        self.challenger_joined_at = kwargs.get("challenger_joined_at")
        self.opponent_joined_at = kwargs.get("opponent_joined_at")
        self.created_at = kwargs.get("created_at", datetime.utcnow())
        self.fee_applied = kwargs.get("fee_applied", False)

        self.challenger_submitted = kwargs.get("challenger_submitted", False)
        self.opponent_submitted = kwargs.get("opponent_submitted", False)
        self.challenger_finished_at = kwargs.get("challenger_finished_at")
        self.opponent_finished_at = kwargs.get("opponent_finished_at")
        self.challenger_score = kwargs.get("challenger_score", 0)
        self.opponent_score = kwargs.get("opponent_score", 0)

        self.saved = 0

    def save(self):
        self.saved += 1


class DuelLifecycleE2ESimTests(unittest.TestCase):
    def test_pending_invite_expires(self):
        duel = _DummyDuel(
            status="pending",
            expires_at=datetime.utcnow() - timedelta(minutes=1),
        )
        changed = duel_module._duel_expire_if_needed(duel)
        self.assertTrue(changed)
        self.assertEqual(duel.status, "expired")
        self.assertIsNotNone(duel.ended_at)
        self.assertGreaterEqual(duel.saved, 1)

    def test_waiting_duel_expires_and_refunds(self):
        duel = _DummyDuel(
            status="accepted_waiting",
            fee_applied=True,
            created_at=datetime.utcnow() - timedelta(seconds=600),
        )
        with patch.object(duel_module, "_duel_refund_entry_if_needed") as refund_mock:
            changed = duel_module._duel_expire_waiting_if_needed(duel)
            self.assertTrue(changed)
            self.assertEqual(duel.status, "expired")
            refund_mock.assert_called_once_with(duel)

    def test_waiting_duel_not_expired_before_timeout(self):
        duel = _DummyDuel(
            status="accepted_waiting",
            fee_applied=True,
            created_at=datetime.utcnow() - timedelta(seconds=30),
        )
        with patch.object(duel_module, "_duel_refund_entry_if_needed") as refund_mock:
            changed = duel_module._duel_expire_waiting_if_needed(duel)
            self.assertFalse(changed)
            self.assertEqual(duel.status, "accepted_waiting")
            refund_mock.assert_not_called()

    def test_live_duel_autocompletes_on_both_timeouts(self):
        duel = _DummyDuel(status="live")

        def fake_time_left(_duel, slot, now=None):
            return 0 if slot in {"challenger", "opponent"} else 999

        with patch.object(duel_module, "_duel_time_left_seconds", side_effect=fake_time_left), patch.object(
            duel_module, "_duel_try_settle"
        ) as settle_mock:
            duel_module._duel_autosubmit_timeout(duel)
            self.assertTrue(duel.challenger_submitted)
            self.assertTrue(duel.opponent_submitted)
            self.assertEqual(duel.status, "completed")
            settle_mock.assert_called_once_with(duel)

    def test_live_duel_not_complete_if_one_side_still_has_time(self):
        duel = _DummyDuel(status="live")

        def fake_time_left(_duel, slot, now=None):
            if slot == "challenger":
                return 0
            if slot == "opponent":
                return 120
            return 999

        with patch.object(duel_module, "_duel_time_left_seconds", side_effect=fake_time_left), patch.object(
            duel_module, "_duel_try_settle"
        ) as settle_mock:
            duel_module._duel_autosubmit_timeout(duel)
            self.assertTrue(duel.challenger_submitted)
            self.assertFalse(duel.opponent_submitted)
            self.assertEqual(duel.status, "live")
            settle_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
