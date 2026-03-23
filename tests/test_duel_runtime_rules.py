import unittest
from datetime import datetime, timedelta, timezone

from study_platform.student import (
    _duel_pair_lock_remaining_from_latest,
    _duel_should_apply_finish_penalty,
)


class DuelRuntimeRulesTests(unittest.TestCase):
    def test_finish_penalty_applies_when_20_or_more(self):
        self.assertTrue(_duel_should_apply_finish_penalty(20))
        self.assertTrue(_duel_should_apply_finish_penalty(45))

    def test_finish_penalty_not_applied_below_20(self):
        self.assertFalse(_duel_should_apply_finish_penalty(19))
        self.assertFalse(_duel_should_apply_finish_penalty(0))

    def test_pair_lock_full_for_active_status(self):
        now = datetime.now(timezone.utc)
        self.assertEqual(
            _duel_pair_lock_remaining_from_latest(now, "pending", now - timedelta(seconds=1000)),
            45,
        )
        self.assertEqual(
            _duel_pair_lock_remaining_from_latest(now, "accepted_waiting", now - timedelta(seconds=1000)),
            45,
        )
        self.assertEqual(
            _duel_pair_lock_remaining_from_latest(now, "live", now - timedelta(seconds=1000)),
            45,
        )

    def test_pair_lock_counts_down_for_closed_status(self):
        now = datetime.now(timezone.utc)
        remaining = _duel_pair_lock_remaining_from_latest(
            now,
            "completed",
            now - timedelta(seconds=10),
        )
        self.assertEqual(remaining, 35)

    def test_pair_lock_zero_when_expired(self):
        now = datetime.now(timezone.utc)
        remaining = _duel_pair_lock_remaining_from_latest(
            now,
            "declined",
            now - timedelta(seconds=120),
        )
        self.assertEqual(remaining, 0)


if __name__ == "__main__":
    unittest.main()
