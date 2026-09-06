import unittest
import uuid
from unittest.mock import Mock, patch

import discord

from riskyroller import state as app_state
from riskyroller.formatters import (
    build_embed,
    build_how_to_play_content,
    build_pending_prompt_content,
    build_pending_question_summary,
    build_question_reply_content,
    auto_close_hint,
    failure_reason,
    format_duration,
    format_lowest_rolloff_note,
    format_room_mentions,
    join_names,
    permission_help,
)
from riskyroller.logic import (
    REQUIRED_THREAD_PERMISSIONS,
    effective_min_game_seconds,
    missing_permissions,
    missing_start_permissions,
    run_tie_rolloff,
)
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

    def test_build_pending_prompt_content_direct_omits_the_rolloff_note(self) -> None:
        # This prompt used to print a "tied → selected" note, but the state it
        # is built from doesn't carry the round's lowest_user: it named
        # whichever tied player had the smaller id, so it was wrong whenever
        # the rolloff picked the other one and contradicted the round embed
        # posted just above it. The embed does know, so the note lives there.
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
        self.assertNotIn("→", content)
        self.assertNotIn("<@30>", content)
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



class RosterNameTests(unittest.TestCase):
    def setUp(self) -> None:
        app_state.guild_display_names.clear()
        self.addCleanup(app_state.guild_display_names.clear)

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
        app_state.guild_display_names.update({(2, 44): "Alice", (2, 55): "Bob"})

        embed = build_embed(self.closed_state())

        self.assertIn("**80** — Alice", embed.fields[0].value or "")
        self.assertIn("**20** — Bob", embed.fields[0].value or "")
        self.assertIn("**Asks:** Alice", embed.fields[1].value or "")
        self.assertIn("**Answers:** Bob", embed.fields[1].value or "")

    def test_unknown_player_falls_back_to_a_mention(self) -> None:
        app_state.guild_display_names[(2, 44)] = "Alice"

        embed = build_embed(self.closed_state())

        self.assertIn("**Asks:** Alice", embed.fields[1].value or "")
        self.assertIn("**Answers:** <@55>", embed.fields[1].value or "")

    def test_live_guild_member_wins_and_is_memoised(self) -> None:
        app_state.guild_display_names[(2, 44)] = "Old Name"
        member = Mock()
        member.display_name = "Live Alice"
        guild = Mock()
        guild.get_member.side_effect = lambda uid: member if uid == 44 else None

        embed = build_embed(self.closed_state(), guild)

        self.assertIn("**Asks:** Live Alice", embed.fields[1].value or "")
        self.assertIn("**Answers:** <@55>", embed.fields[1].value or "")
        self.assertEqual("Live Alice", app_state.guild_display_names[(2, 44)])

    def test_display_name_markdown_is_escaped(self) -> None:
        app_state.guild_display_names[(2, 44)] = "_al*ice_"

        embed = build_embed(self.closed_state())

        self.assertIn("\\_al\\*ice\\_", embed.fields[1].value or "")

    def test_rolloff_note_uses_the_name_resolver(self) -> None:
        app_state.guild_display_names.update({(2, 44): "Alice", (2, 55): "Bob"})
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

    def test_thread_permissions_are_checked_separately(self) -> None:
        perms = discord.Permissions(view_channel=True, send_messages=True, embed_links=True)

        self.assertEqual(
            ["Create Public Threads", "Send Messages in Threads"],
            missing_permissions(perms, REQUIRED_THREAD_PERMISSIONS),
        )
        self.assertEqual([], missing_start_permissions(perms))


class RefusalCopyHelperTests(unittest.TestCase):
    def test_format_duration(self) -> None:
        cases = [
            (0, "0 seconds"), (1, "1 second"), (45, "45 seconds"), (60, "1 minute"),
            (90, "1 minute 30 seconds"), (1800, "30 minutes"), (3600, "1 hour"),
            (3900, "1 hour 5 minutes"), (7200, "2 hours"), (-5, "0 seconds"),
        ]
        for seconds, expected in cases:
            with self.subTest(seconds=seconds):
                self.assertEqual(expected, format_duration(seconds))

    def test_room_mentions_are_capped_so_the_post_always_fits(self) -> None:
        small = {3, 1, 2}
        big = set(range(1, 81))

        self.assertEqual("<@1> <@2> <@3>", format_room_mentions(small))
        capped = format_room_mentions(big)
        self.assertTrue(capped.endswith(" and 30 more"))
        self.assertEqual(50, capped.count("<@"))
        # Worst case: 50 max-length snowflake mentions plus a 300-char question stays under 2,000.
        worst = format_room_mentions({10**18 + i for i in range(80)})
        self.assertLess(len(worst) + len("\n<@1000000000000000000> asks:\n") + 300, 2000)

    def test_join_names(self) -> None:
        self.assertEqual("", join_names([]))
        self.assertEqual("a", join_names(["a"]))
        self.assertEqual("a and b", join_names(["a", "b"]))
        self.assertEqual("a, b and c", join_names(["a", "b", "c"]))

    def test_auto_close_hint_states_the_real_settings(self) -> None:
        opened = 1_000_000.0
        both = RiskyRollState(
            channel_id=1, guild_id=2, opener_id=3, auto_close_players=25, auto_close_minutes=120, created_at=opened
        )
        players = RiskyRollState(channel_id=1, guild_id=2, opener_id=3, auto_close_players=4)
        minutes = RiskyRollState(channel_id=1, guild_id=2, opener_id=3, auto_close_minutes=1, created_at=opened)
        neither = RiskyRollState(channel_id=1, guild_id=2, opener_id=3)

        # The minutes deadline counts from when the round opened, so it is a
        # relative Discord timestamp rather than a duration that goes stale.
        self.assertEqual(
            f"It auto-closes once 25 players have rolled or <t:{int(opened) + 7200}:R>, whichever comes first.",
            auto_close_hint(both),
        )
        self.assertEqual("It auto-closes once 4 players have rolled.", auto_close_hint(players))
        self.assertEqual(f"It auto-closes <t:{int(opened) + 60}:R>.", auto_close_hint(minutes))
        self.assertEqual("", auto_close_hint(neither))

    def test_auto_close_hint_after_the_threshold_is_met(self) -> None:
        state = RiskyRollState(
            channel_id=1, guild_id=2, opener_id=3, auto_close_players=2, auto_close_minutes=120,
            rolls={10: 50, 20: 60},
        )

        self.assertEqual(
            "Enough players have rolled — it closes itself once the minimum time is up.",
            auto_close_hint(state),
        )

    def test_permission_help_names_the_permission_and_where_to_grant_it(self) -> None:
        one = permission_help(["View Channel"])
        three = permission_help(["View Channel", "Send Messages", "Embed Links"])

        self.assertIn("I'm missing View Channel in this channel. An admin can grant it under", one)
        self.assertIn("Server Settings", one)
        self.assertIn("Edit Channel", one)
        self.assertIn("I'm missing View Channel, Send Messages and Embed Links in this channel. An admin can grant them", three)
        self.assertEqual("", permission_help([]))

    def test_failure_reason_does_not_blame_permissions_the_bot_has(self) -> None:
        held = discord.Permissions(view_channel=True, send_messages=True, embed_links=True)
        lacking = discord.Permissions(send_messages=True, embed_links=True)

        self.assertIn("permissions here look right", failure_reason(held))
        self.assertNotIn("Server Settings", failure_reason(held))
        self.assertIn("I'm missing View Channel", failure_reason(lacking))






class QuestionReplyContentTests(unittest.TestCase):
    """The live reply text. Its embed-building predecessor had these tests
    while it had no production callers at all; the text that players actually
    read had none."""

    def make_state(self, **kwargs) -> PostedQuestionState:
        defaults = dict(
            message_id=1,
            channel_id=2,
            guild_id=3,
            asker_id=10,
            allowed_replier_ids={20},
            question_text="What is your favorite color?",
        )
        defaults.update(kwargs)
        return PostedQuestionState(**defaults)

    def test_names_the_targets_the_asker_and_both_texts(self) -> None:
        content = build_question_reply_content(self.make_state(), replier_id=20, reply_text="Blue.")

        self.assertIn("<@20>", content)
        self.assertIn("<@10> asks:", content)
        self.assertIn("What is your favorite color?", content)
        self.assertIn("<@20>: Blue.", content)

    def test_both_targets_are_named_when_the_100_rule_applied(self) -> None:
        state = self.make_state(allowed_replier_ids={20, 30}, asker_rolled_100=True)

        content = build_question_reply_content(state, replier_id=20, reply_text="Blue.")

        self.assertIn("<@20>", content)
        self.assertIn("<@30>", content)
        self.assertIn("<@20>: Blue.", content)


class HowToPlayContentTests(unittest.TestCase):
    """The live How to Play text — what the button actually posts."""

    def test_covers_every_rule_the_round_can_apply(self) -> None:
        content = build_how_to_play_content()

        self.assertIn("How to Play", content)
        for fragment in ("Roll", "69", "100", "Rolled 1", "Close"):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, content)

    def test_says_a_69_beats_every_other_roll(self) -> None:
        # resolve() short-circuits on a 69 before it ever computes a highest
        # or lowest, so a 69 wins over a 100 and the round has no loser at
        # all. "Highest roll wins" on its own reads as the opposite.
        content = build_how_to_play_content()

        self.assertIn("Beats every other roll", content)


class SecondExtremeIsValueBasedTests(unittest.TestCase):
    """The extra asker/answerer is chosen by what someone rolled, not by where
    they landed once the two extremes are removed. A rolloff loser who rolled
    the winning value is not a member of the bottom 2, and someone who rolled
    the losing value is not a member of the top 2."""

    def _resolved(self, rolls: dict[int, int], *, winner: int) -> RiskyRollState:
        state = RiskyRollState(channel_id=1, guild_id=2, opener_id=1, rolls=rolls)
        # Pin the rolloff so the assertions below are about the selection rule
        # rather than about which tied player chance happened to pick.
        with patch("riskyroller.models.run_tie_rolloff", return_value=(winner, [])):
            state.resolve()
        return state

    def test_a_rolloff_loser_who_rolled_100_is_not_an_extra_answerer(self) -> None:
        # 100 / 100 / 40: player 20 lost the rolloff for the win, so they are
        # not the winner — but they rolled a 100 and must not be asked as one
        # of the bottom 2 with a skull beside their name.
        state = self._resolved({10: 100, 20: 100, 30: 40}, winner=10)

        self.assertEqual(10, state.highest_user)
        self.assertEqual(30, state.lowest_user)
        self.assertIsNone(state.second_lowest_user)

    def test_a_rolloff_loser_who_rolled_1_is_not_an_extra_asker(self) -> None:
        # 90 / 1 / 1: player 20 survived the rolloff for the loss, but they
        # still rolled a 1 and are not one of the top 2.
        state = self._resolved({10: 90, 20: 1, 30: 1}, winner=30)

        self.assertEqual(10, state.highest_user)
        self.assertEqual(30, state.lowest_user)
        self.assertIsNone(state.second_highest_user)

    def test_a_genuine_third_player_is_still_chosen(self) -> None:
        # Nobody tied at either end, so the rule applies exactly as before.
        state = RiskyRollState(channel_id=1, guild_id=2, opener_id=1, rolls={10: 100, 20: 50, 30: 40})
        state.resolve()

        self.assertEqual(20, state.second_lowest_user)


if __name__ == "__main__":
    unittest.main()
