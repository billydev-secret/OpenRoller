import unittest
import uuid
from unittest.mock import patch

from riskyroller.formatters import (
    build_embed,
    build_pending_prompt_content,
    build_pending_question_summary,
    build_rolloff_embed,
)
from riskyroller.logic import run_tie_rolloff
from riskyroller.models import (
    PendingQuestionState,
    ResolutionResult,
    RiskyRollState,
    RoundResult,
)


class RiskyRollStateTests(unittest.TestCase):
    def make_state(
        self,
        *,
        rolls: dict[int, int] | None = None,
        reroll_user_ids: set[int] | None = None,
    ) -> RiskyRollState:
        return RiskyRollState(
            channel_id=123,
            guild_id=456,
            opener_id=789,
            rolls=rolls or {},
            reroll_user_ids=reroll_user_ids or set(),
        )

    def test_resolve_not_enough_when_fewer_than_two_rolls(self) -> None:
        state = self.make_state(rolls={1: 42})

        result = state.resolve()

        self.assertEqual(RoundResult.NOT_ENOUGH, result.result_type)
        self.assertTrue(state.is_open)
        self.assertIsNone(state.highest_user)
        self.assertIsNone(state.lowest_user)

    def test_resolve_waiting_for_rerolls_when_some_missing(self) -> None:
        state = self.make_state(rolls={1: 75, 3: 5}, reroll_user_ids={1, 2})

        result = state.resolve()

        self.assertEqual(RoundResult.WAITING_FOR_REROLLS, result.result_type)
        self.assertTrue(state.is_open)
        self.assertIsNone(state.highest_user)
        self.assertIsNone(state.lowest_user)

    def test_resolve_tie_when_highest_roll_is_shared(self) -> None:
        state = self.make_state(rolls={1: 88, 2: 88, 3: 10})

        result = state.resolve()

        self.assertEqual(RoundResult.TIE, result.result_type)
        self.assertEqual([1, 2], sorted(result.rolloff_user_ids))
        self.assertIsNotNone(result.rolloff_rounds)
        self.assertFalse(state.is_open)
        self.assertIsNotNone(state.highest_user)
        self.assertIsNotNone(state.lowest_user)

    def test_resolve_sixtynine_closes_round_and_sets_winner(self) -> None:
        state = self.make_state(rolls={10: 12, 20: 69, 30: 98})

        result = state.resolve()

        self.assertEqual(RoundResult.SIXTYNINE, result.result_type)
        self.assertEqual(20, state.highest_user)
        self.assertIsNone(state.lowest_user)
        self.assertFalse(state.is_open)

    def test_resolve_ok_sets_highest_lowest_and_closes_round(self) -> None:
        state = self.make_state(rolls={1: 93, 2: 18, 3: 50})

        result = state.resolve()

        self.assertEqual(RoundResult.OK, result.result_type)
        self.assertEqual(1, state.highest_user)
        self.assertEqual(2, state.lowest_user)
        self.assertFalse(state.is_open)

    def test_prepare_reroll_removes_tied_rolls_and_resets_result(self) -> None:
        state = self.make_state(rolls={1: 100, 2: 100, 3: 8})
        state.highest_user = 1
        state.lowest_user = 3

        state.prepare_reroll([1, 2])

        self.assertEqual({1, 2}, state.reroll_user_ids)
        self.assertEqual({3: 8}, state.rolls)
        self.assertIsNone(state.highest_user)
        self.assertIsNone(state.lowest_user)

    def test_can_roll_restricts_to_reroll_set_when_active(self) -> None:
        state = self.make_state(rolls={3: 45}, reroll_user_ids={1, 2})

        self.assertTrue(state.can_roll(1))
        self.assertTrue(state.can_roll(2))
        self.assertFalse(state.can_roll(3))
        self.assertFalse(state.can_roll(4))

        state.add_roll(1, 11)

        self.assertFalse(state.can_roll(1))
        self.assertTrue(state.can_roll(2))

    def test_add_roll_clears_reroll_set_once_all_rerolls_are_in(self) -> None:
        state = self.make_state(rolls={3: 40}, reroll_user_ids={1, 2})

        state.add_roll(1, 70)
        self.assertEqual({1, 2}, state.reroll_user_ids)

        state.add_roll(2, 90)
        self.assertEqual(set(), state.reroll_user_ids)

    def test_reroll_mentions_are_sorted_for_stable_output(self) -> None:
        state = self.make_state(reroll_user_ids={30, 10, 20})

        mentions = state.reroll_mentions()

        self.assertEqual("<@10>, <@20>, <@30>", mentions)

    def test_pending_reroll_mentions_shows_only_pending_users(self) -> None:
        state = self.make_state(rolls={1: 50}, reroll_user_ids={1, 2, 3})

        mentions = state.pending_reroll_mentions()

        self.assertEqual("<@2>, <@3>", mentions)

    def test_resolve_multiple_sixtynine_triggers_rolloff(self) -> None:
        state = self.make_state(rolls={1: 69, 2: 69, 3: 20})

        result = state.resolve()

        self.assertEqual(RoundResult.SIXTYNINE_TIE, result.result_type)
        self.assertEqual([1, 2], sorted(result.rolloff_user_ids))
        self.assertIsNotNone(result.rolloff_rounds)
        self.assertFalse(state.is_open)
        self.assertIsNotNone(state.highest_user)
        self.assertIn(state.highest_user, [1, 2])  # Winner is one of the 69 rollers
        self.assertIsNone(state.lowest_user)

    def test_resolve_lowest_tie_runs_rolloff(self) -> None:
        state = self.make_state(rolls={1: 100, 2: 10, 3: 10})

        result = state.resolve()

        self.assertEqual(RoundResult.OK, result.result_type)
        self.assertEqual(1, state.highest_user)
        self.assertIn(state.lowest_user, [2, 3])  # One of the tied lowest
        self.assertEqual({2, 3}, state.lowest_tie_user_ids)
        self.assertFalse(state.is_open)

    def test_resolve_with_zero_rolls(self) -> None:
        state = self.make_state(rolls={})

        result = state.resolve()

        self.assertEqual(RoundResult.NOT_ENOUGH, result.result_type)
        self.assertTrue(state.is_open)

    def test_resolve_highest_tie_with_two_players_loser_is_lowest(self) -> None:
        state = self.make_state(rolls={1: 50, 2: 50})

        result = state.resolve()

        self.assertEqual(RoundResult.TIE, result.result_type)
        self.assertIsNotNone(result.rolloff_rounds)
        self.assertFalse(state.is_open)
        # Winner and lowest should be different (loser of rolloff becomes lowest)
        self.assertNotEqual(state.highest_user, state.lowest_user)
        self.assertIn(state.highest_user, [1, 2])
        self.assertIn(state.lowest_user, [1, 2])

    def test_resolve_highest_tie_with_lowest_also_tied(self) -> None:
        state = self.make_state(rolls={1: 90, 2: 90, 3: 10, 4: 10})

        result = state.resolve()

        self.assertEqual(RoundResult.TIE, result.result_type)
        self.assertEqual([1, 2], sorted(result.rolloff_user_ids))
        self.assertIsNotNone(result.rolloff_rounds)
        self.assertFalse(state.is_open)
        # Highest winner should be one of 1 or 2
        self.assertIn(state.highest_user, [1, 2])
        # Lowest should be one of 3 or 4 (with rolloff)
        self.assertIn(state.lowest_user, [3, 4])
        self.assertEqual({3, 4}, state.lowest_tie_user_ids)

    def test_resolve_clears_reroll_user_ids_on_tie_resolution(self) -> None:
        state = self.make_state(rolls={1: 80, 2: 80, 3: 20})
        state.reroll_user_ids = {1, 2}

        result = state.resolve()

        self.assertEqual(RoundResult.TIE, result.result_type)
        self.assertEqual(set(), state.reroll_user_ids)

    def test_resolve_clears_lowest_tie_user_ids_at_start(self) -> None:
        state = self.make_state(rolls={1: 100, 2: 50})
        state.lowest_tie_user_ids = {99, 98}  # Stale data

        result = state.resolve()

        self.assertEqual(RoundResult.OK, result.result_type)
        self.assertEqual(set(), state.lowest_tie_user_ids)  # Should be cleared


class GameStatePresentationTests(unittest.TestCase):
    def test_build_embed_for_open_round_no_rolls(self) -> None:
        state = RiskyRollState(channel_id=1, guild_id=2, opener_id=3)

        embed = build_embed(state)

        self.assertEqual("Press **Roll** to join this round.", embed.description)
        self.assertEqual("Rolls (0)", embed.fields[0].name)
        self.assertEqual("No rolls yet.", embed.fields[0].value)

    def test_build_embed_for_open_reroll_waiting(self) -> None:
        state = RiskyRollState(
            channel_id=1,
            guild_id=2,
            opener_id=3,
            rolls={11: 76},
            reroll_user_ids={11, 22},
        )

        embed = build_embed(state)

        self.assertEqual("Tie for highest roll. Tied players must reroll.", embed.description)
        self.assertEqual("Reroll", embed.fields[1].name)
        self.assertIn("Tied users: <@11>, <@22>", embed.fields[1].value)
        self.assertIn("Waiting on: <@22>", embed.fields[1].value)

    def test_build_embed_for_closed_standard_result(self) -> None:
        state = RiskyRollState(
            channel_id=1,
            guild_id=2,
            opener_id=3,
            rolls={44: 80, 55: 20},
            is_open=False,
            highest_user=44,
            lowest_user=55,
        )

        embed = build_embed(state)

        self.assertEqual("Round closed.", embed.description)
        self.assertEqual("Result", embed.fields[1].name)
        self.assertEqual("<@44> asks\n<@55> answers", embed.fields[1].value)

    def test_build_embed_for_closed_standard_result_with_lowest_tie_rolloff(self) -> None:
        state = RiskyRollState(
            channel_id=1,
            guild_id=2,
            opener_id=3,
            rolls={44: 80, 55: 20, 66: 20},
            is_open=False,
            highest_user=44,
            lowest_user=55,
            lowest_tie_user_ids={55, 66},
        )

        embed = build_embed(state)

        self.assertEqual("Round closed.", embed.description)
        self.assertEqual("Result", embed.fields[1].name)
        self.assertIn("<@44> asks\n<@55> answers", embed.fields[1].value)
        self.assertIn("<@55>, <@66> -> <@55>.", embed.fields[1].value)

    def test_build_embed_for_closed_sixtynine_result(self) -> None:
        state = RiskyRollState(
            channel_id=1,
            guild_id=2,
            opener_id=3,
            rolls={99: 69, 100: 10},
            is_open=False,
            highest_user=99,
            lowest_user=None,
        )

        embed = build_embed(state)

        self.assertEqual("Round closed.", embed.description)
        self.assertEqual("Result", embed.fields[1].name)
        self.assertIn("69 rolled.", embed.fields[1].value)
        self.assertIn("<@99> wins and asks the room a question.", embed.fields[1].value)

    def test_build_pending_prompt_content_direct(self) -> None:
        state = PendingQuestionState(
            channel_id=1,
            guild_id=2,
            winner_id=10,
            participant_user_ids={30, 20},
            game_id=str(uuid.uuid4()),
            prompt_kind="direct",
        )

        content = build_pending_prompt_content(state)

        self.assertIn("<@10> won the round.", content)
        self.assertIn("<@20> <@30>", content)

    def test_build_pending_prompt_content_direct_with_lowest_tie_rolloff(self) -> None:
        state = PendingQuestionState(
            channel_id=1,
            guild_id=2,
            winner_id=10,
            participant_user_ids={20},
            game_id=str(uuid.uuid4()),
            lowest_tie_user_ids={20, 30},
            prompt_kind="direct",
        )

        content = build_pending_prompt_content(state)

        self.assertIn("<@10> won the round.", content)
        self.assertIn("<@20>, <@30> -> <@20>.", content)
        self.assertIn("Click **Ask Question** to send your question to <@20>.", content)

    def test_build_pending_prompt_content_room(self) -> None:
        state = PendingQuestionState(
            channel_id=1,
            guild_id=2,
            winner_id=10,
            participant_user_ids={10, 20, 30},
            game_id=str(uuid.uuid4()),
            prompt_kind="room",
        )

        content = build_pending_prompt_content(state)

        self.assertIn("<@10> rolled 69 and wins.", content)
        self.assertIn("everyone who rolled", content)

    def test_build_pending_question_summary_direct(self) -> None:
        state = PendingQuestionState(
            channel_id=1,
            guild_id=2,
            winner_id=42,
            participant_user_ids={8, 9},
            game_id=str(uuid.uuid4()),
            prompt_kind="direct",
        )

        summary = build_pending_question_summary(state, "How old are you?")

        self.assertEqual("<@42> asked <@8> <@9>:\nHow old are you?", summary)

    def test_build_pending_question_summary_room(self) -> None:
        state = PendingQuestionState(
            channel_id=1,
            guild_id=2,
            winner_id=42,
            participant_user_ids={8, 9, 42},
            game_id=str(uuid.uuid4()),
            prompt_kind="room",
        )

        summary = build_pending_question_summary(state, "Room question?")

        self.assertEqual("<@42> rolled 69 and asked:\nRoom question?", summary)

    def test_run_tie_rolloff_retries_until_single_winner(self) -> None:
        with patch(
            "riskyroller.logic.random.randint",
            side_effect=[50, 50, 60, 60, 99, 10],
        ):
            winner_id, rounds = run_tie_rolloff([2, 1])

        self.assertEqual(1, winner_id)
        self.assertEqual(3, len(rounds))
        self.assertEqual({1: 50, 2: 50}, rounds[0])
        self.assertEqual({1: 60, 2: 60}, rounds[1])
        self.assertEqual({1: 99, 2: 10}, rounds[2])

    def test_build_rolloff_embed_contains_rounds_and_winner(self) -> None:
        embed = build_rolloff_embed(
            tied_user_ids=[3, 1, 2],
            rounds=[{1: 70, 2: 70, 3: 42}, {1: 88, 2: 20}],
            winner_id=1,
        )

        self.assertEqual("Tie Rolloff", embed.title)
        self.assertEqual(3, len(embed.fields))
        self.assertEqual("Rolloff Round 1", embed.fields[0].name)
        self.assertEqual("Rolloff Round 2", embed.fields[1].name)
        self.assertEqual("Rolloff Winner", embed.fields[2].name)
        self.assertEqual("<@1>", embed.fields[2].value)


if __name__ == "__main__":
    unittest.main()
