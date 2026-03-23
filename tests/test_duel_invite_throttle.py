import unittest
from datetime import datetime, timedelta, timezone

from study_platform.student import _duel_invite_throttle_decision


class DuelInviteThrottleTests(unittest.TestCase):
    def test_blocks_on_pending_limit(self):
        now = datetime.now(timezone.utc)
        decision = _duel_invite_throttle_decision(
            now=now,
            pending_count=3,
            latest_any_created_at=None,
            latest_same_created_at=None,
        )
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["reason"], "pending_limit")

    def test_blocks_on_global_cooldown(self):
        now = datetime.now(timezone.utc)
        decision = _duel_invite_throttle_decision(
            now=now,
            pending_count=0,
            latest_any_created_at=now - timedelta(seconds=15),
            latest_same_created_at=None,
        )
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["reason"], "global_cooldown")
        self.assertGreaterEqual(int(decision["remaining"] or 0), 1)

    def test_blocks_on_same_opponent_cooldown(self):
        now = datetime.now(timezone.utc)
        decision = _duel_invite_throttle_decision(
            now=now,
            pending_count=0,
            latest_any_created_at=now - timedelta(seconds=500),
            latest_same_created_at=now - timedelta(seconds=20),
        )
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["reason"], "same_opponent_cooldown")
        self.assertGreaterEqual(int(decision["remaining"] or 0), 1)

    def test_allows_when_limits_passed(self):
        now = datetime.now(timezone.utc)
        decision = _duel_invite_throttle_decision(
            now=now,
            pending_count=0,
            latest_any_created_at=now - timedelta(seconds=120),
            latest_same_created_at=now - timedelta(seconds=500),
        )
        self.assertTrue(decision["allowed"])
        self.assertIsNone(decision["reason"])


if __name__ == "__main__":
    unittest.main()
