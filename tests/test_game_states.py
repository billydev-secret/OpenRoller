import unittest
import uuid
from unittest.mock import Mock, patch

import discord

from riskyroller import state as app_state
from riskyroller.formatters import (
    NOTICE_EMBED_COLOR,
    build_embed,
    build_how_to_play_embed,
    build_pending_prompt_content,
    build_pending_question_summary,
    build_question_reply_embed,
    build_rolloff_embed,
    format_lowest_rolloff_note,
)
from riskyroller.logic import effective_min_game_seconds, missing_start_permissions, run_tie_rolloff
from riskyroller.models import (
    PendingQuestionState,
    PostedQuestionState,
    PromptKind,
    RiskyRollState,
    RoundResult,
)


class RiskyRollStateTests(unittest.TestCase):
    def make_state(self, *, rolls: dict[int, int] | None = None) -> RiskyRollState:
        return RiskyRollState(
            channel_id=123,
            guild_id=456,
            opener_id=789,
            rolls=rolls or {},
        )

    def test_resolve_not_enough_when_fewer_than_two_rolls(self) -> None:
        state = self.make_state(rolls={1: 42})

        result = state.resolve()

        self.assertEqual(RoundResult.NOT_ENOUGH, result.result_type)
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

    def test_can_roll_is_one_roll_per_player(self) -> None:
        state = self.make_state(rolls={3: 45})

        self.assertFalse(state.can_roll(3))
        self.assertTrue(state.can_roll(4))

        state.add_roll(4, 11)

        self.assertFalse(state.can_roll(4))

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

    def test_resolve_clears_lowest_tie_user_ids_at_start(self) -> None:
        state = self.make_state(rolls={1: 100, 2: 50})
        state.lowest_tie_user_ids = {99, 98}  # Stale data

        result = state.resolve()

        self.assertEqual(RoundResult.OK, result.result_type)
        self.assertEqual(set(), state.lowest_tie_user_ids)  # Should be cleared

    def test_resolve_clears_every_tie_set_at_start(self) -> None:
        state = self.make_state(rolls={1: 90, 2: 50})
        state.highest_tie_user_ids = {97}
        state.second_lowest_tie_user_ids = {96}
        state.second_highest_tie_user_ids = {95}

        state.resolve()

        self.assertEqual(set(), state.highest_tie_user_ids)
        self.assertEqual(set(), state.second_lowest_tie_user_ids)
        self.assertEqual(set(), state.second_highest_tie_user_ids)

    def test_resolve_highest_tie_records_who_was_in_the_rolloff(self) -> None:
        state = self.make_state(rolls={1: 88, 2: 88, 3: 10})

        state.resolve()

        self.assertEqual({1, 2}, state.highest_tie_user_ids)
        self.assertIn(state.highest_user, {1, 2})

    def test_resolve_sixtynine_tie_records_who_was_in_the_rolloff(self) -> None:
        state = self.make_state(rolls={1: 69, 2: 69, 3: 20})

        state.resolve()

        self.assertEqual({1, 2}, state.highest_tie_user_ids)

    def test_resolve_unique_highest_leaves_highest_tie_set_empty(self) -> None:
        state = self.make_state(rolls={1: 93, 2: 18, 3: 50})

        state.resolve()

        self.assertEqual(set(), state.highest_tie_user_ids)

    def test_100_rule_records_second_lowest_tie(self) -> None:
        state = self.make_state(rolls={1: 100, 2: 5, 3: 20, 4: 20})

        state.resolve()

        self.assertEqual(1, state.highest_user)
        self.assertEqual(2, state.lowest_user)
        self.assertIn(state.second_lowest_user, {3, 4})
        self.assertEqual({3, 4}, state.second_lowest_tie_user_ids)
        self.assertEqual(set(), state.second_highest_tie_user_ids)

    def test_1_rule_records_second_highest_tie(self) -> None:
        state = self.make_state(rolls={1: 1, 2: 90, 3: 50, 4: 50})

        state.resolve()

        self.assertEqual(2, state.highest_user)
        self.assertEqual(1, state.lowest_user)
        self.assertIn(state.second_highest_user, {3, 4})
        self.assertEqual({3, 4}, state.second_highest_tie_user_ids)
        self.assertEqual(set(), state.second_lowest_tie_user_ids)

    def test_second_extreme_without_a_tie_leaves_tie_set_empty(self) -> None:
        state = self.make_state(rolls={1: 100, 2: 5, 3: 20, 4: 30})

        state.resolve()

        self.assertEqual(3, state.second_lowest_user)
        self.assertEqual(set(), state.second_lowest_tie_user_ids)


class GameStatePresentationTests(unittest.TestCase):
    def test_build_embed_for_open_round_no_rolls(self) -> None:
        state = RiskyRollState(channel_id=1, guild_id=2, opener_id=3)

        embed = build_embed(state)

        self.assertIn("Press **Roll** to join", embed.description or "")
        self.assertEqual("Rolls (0)", embed.fields[0].name)
        self.assertEqual("No rolls yet.", embed.fields[0].value)

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

        self.assertEqual("Round over.", embed.description)
        self.assertEqual("Result", embed.fields[1].name)
        self.assertIn("**Asks:** <@44>", embed.fields[1].value or "")
        self.assertIn("**Answers:** <@55>", embed.fields[1].value or "")

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

        self.assertEqual("Round over.", embed.description)
        self.assertEqual("Result", embed.fields[1].name)
        self.assertIn("**Asks:** <@44>", embed.fields[1].value or "")
        self.assertIn("**Answers:** <@55>", embed.fields[1].value or "")
        self.assertIn("<@55>, <@66> → <@55>", embed.fields[1].value or "")

    def test_build_embed_shows_highest_rolloff_note(self) -> None:
        state = RiskyRollState(
            channel_id=1,
            guild_id=2,
            opener_id=3,
            rolls={44: 80, 55: 80, 66: 20},
            is_open=False,
            highest_user=44,
            lowest_user=66,
            highest_tie_user_ids={44, 55},
        )

        embed = build_embed(state)

        self.assertIn("<@44>, <@55> → <@44>", embed.fields[1].value or "")

    def test_build_embed_shows_second_extreme_rolloff_notes(self) -> None:
        state = RiskyRollState(
            channel_id=1,
            guild_id=2,
            opener_id=3,
            rolls={1: 100, 2: 5, 3: 20, 4: 20},
            is_open=False,
            highest_user=1,
            lowest_user=2,
            second_lowest_user=3,
            second_lowest_tie_user_ids={3, 4},
        )

        embed = build_embed(state)

        value = embed.fields[1].value or ""
        self.assertIn("**Answers:** <@2> and <@3>", value)
        self.assertIn("<@3>, <@4> → <@3>", value)

    def test_build_embed_for_closed_sixtynine_tie_shows_rolloff_note(self) -> None:
        state = RiskyRollState(
            channel_id=1,
            guild_id=2,
            opener_id=3,
            rolls={99: 69, 98: 69, 100: 10},
            is_open=False,
            highest_user=99,
            lowest_user=None,
            highest_tie_user_ids={98, 99},
        )

        embed = build_embed(state)

        value = embed.fields[1].value or ""
        self.assertIn("**Answers:** the room", value)
        self.assertIn("<@98>, <@99> → <@99>", value)

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

        self.assertEqual("Round over.", embed.description)
        self.assertEqual("Result", embed.fields[1].name)
        self.assertIn("**Asks:** <@99>", embed.fields[1].value or "")
        self.assertIn("**Answers:** the room", embed.fields[1].value or "")

    def test_build_pending_prompt_content_direct(self) -> None:
        state = PendingQuestionState(
            channel_id=1,
            guild_id=2,
            winner_id=10,
            participant_user_ids={30, 20},
            game_id=str(uuid.uuid4()),
            prompt_kind=PromptKind.DIRECT,
        )

        content = build_pending_prompt_content(state)

        self.assertIn("<@10> wins the round.", content)
        self.assertIn("<@20> <@30>", content)

    def test_build_pending_prompt_content_direct_with_lowest_tie_rolloff(self) -> None:
        state = PendingQuestionState(
            channel_id=1,
            guild_id=2,
            winner_id=10,
            participant_user_ids={20},
            game_id=str(uuid.uuid4()),
            lowest_tie_user_ids={20, 30},
            prompt_kind=PromptKind.DIRECT,
        )

        content = build_pending_prompt_content(state)

        self.assertIn("<@10> wins the round.", content)
        self.assertIn("<@20>, <@30> → <@20>", content)
        self.assertIn("Click **Ask Question** to send your question to <@20>.", content)

    def test_build_pending_prompt_content_room(self) -> None:
        state = PendingQuestionState(
            channel_id=1,
            guild_id=2,
            winner_id=10,
            participant_user_ids={10, 20, 30},
            game_id=str(uuid.uuid4()),
            prompt_kind=PromptKind.ROOM,
        )

        content = build_pending_prompt_content(state)

        self.assertIn("<@10> rolled **69**", content)
        self.assertIn("they ask the room", content)

    def test_build_pending_question_summary_direct(self) -> None:
        state = PendingQuestionState(
            channel_id=1,
            guild_id=2,
            winner_id=42,
            participant_user_ids={8, 9},
            game_id=str(uuid.uuid4()),
            prompt_kind=PromptKind.DIRECT,
        )

        summary = build_pending_question_summary(state, "How old are you?")

        self.assertEqual("<@42> asked <@8> <@9>:\n> How old are you?", summary)

    def test_build_pending_question_summary_room(self) -> None:
        state = PendingQuestionState(
            channel_id=1,
            guild_id=2,
            winner_id=42,
            participant_user_ids={8, 9, 42},
            game_id=str(uuid.uuid4()),
            prompt_kind=PromptKind.ROOM,
        )

        summary = build_pending_question_summary(state, "Room question?")

        self.assertEqual("<@42> rolled 69 and asked:\n> Room question?", summary)

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

        self.assertIn("Tie Rolloff", embed.title or "")
        self.assertEqual(3, len(embed.fields))
        self.assertEqual("Round 1", embed.fields[0].name)
        self.assertEqual("Round 2", embed.fields[1].name)
        self.assertIn("Rolloff Winner", embed.fields[2].name)
        self.assertEqual("<@1>", embed.fields[2].value)


class RosterNameTests(unittest.TestCase):
    def setUp(self) -> None:
        app_state.display_names.clear()
        self.addCleanup(app_state.display_names.clear)

    def closed_state(self) -> RiskyRollState:
        return RiskyRollState(
            channel_id=1,
            guild_id=2,
            opener_id=3,
            rolls={44: 80, 55: 20},
            is_open=False,
            highest_user=44,
            lowest_user=55,
        )

    def test_roster_and_result_use_cached_display_names(self) -> None:
        app_state.display_names.update({44: "Alice", 55: "Bob"})

        embed = build_embed(self.closed_state())

        self.assertIn("**80** — Alice", embed.fields[0].value or "")
        self.assertIn("**20** — Bob", embed.fields[0].value or "")
        self.assertIn("**Asks:** Alice", embed.fields[1].value or "")
        self.assertIn("**Answers:** Bob", embed.fields[1].value or "")

    def test_unknown_player_falls_back_to_a_mention(self) -> None:
        app_state.display_names[44] = "Alice"

        embed = build_embed(self.closed_state())

        self.assertIn("**Asks:** Alice", embed.fields[1].value or "")
        self.assertIn("**Answers:** <@55>", embed.fields[1].value or "")

    def test_live_guild_member_wins_and_is_memoised(self) -> None:
        app_state.display_names[44] = "Old Name"
        member = Mock()
        member.display_name = "Live Alice"
        guild = Mock()
        guild.get_member.side_effect = lambda uid: member if uid == 44 else None

        embed = build_embed(self.closed_state(), guild)

        self.assertIn("**Asks:** Live Alice", embed.fields[1].value or "")
        self.assertIn("**Answers:** <@55>", embed.fields[1].value or "")
        self.assertEqual("Live Alice", app_state.display_names[44])

    def test_display_name_markdown_is_escaped(self) -> None:
        app_state.display_names[44] = "_al*ice_"

        embed = build_embed(self.closed_state())

        self.assertIn("\\_al\\*ice\\_", embed.fields[1].value or "")

    def test_rolloff_note_uses_the_name_resolver(self) -> None:
        app_state.display_names.update({44: "Alice", 55: "Bob"})
        state = self.closed_state()
        state.rolls = {44: 80, 55: 80, 66: 20}
        state.lowest_user = 66
        state.highest_tie_user_ids = {44, 55}

        embed = build_embed(state)

        self.assertIn("Alice, Bob → Alice", embed.fields[1].value or "")

    def test_format_lowest_rolloff_note_defaults_to_mentions(self) -> None:
        self.assertEqual("<@1>, <@2> → <@2>", format_lowest_rolloff_note({1, 2}, 2))
        self.assertEqual("A, B → B", format_lowest_rolloff_note({1, 2}, 2, {1: "A", 2: "B"}.__getitem__))


class MinGameTimeTests(unittest.TestCase):
    def test_unset_guild_uses_default(self) -> None:
        self.assertEqual(1800, effective_min_game_seconds({}, 1, False, 1800))

    def test_configured_value_wins_over_default(self) -> None:
        self.assertEqual(60, effective_min_game_seconds({1: 60}, 1, False, 1800))

    def test_zero_disables_rather_than_falling_back(self) -> None:
        self.assertEqual(0, effective_min_game_seconds({1: 0}, 1, False, 1800))

    def test_skip_flag_wins_outright(self) -> None:
        self.assertEqual(0, effective_min_game_seconds({1: 60}, 1, True, 1800))
        self.assertEqual(0, effective_min_game_seconds({}, 1, True, 1800))


class StartPermissionTests(unittest.TestCase):
    def test_all_present_is_empty(self) -> None:
        perms = discord.Permissions(view_channel=True, send_messages=True, embed_links=True)

        self.assertEqual([], missing_start_permissions(perms))

    def test_view_channel_is_reported_first(self) -> None:
        perms = discord.Permissions(send_messages=True, embed_links=True)

        self.assertEqual(["View Channel"], missing_start_permissions(perms))

    def test_everything_missing_lists_all_three(self) -> None:
        self.assertEqual(
            ["View Channel", "Send Messages", "Embed Links"],
            missing_start_permissions(discord.Permissions.none()),
        )

    def test_unrelated_permissions_do_not_matter(self) -> None:
        perms = discord.Permissions(view_channel=True, send_messages=True, embed_links=True, administrator=False)

        self.assertEqual([], missing_start_permissions(perms))


class QuestionReplyEmbedTests(unittest.TestCase):
    def make_state(self, **overrides) -> PostedQuestionState:
        defaults = dict(
            message_id=12345,
            channel_id=100,
            guild_id=200,
            asker_id=10,
            allowed_replier_ids={20},
            question_text="What is your favorite color?",
        )
        defaults.update(overrides)
        return PostedQuestionState(**defaults)  # type: ignore[arg-type]

    def _field(self, embed, name):
        for field in embed.fields:
            if field.name == name:
                return field
        self.fail(f"Field {name!r} not found in embed")

    def test_standard_case_no_special_markers(self) -> None:
        state = self.make_state()

        embed = build_question_reply_embed(state, replier_id=20, reply_text="Blue.")

        self.assertEqual("🎲 Question", embed.title)
        self.assertEqual("<@10>", self._field(embed, "Asks").value)
        self.assertEqual("<@20>", self._field(embed, "Answers").value)
        self.assertEqual("> What is your favorite color?", self._field(embed, "Question").value)
        self.assertEqual("> Blue.", self._field(embed, "Reply").value)

    def test_100_case_shows_star_on_asker_and_both_targets(self) -> None:
        state = self.make_state(
            allowed_replier_ids={20, 30},
            asker_rolled_100=True,
        )

        embed = build_question_reply_embed(state, replier_id=20, reply_text="Blue.")

        self.assertEqual("<@10> ⭐", self._field(embed, "Asks").value)
        self.assertEqual("<@20> and <@30>", self._field(embed, "Answers").value)
        self.assertEqual("<@20>\n> Blue.", self._field(embed, "Reply").value)

    def test_1_case_shows_skull_on_target(self) -> None:
        state = self.make_state(target_rolled_1=True)

        embed = build_question_reply_embed(state, replier_id=20, reply_text="Red.")

        self.assertEqual("<@10>", self._field(embed, "Asks").value)
        self.assertEqual("<@20> ☠️", self._field(embed, "Answers").value)
        self.assertEqual("> Red.", self._field(embed, "Reply").value)

    def test_single_target_reply_does_not_prepend_replier_mention(self) -> None:
        state = self.make_state()

        embed = build_question_reply_embed(state, replier_id=20, reply_text="Hi.")

        self.assertEqual("> Hi.", self._field(embed, "Reply").value)


class HowToPlayEmbedTests(unittest.TestCase):
    def test_title_mentions_how_to_play(self) -> None:
        embed = build_how_to_play_embed()

        self.assertIn("How to Play", embed.title)

    def test_description_covers_core_rules(self) -> None:
        embed = build_how_to_play_embed()

        description = embed.description or ""
        self.assertIn("Roll", description)
        self.assertIn("69", description)
        self.assertIn("100", description)
        self.assertIn("Rolled 1", description)

    def test_uses_notice_embed_color(self) -> None:
        embed = build_how_to_play_embed()

        self.assertEqual(NOTICE_EMBED_COLOR, embed.color)


if __name__ == "__main__":
    unittest.main()
