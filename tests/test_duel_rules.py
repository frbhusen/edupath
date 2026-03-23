import unittest
from datetime import datetime, timedelta, timezone

from study_platform.student import _duel_compute_settlement_plan


class DuelSettlementPlanTests(unittest.TestCase):
    def test_higher_score_wins(self):
        now = datetime.now(timezone.utc)
        plan = _duel_compute_settlement_plan(
            challenger_score=12,
            opponent_score=10,
            challenger_finished_at=now,
            opponent_finished_at=now + timedelta(seconds=30),
            challenger_streak_before=0,
            opponent_streak_before=0,
        )
        self.assertEqual(plan["winner_slot"], "challenger")
        self.assertEqual(plan["loser_slot"], "opponent")

    def test_tie_breaker_by_faster_finish(self):
        now = datetime.now(timezone.utc)
        plan = _duel_compute_settlement_plan(
            challenger_score=9,
            opponent_score=9,
            challenger_finished_at=now + timedelta(seconds=20),
            opponent_finished_at=now,
            challenger_streak_before=2,
            opponent_streak_before=3,
        )
        self.assertEqual(plan["winner_slot"], "opponent")

    def test_tie_breaker_defaults_to_challenger_if_equal_finish(self):
        now = datetime.now(timezone.utc)
        plan = _duel_compute_settlement_plan(
            challenger_score=8,
            opponent_score=8,
            challenger_finished_at=now,
            opponent_finished_at=now,
            challenger_streak_before=0,
            opponent_streak_before=0,
        )
        self.assertEqual(plan["winner_slot"], "challenger")

    def test_base_win_xp_30_below_10_streak(self):
        plan = _duel_compute_settlement_plan(
            challenger_score=10,
            opponent_score=9,
            challenger_finished_at=None,
            opponent_finished_at=None,
            challenger_streak_before=9,
            opponent_streak_before=0,
        )
        self.assertEqual(plan["base_win_xp"], 30)

    def test_base_win_xp_35_on_10_plus_streak(self):
        plan = _duel_compute_settlement_plan(
            challenger_score=10,
            opponent_score=9,
            challenger_finished_at=None,
            opponent_finished_at=None,
            challenger_streak_before=10,
            opponent_streak_before=0,
        )
        self.assertEqual(plan["base_win_xp"], 35)

    def test_streak_bonus_thresholds(self):
        p5 = _duel_compute_settlement_plan(10, 9, None, None, 4, 0)
        p7 = _duel_compute_settlement_plan(10, 9, None, None, 6, 0)
        p10 = _duel_compute_settlement_plan(10, 9, None, None, 9, 0)
        self.assertEqual(p5["streak_bonus"], 30)
        self.assertEqual(p7["streak_bonus"], 50)
        self.assertEqual(p10["streak_bonus"], 75)

    def test_loser_penalty_for_10_plus_streak(self):
        plan = _duel_compute_settlement_plan(
            challenger_score=9,
            opponent_score=10,
            challenger_finished_at=None,
            opponent_finished_at=None,
            challenger_streak_before=12,
            opponent_streak_before=2,
        )
        self.assertEqual(plan["loser_slot"], "challenger")
        self.assertEqual(plan["loser_penalty"], -5)


if __name__ == "__main__":
    unittest.main()
