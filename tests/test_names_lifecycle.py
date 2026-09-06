import asyncio
import unittest
from unittest import mock
from unittest.mock import AsyncMock, Mock, patch

from riskyroller import state as app_state
from riskyroller.bot import Bot
from riskyroller.formatters import build_embed
from riskyroller.models import RiskyRollState
from riskyroller.views import QuestionReplyView, RiskyRollView, SixtyNineQuestionView


def _interaction(user_id: int, display_name: str) -> Mock:
    interaction = Mock()
    interaction.user = Mock()
    interaction.user.id = user_id
    interaction.user.display_name = display_name
    interaction.guild = None
    interaction.channel = Mock()
    interaction.client = Mock()
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    interaction.followup.send = AsyncMock(return_value=Mock(id=555))
    return interaction


class CrossGuildDisplayNameTests(unittest.IsolatedAsyncioTestCase):
    """A18: a nickname captured when a player rolls in one guild must never
    be handed back for a different guild's roster — the process-wide,
    bare-user-id display_names dict let a later roll in guild B overwrite
    the name guild A's roster was relying on."""

    GUILD_A = 1001
    GUILD_B = 2002
    USER_ID = 5000

    def setUp(self) -> None:
        for d in (
            app_state.active_games,
            app_state.guild_display_names,
            app_state.auto_close_tasks,
        ):
            d.clear()
            self.addCleanup(d.clear)
        self.fake_store = Mock(save_round=AsyncMock())
        patcher = patch.object(app_state, "store", self.fake_store)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def _roll(self, guild_id: int, game_id: str, display_name: str) -> RiskyRollState:
        state = RiskyRollState(channel_id=guild_id, guild_id=guild_id, opener_id=self.USER_ID, game_id=game_id)
        app_state.active_games[game_id] = state
        view = RiskyRollView(game_id)
        await view.roll_button.callback(_interaction(self.USER_ID, display_name))
        return state

    async def test_a_name_captured_in_one_guild_never_leaks_into_another(self) -> None:
        state_a = await self._roll(self.GUILD_A, "game-a", "Alice")
        await self._roll(self.GUILD_B, "game-b", "Kitten (DMs open)")

        # Re-render guild A's roster after the guild-B roll — this is the
        # reported repro: a later roll elsewhere must not change what guild
        # A's own roster prints for the same user.
        roster = build_embed(state_a).fields[0].value or ""

        self.assertIn("Alice", roster)
        self.assertNotIn("Kitten", roster)


class GuildRemovalClearsDisplayNamesTests(unittest.IsolatedAsyncioTestCase):
    """A18: on_guild_remove must evict only the departed guild's cached
    names, leaving every other guild's untouched, and the cache must not
    grow forever for guilds the bot is no longer in."""

    GUILD_A = 3001
    GUILD_B = 3002

    def setUp(self) -> None:
        for d in (
            app_state.guild_display_names,
            app_state.active_games,
            app_state.pending_questions,
            app_state.posted_questions,
            app_state.ping_roles,
            app_state.min_game_seconds,
            app_state.max_games_per_channel,
        ):
            d.clear()
            self.addCleanup(d.clear)
        self.fake_store = Mock(delete_guild_data=AsyncMock(return_value=[]))
        patcher = patch.object(app_state, "store", self.fake_store)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.bot = Bot()

    async def test_on_guild_remove_drops_only_that_guilds_cached_names(self) -> None:
        app_state.guild_display_names[(self.GUILD_A, 1)] = "Alice"
        app_state.guild_display_names[(self.GUILD_B, 1)] = "Kitten"

        await self.bot.on_guild_remove(Mock(id=self.GUILD_A))

        self.assertNotIn((self.GUILD_A, 1), app_state.guild_display_names)
        self.assertEqual("Kitten", app_state.guild_display_names[(self.GUILD_B, 1)])


class GuildRemovalLockingTests(unittest.IsolatedAsyncioTestCase):
    """A1 (bot.py half): on_guild_remove must serialize with roll_button,
    close_button and auto_close_round through the same per-game lock, or a
    press already in flight can finish after teardown and re-edit the round
    message, save a roll, or post a winner prompt for a guild whose data
    teardown is in the middle of deleting."""

    GUILD_ID = 4001

    def setUp(self) -> None:
        app_state.active_games.clear()
        app_state.auto_close_tasks.clear()
        self.addCleanup(app_state.active_games.clear)
        self.addCleanup(app_state.auto_close_tasks.clear)
        self.fake_store = Mock(delete_guild_data=AsyncMock(return_value=[]))
        patcher = patch.object(app_state, "store", self.fake_store)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.bot = Bot()

    async def test_on_guild_remove_waits_for_an_in_flight_operation_on_the_same_game(self) -> None:
        game_id = "in-flight-game"
        app_state.active_games[game_id] = RiskyRollState(
            channel_id=1, guild_id=self.GUILD_ID, opener_id=1, game_id=game_id
        )

        lock = app_state.get_game_lock(game_id)
        await lock.acquire()
        try:
            task = asyncio.create_task(self.bot.on_guild_remove(Mock(id=self.GUILD_ID)))
            await asyncio.sleep(0)
            # Teardown is blocked behind the same lock a roll/close/auto-close
            # would hold, so the game must still be here.
            self.assertIn(game_id, app_state.active_games)
        finally:
            lock.release()

        await task
        self.assertNotIn(game_id, app_state.active_games)


class StartupGuildSweepTests(unittest.IsolatedAsyncioTestCase):
    """A30: a guild that removed the bot while it was offline is never seen
    by on_guild_remove, so its settings and rounds would otherwise persist
    forever and its auto-close timers keep getting re-armed on every
    restart. The sweep runs once self.guilds is known (on the first ready)
    and drops anything left over for a guild we're no longer in."""

    STILL_HERE = 6001
    LONG_GONE = 6002

    def setUp(self) -> None:
        for d in (
            app_state.active_games,
            app_state.pending_questions,
            app_state.posted_questions,
            app_state.ping_roles,
            app_state.min_game_seconds,
            app_state.max_games_per_channel,
            app_state.guild_display_names,
        ):
            d.clear()
            self.addCleanup(d.clear)
        self.fake_store = Mock(delete_guild_data=AsyncMock(return_value=[]))
        patcher = patch.object(app_state, "store", self.fake_store)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.bot = Bot()

    async def test_a_guild_missing_from_self_guilds_is_dropped(self) -> None:
        gone_game, here_game = "gone-game", "here-game"
        app_state.active_games[gone_game] = RiskyRollState(
            channel_id=1, guild_id=self.LONG_GONE, opener_id=1, game_id=gone_game
        )
        app_state.active_games[here_game] = RiskyRollState(
            channel_id=2, guild_id=self.STILL_HERE, opener_id=1, game_id=here_game
        )
        app_state.ping_roles[self.LONG_GONE] = 999

        with mock.patch.object(
            Bot, "guilds", new_callable=mock.PropertyMock, return_value=[Mock(id=self.STILL_HERE)]
        ):
            await self.bot._sweep_guilds_left_while_offline()

        self.assertNotIn(gone_game, app_state.active_games)
        self.assertIn(here_game, app_state.active_games)
        self.assertNotIn(self.LONG_GONE, app_state.ping_roles)
        self.fake_store.delete_guild_data.assert_awaited_once_with(self.LONG_GONE)

    async def test_runs_only_once(self) -> None:
        app_state.ping_roles[self.LONG_GONE] = 1

        with mock.patch.object(Bot, "guilds", new_callable=mock.PropertyMock, return_value=[]):
            await self.bot._sweep_guilds_left_while_offline()
            self.fake_store.delete_guild_data.reset_mock()
            app_state.ping_roles[self.LONG_GONE] = 1  # as if a race re-populated it
            await self.bot._sweep_guilds_left_while_offline()

        self.fake_store.delete_guild_data.assert_not_awaited()


class PersistentViewFallbackTests(unittest.IsolatedAsyncioTestCase):
    """A28: sweep_old_posted_questions / sweep_old_pending_questions clear a
    row older than the TTL at startup, but the Discord message it came from
    still has live buttons on it — load_posted_questions/load_pending_questions
    never return the swept row, so setup_hook never calls add_view for that
    exact message. discord.py then has nothing to route the press to at all,
    and the presser gets Discord's bare "This interaction failed" with no
    reply from the bot. A message-id-less registration of each view class is
    discord.py's own fallback match for a press whose message has no
    specific view — and both callbacks already look up their state by the
    interaction's own message id (QuestionReplyView) or an empty game_id
    (SixtyNineQuestionView) and give a real explanation when it's missing, so
    registering the fallback is the whole fix."""

    def setUp(self) -> None:
        self.fake_store = Mock(
            initialize=AsyncMock(),
            sweep_old_posted_questions=AsyncMock(return_value=0),
            sweep_old_pending_questions=AsyncMock(return_value=0),
            load_ping_roles=AsyncMock(return_value={}),
            load_min_game_times=AsyncMock(return_value={}),
            load_max_games_per_channel=AsyncMock(return_value={}),
            load_active_rounds=AsyncMock(return_value=[]),
            load_pending_questions=AsyncMock(return_value=[]),
            load_posted_questions=AsyncMock(return_value=[]),
        )
        patcher = patch.object(app_state, "store", self.fake_store)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.bot = Bot()
        self.bot.add_view = Mock()
        self.bot.tree.sync = AsyncMock()

    async def test_setup_hook_registers_message_less_fallback_views(self) -> None:
        await self.bot.setup_hook()

        fallback_classes = {
            type(call.args[0])
            for call in self.bot.add_view.call_args_list
            if call.args and call.kwargs.get("message_id") is None
        }
        self.assertIn(SixtyNineQuestionView, fallback_classes)
        self.assertIn(QuestionReplyView, fallback_classes)


if __name__ == "__main__":
    unittest.main()
