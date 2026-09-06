import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord

from riskyroller import state as app_state
from riskyroller.commands import (
    MAX_ROUND_LIFETIME_MINUTES,
    SETUP_FAILED_TEXT,
    _reset_channel_state,
    _set_max_games_per_channel,
    _start_game,
)
from riskyroller.config import DEFAULT_MAX_GAMES_PER_CHANNEL
from riskyroller.invite import invite_url
from riskyroller.models import RiskyRollState


def _fake_store(*method_names: str) -> Mock:
    """A store double with the given methods as AsyncMocks.

    Every test that can reach app_state.store must use one of these instead
    of the real (file-backed) store.
    """
    return Mock(**{name: AsyncMock() for name in method_names})


def _permissive_interaction(*, guild_id: int = 200, channel_id: int = 100, user_id: int = 1) -> Mock:
    """A Mock interaction shaped like an ordinary, cached, fully-permitted call.

    ``interaction.client.get_guild`` returning the guild itself is what a real
    interaction looks like for a guild the bot is actually in (see
    StartGameGuardTests) — the shape every test below needs unless it's
    specifically exercising that guard.
    """
    interaction = Mock()
    interaction.guild.id = guild_id
    # A real text channel: _start_game refuses anything that isn't a text
    # channel or thread, since the auto-close path can't re-find any other
    # kind by id.
    interaction.channel = Mock(spec=discord.TextChannel)
    interaction.channel.id = channel_id
    interaction.user.id = user_id
    interaction.client.get_guild.return_value = interaction.guild
    interaction.app_permissions = Mock(view_channel=True, send_messages=True, embed_links=True)
    interaction.response.is_done.return_value = False
    interaction.response.send_message = AsyncMock()
    interaction.original_response = AsyncMock(return_value=Mock(id=999))
    return interaction


class StartGameGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        app_state.active_games.clear()
        self.addCleanup(app_state.active_games.clear)

    async def test_uncached_guild_is_refused_with_a_hedged_message(self) -> None:
        # A commands-only install (applications.commands scope, no bot
        # account) never gets a GUILD_CREATE for the guild, so the client's
        # gateway cache never has an entry for it. guild.me is NOT a usable
        # signal for this: discord.py synthesises it onto every interaction
        # regardless of install shape (Interaction._from_data adds it from
        # the client's own user whenever it's missing — see
        # discord/interactions.py), so a Mock that merely sets guild.me =
        # None never exercises the real precondition and would pass even
        # with the guard deleted.
        fake_store = _fake_store("save_round")
        with (
            patch.object(app_state, "store", fake_store),
            patch("riskyroller.commands.arm_auto_close", Mock()),
        ):
            interaction = Mock()
            interaction.guild.id = 200
            interaction.channel = Mock(spec=discord.TextChannel)
            interaction.channel.id = 100
            interaction.client.get_guild.return_value = None
            interaction.client.application_id = 4242
            interaction.app_permissions = Mock(view_channel=True, send_messages=True, embed_links=True)
            interaction.response.is_done.return_value = False
            interaction.response.send_message = AsyncMock()
            interaction.original_response = AsyncMock(return_value=Mock(id=999))

            await _start_game(
                interaction, auto_close_players=25, auto_close_minutes=120, ping=True, skip_min_game_time=False
            )

        interaction.client.get_guild.assert_called_once_with(200)
        text = interaction.response.send_message.await_args.args[0]
        self.assertIn("don't see a bot account", text)
        self.assertIn(invite_url(4242), text)
        self.assertEqual({}, app_state.active_games)

    async def test_cached_guild_is_allowed_to_start_a_round(self) -> None:
        # The other half of the same precondition: a normal, cached guild —
        # the shape every real interaction has for a guild the bot is
        # genuinely in — must not be refused by the guard above.
        fake_store = _fake_store("save_round")
        with (
            patch.object(app_state, "store", fake_store),
            patch("riskyroller.commands.arm_auto_close", Mock()),
        ):
            interaction = _permissive_interaction()
            await _start_game(
                interaction, auto_close_players=25, auto_close_minutes=120, ping=False, skip_min_game_time=False
            )
            # Let the scheduled auto_close_tasks entry (a mock, so this
            # finishes at once) run rather than leaving it pending.
            await asyncio.sleep(0)

        interaction.client.get_guild.assert_called_once_with(interaction.guild.id)
        interaction.response.send_message.assert_awaited_once()
        self.assertEqual(1, len(app_state.active_games))


class AutoCloseNormalizationTests(unittest.IsolatedAsyncioTestCase):
    """Both close paths off must never happen: nothing else ever ends a round
    (no message/channel-delete listener, no sweep of active_rounds, and
    setup_hook restores it across every restart).
    """

    def setUp(self) -> None:
        app_state.active_games.clear()
        self.addCleanup(app_state.active_games.clear)
        self.fake_store = _fake_store("save_round")
        store_patcher = patch.object(app_state, "store", self.fake_store)
        store_patcher.start()
        self.addCleanup(store_patcher.stop)
        # arm_auto_close is synchronous: it creates the task and registers
        # the done-callback, so a plain Mock is the right stand-in.
        self.schedule_mock = Mock()
        schedule_patcher = patch("riskyroller.commands.arm_auto_close", self.schedule_mock)
        schedule_patcher.start()
        self.addCleanup(schedule_patcher.stop)

    async def _start(self, players: int | None, minutes: int | None) -> RiskyRollState:
        interaction = _permissive_interaction()
        await _start_game(
            interaction, auto_close_players=players, auto_close_minutes=minutes, ping=False, skip_min_game_time=False
        )
        # Let any auto_close_tasks entry actually run (it's a mock, so this
        # finishes at once) rather than leaving it pending when the test ends.
        await asyncio.sleep(0)
        return next(iter(app_state.active_games.values()))

    async def test_one_player_is_below_the_floor_and_does_not_arm(self) -> None:
        state = await self._start(1, 120)
        self.assertIsNone(state.auto_close_players)
        self.assertEqual(120, state.auto_close_minutes)

    async def test_two_players_is_the_floor_and_arms(self) -> None:
        state = await self._start(2, None)
        self.assertEqual(2, state.auto_close_players)
        self.assertIsNone(state.auto_close_minutes)

    async def test_zero_minutes_disables_the_minutes_close(self) -> None:
        state = await self._start(25, 0)
        self.assertEqual(25, state.auto_close_players)
        self.assertIsNone(state.auto_close_minutes)

    async def test_negative_minutes_disables_the_minutes_close(self) -> None:
        state = await self._start(25, -5)
        self.assertIsNone(state.auto_close_minutes)

    async def test_positive_minutes_arms_the_minutes_close(self) -> None:
        state = await self._start(25, 45)
        self.assertEqual(45, state.auto_close_minutes)

    async def test_both_disabled_falls_back_to_the_lifetime_ceiling(self) -> None:
        state = await self._start(0, 0)
        self.assertIsNone(state.auto_close_players)
        self.assertEqual(MAX_ROUND_LIFETIME_MINUTES, state.auto_close_minutes)
        self.schedule_mock.assert_called_once()
        self.assertEqual(MAX_ROUND_LIFETIME_MINUTES * 60, self.schedule_mock.call_args.args[2])

    async def test_both_none_falls_back_to_the_lifetime_ceiling(self) -> None:
        state = await self._start(None, None)
        self.assertIsNone(state.auto_close_players)
        self.assertEqual(MAX_ROUND_LIFETIME_MINUTES, state.auto_close_minutes)


class StartGamePermissionsTests(unittest.IsolatedAsyncioTestCase):
    """The permission refusal is tested at the helper level (test_game_states,
    via missing_start_permissions) and here, where _start_game actually
    consults it — deleting the block from _start_game left the rest of the
    suite green.
    """

    def setUp(self) -> None:
        app_state.active_games.clear()
        self.addCleanup(app_state.active_games.clear)

    async def test_missing_permission_is_refused_inside_start_game(self) -> None:
        fake_store = _fake_store("save_round")
        with (
            patch.object(app_state, "store", fake_store),
            patch("riskyroller.commands.arm_auto_close", Mock()),
        ):
            interaction = _permissive_interaction()
            interaction.app_permissions = Mock(view_channel=True, send_messages=False, embed_links=True)

            await _start_game(
                interaction, auto_close_players=25, auto_close_minutes=120, ping=False, skip_min_game_time=False
            )

        interaction.response.send_message.assert_awaited_once()
        text = interaction.response.send_message.await_args.args[0]
        self.assertIn("can't run a round here", text)
        self.assertEqual({}, app_state.active_games)


class StartGameCapTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        app_state.active_games.clear()
        app_state.max_games_per_channel.clear()
        self.addCleanup(app_state.active_games.clear)
        self.addCleanup(app_state.max_games_per_channel.clear)

    async def test_channel_at_its_configured_cap_is_refused(self) -> None:
        app_state.max_games_per_channel[300] = 2
        app_state.active_games["g1"] = RiskyRollState(channel_id=100, guild_id=300, opener_id=1, game_id="g1")
        app_state.active_games["g2"] = RiskyRollState(channel_id=100, guild_id=300, opener_id=1, game_id="g2")

        # Defensive: this round is expected to be refused before it ever
        # touches the store, but a broken cap comparison must fail on a
        # clean assertion below rather than an incidental real-DB error.
        fake_store = _fake_store("save_round")
        with (
            patch.object(app_state, "store", fake_store),
            patch("riskyroller.commands.arm_auto_close", Mock()),
        ):
            interaction = _permissive_interaction(guild_id=300, channel_id=100)
            await _start_game(
                interaction, auto_close_players=25, auto_close_minutes=120, ping=False, skip_min_game_time=False
            )

        text = interaction.response.send_message.await_args.args[0]
        self.assertIn("already has 2 open round", text)
        self.assertEqual(2, len(app_state.active_games))
        fake_store.save_round.assert_not_called()

    async def test_channel_below_the_default_cap_is_allowed(self) -> None:
        # No per-guild override set for this guild: falls back to
        # DEFAULT_MAX_GAMES_PER_CHANNEL, which one open round is well under.
        fake_store = _fake_store("save_round")
        with (
            patch.object(app_state, "store", fake_store),
            patch("riskyroller.commands.arm_auto_close", Mock()),
        ):
            interaction = _permissive_interaction(guild_id=301, channel_id=101)
            await _start_game(
                interaction, auto_close_players=25, auto_close_minutes=120, ping=False, skip_min_game_time=False
            )
            await asyncio.sleep(0)

        interaction.response.send_message.assert_awaited_once()
        self.assertEqual(1, len(app_state.active_games))
        self.assertNotIn(301, app_state.max_games_per_channel)


class StartGameFailureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        app_state.active_games.clear()
        self.addCleanup(app_state.active_games.clear)

    async def test_a_failed_start_pops_the_game_deletes_the_row_and_disables_the_message(self) -> None:
        fake_store = _fake_store("save_round", "delete_round")
        fake_store.save_round = AsyncMock(side_effect=RuntimeError("db is gone"))
        with patch.object(app_state, "store", fake_store):
            interaction = _permissive_interaction()
            # The initial response succeeded and the failure happens after
            # (persisting the round), so by the time the except block runs
            # the interaction really is done.
            interaction.response.is_done = Mock(return_value=True)
            failing_message = interaction.original_response.return_value
            failing_message.edit = AsyncMock()

            with self.assertRaises(RuntimeError):
                await _start_game(
                    interaction, auto_close_players=25, auto_close_minutes=120, ping=False, skip_min_game_time=False
                )

        self.assertEqual({}, app_state.active_games)
        fake_store.delete_round.assert_awaited_once()
        failing_message.edit.assert_awaited_once()
        self.assertEqual(SETUP_FAILED_TEXT, failing_message.edit.await_args.kwargs["content"])

    async def test_a_failing_delete_round_does_not_mask_the_original_error_or_skip_cleanup(self) -> None:
        fake_store = _fake_store("save_round", "delete_round")
        fake_store.delete_round = AsyncMock(side_effect=RuntimeError("store boom"))
        with patch.object(app_state, "store", fake_store):
            interaction = _permissive_interaction()
            interaction.response.send_message = AsyncMock(side_effect=RuntimeError("original boom"))
            interaction.response.is_done = Mock(return_value=False)

            with self.assertRaises(RuntimeError) as ctx:
                await _start_game(
                    interaction, auto_close_players=25, auto_close_minutes=120, ping=False, skip_min_game_time=False
                )

        self.assertEqual("original boom", str(ctx.exception))
        self.assertEqual({}, app_state.active_games)
        fake_store.delete_round.assert_awaited_once()


class SetMaxGamesCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        app_state.max_games_per_channel.clear()
        self.addCleanup(app_state.max_games_per_channel.clear)

    async def test_zero_resets_the_configured_cap_to_the_default(self) -> None:
        app_state.max_games_per_channel[400] = 3
        fake_store = _fake_store("set_max_games_per_channel")
        with patch.object(app_state, "store", fake_store):
            interaction = Mock()
            interaction.guild.id = 400
            interaction.response.is_done.return_value = False
            interaction.response.send_message = AsyncMock()

            await _set_max_games_per_channel(interaction, 0)

        self.assertNotIn(400, app_state.max_games_per_channel)
        fake_store.set_max_games_per_channel.assert_awaited_once_with(400, None)
        text = interaction.response.send_message.await_args.args[0]
        self.assertIn(str(DEFAULT_MAX_GAMES_PER_CHANNEL), text)

    async def test_positive_count_sets_the_configured_cap(self) -> None:
        fake_store = _fake_store("set_max_games_per_channel")
        with patch.object(app_state, "store", fake_store):
            interaction = Mock()
            interaction.guild.id = 401
            interaction.response.is_done.return_value = False
            interaction.response.send_message = AsyncMock()

            await _set_max_games_per_channel(interaction, 5)

        self.assertEqual(5, app_state.max_games_per_channel[401])
        fake_store.set_max_games_per_channel.assert_awaited_once_with(401, 5)


class ResetChannelStateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        app_state.active_games.clear()
        app_state.pending_questions.clear()
        app_state.posted_questions.clear()
        app_state.auto_close_tasks.clear()
        self.addCleanup(app_state.active_games.clear)
        self.addCleanup(app_state.pending_questions.clear)
        self.addCleanup(app_state.posted_questions.clear)
        self.addCleanup(app_state.auto_close_tasks.clear)

    def _interaction(self, channel_id: int) -> Mock:
        interaction = Mock()
        interaction.channel.id = channel_id
        interaction.response.defer = AsyncMock()
        # A deferred interaction reads as done for the rest of its life.
        interaction.response.is_done.return_value = True
        interaction.followup.send = AsyncMock()
        return interaction

    async def test_defers_before_doing_any_cleanup_work(self) -> None:
        # Up to dozens of message edits can happen before any reply is sent;
        # deferring first is what keeps that under Discord's 3-second
        # initial-response window.
        interaction = self._interaction(channel_id=100)

        await _reset_channel_state(interaction)

        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.followup.send.assert_awaited_once()
        interaction.response.send_message.assert_not_called()

    async def test_only_clears_the_calling_channels_rounds(self) -> None:
        fake_store = _fake_store("delete_round", "delete_pending_question", "delete_posted_question")
        with patch.object(app_state, "store", fake_store):
            app_state.active_games["here"] = RiskyRollState(channel_id=100, guild_id=1, opener_id=1, game_id="here")
            app_state.active_games["elsewhere"] = RiskyRollState(
                channel_id=200, guild_id=1, opener_id=1, game_id="elsewhere"
            )

            interaction = self._interaction(channel_id=100)
            await _reset_channel_state(interaction)

        self.assertNotIn("here", app_state.active_games)
        self.assertIn("elsewhere", app_state.active_games)
        fake_store.delete_round.assert_awaited_once_with("here")


class StartGameChannelTypeTests(unittest.IsolatedAsyncioTestCase):
    """A round can only live where every later step can re-find its channel.

    get_text_channel accepts a text channel or a thread and nothing else, so a
    round opened in a voice or stage channel's built-in chat took rolls
    normally and was then destroyed by auto-close with no result posted.
    """

    def setUp(self) -> None:
        app_state.active_games.clear()
        self.addCleanup(app_state.active_games.clear)

    async def test_voice_channel_chat_is_refused_before_anything_is_created(self) -> None:
        interaction = _permissive_interaction()
        interaction.channel = Mock(spec=discord.VoiceChannel)
        interaction.channel.id = 100
        fake_store = _fake_store("save_round")

        with patch.object(app_state, "store", fake_store):
            await _start_game(
                interaction,
                auto_close_players=25,
                auto_close_minutes=120,
                ping=False,
                skip_min_game_time=False,
            )

        text = interaction.response.send_message.await_args.args[0]
        self.assertIn("ordinary text channel or a thread", text)
        self.assertEqual({}, app_state.active_games)
        fake_store.save_round.assert_not_awaited()

    async def test_a_thread_is_allowed(self) -> None:
        interaction = _permissive_interaction()
        interaction.channel = Mock(spec=discord.Thread)
        interaction.channel.id = 100
        interaction.channel.send = AsyncMock(return_value=Mock(id=999))
        fake_store = _fake_store("save_round")

        with (
            patch.object(app_state, "store", fake_store),
            patch("riskyroller.commands.arm_auto_close", Mock()),
        ):
            await _start_game(
                interaction,
                auto_close_players=25,
                auto_close_minutes=120,
                ping=False,
                skip_min_game_time=False,
            )

        self.assertEqual(1, len(app_state.active_games))


if __name__ == "__main__":
    unittest.main()
