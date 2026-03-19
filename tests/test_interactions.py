"""Integration tests that simulate real Discord API interactions.

Each test constructs mock Discord objects (Interaction, User, Channel, Message)
mirroring the actual discord.py API surface, then exercises the bot's handlers
directly — no real network connection required.

Flows covered:
  - /risky_start command (basic, with auto-close params, channel limit)
  - /risky_set_ping command
  - /risky_reset_state command
  - Roll button (normal, double-roll rejection, reroll-phase restriction)
  - Close button (permission check, not-enough-players, normal close, 69 result)
  - Auto-close by player threshold
  - SixtyNineQuestionView ask-question button (permission check, opens modal)
  - SixtyNineQuestionModal submit (question delivered, state cleaned up)
"""

import asyncio
import os
import tempfile
import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from riskyroller import commands, state as app_state
from riskyroller.models import PendingQuestionState, RiskyRollState
from riskyroller.store import MAX_GAMES_PER_CHANNEL, StateStore
from riskyroller.views import RiskyRollView, SixtyNineQuestionModal, SixtyNineQuestionView

# ---------------------------------------------------------------------------
# Capture slash-command handlers from commands.setup()
# ---------------------------------------------------------------------------
# We pass a minimal stand-in for the bot whose tree.command() decorator just
# records the function instead of registering it with Discord.

_handlers: dict = {}


class _CapturingTree:
    def command(self, **kwargs):
        def decorator(func):
            _handlers[kwargs.get("name")] = func
            return func
        return decorator

    def error(self, func):
        _handlers["on_app_command_error"] = func
        return func


class _MockBot:
    tree = _CapturingTree()


commands.setup(_MockBot())

risky_start = _handlers["risky_start"]
risky_set_ping = _handlers["risky_set_ping"]
risky_reset_state = _handlers["risky_reset_state"]


# ---------------------------------------------------------------------------
# Mock interaction factory
# ---------------------------------------------------------------------------

def make_interaction(
    *,
    user_id: int = 100,
    channel_id: int = 200,
    guild_id: int = 300,
    is_response_done: bool = False,
) -> MagicMock:
    """Build a realistic discord.Interaction mock."""
    interaction = MagicMock()

    user = MagicMock()
    user.id = user_id
    user.display_name = f"User{user_id}"
    interaction.user = user

    guild = MagicMock()
    guild.id = guild_id
    interaction.guild = guild

    channel = MagicMock()
    channel.id = channel_id
    channel.name = f"channel-{channel_id}"
    # Make isinstance(channel, discord.TextChannel) pass — needed by get_text_channel.
    channel.__class__ = discord.TextChannel

    # Simulate fetching an existing round message.
    round_msg = MagicMock(spec=discord.Message)
    round_msg.id = 8880
    round_msg.edit = AsyncMock()
    channel.fetch_message = AsyncMock(return_value=round_msg)

    # Simulate posting new messages (prompts, notifications).
    sent_msg = MagicMock(spec=discord.Message)
    sent_msg.id = 9990
    channel.send = AsyncMock(return_value=sent_msg)

    interaction.channel = channel

    client = MagicMock()
    client.get_channel = MagicMock(return_value=channel)
    client.fetch_channel = AsyncMock(return_value=channel)
    interaction.client = client

    response = MagicMock()
    response.is_done = MagicMock(return_value=is_response_done)
    response.send_message = AsyncMock()
    response.edit_message = AsyncMock()
    response.defer = AsyncMock()
    response.send_modal = AsyncMock()
    interaction.response = response

    followup_msg = MagicMock(spec=discord.WebhookMessage)
    followup_msg.id = 7770
    followup = MagicMock()
    followup.send = AsyncMock(return_value=followup_msg)
    interaction.followup = followup

    orig_msg = MagicMock(spec=discord.Message)
    orig_msg.id = 8881
    orig_msg.edit = AsyncMock()
    interaction.original_response = AsyncMock(return_value=orig_msg)

    return interaction


def first_send_message_text(ix: MagicMock) -> str:
    """Extract the text from the first response.send_message call."""
    call = ix.response.send_message.call_args
    if call is None:
        return ""
    return call.args[0] if call.args else call.kwargs.get("content", "")


# ---------------------------------------------------------------------------
# Base test case: fresh temp-file SQLite store + clean app_state per test
# ---------------------------------------------------------------------------

class BotIntegrationTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".sqlite3")
        self.store = StateStore(self.db_path)
        # IsolatedAsyncioTestCase runs setUp synchronously before each test's
        # event loop; asyncio.run() is safe here.
        asyncio.run(self.store.initialize())

        app_state.store = self.store
        app_state.active_games.clear()
        app_state.pending_questions.clear()
        app_state.ping_roles.clear()
        for task in list(app_state.auto_close_tasks.values()):
            task.cancel()
        app_state.auto_close_tasks.clear()

    def tearDown(self) -> None:
        for task in list(app_state.auto_close_tasks.values()):
            task.cancel()
        app_state.auto_close_tasks.clear()
        os.close(self.db_fd)
        os.unlink(self.db_path)

    async def make_open_game(
        self,
        channel_id: int = 200,
        guild_id: int = 300,
        opener_id: int = 100,
        rolls: dict | None = None,
    ) -> RiskyRollState:
        """Seed an active game into app_state and the store."""
        state = RiskyRollState(
            channel_id=channel_id,
            guild_id=guild_id,
            opener_id=opener_id,
            rolls=rolls or {},
            message_id=8880,
        )
        app_state.active_games[state.game_id] = state
        await self.store.save_round(state)
        return state

    async def make_pending_question(
        self,
        winner_id: int = 100,
        channel_id: int = 200,
        guild_id: int = 300,
        prompt_kind: str = "direct",
        participant_user_ids: set | None = None,
        prompt_message_id: int | None = None,
    ) -> PendingQuestionState:
        """Seed a pending question into app_state and the store."""
        pq = PendingQuestionState(
            channel_id=channel_id,
            guild_id=guild_id,
            winner_id=winner_id,
            participant_user_ids=participant_user_ids or {101},
            game_id=str(uuid.uuid4()),
            prompt_kind=prompt_kind,
            prompt_message_id=prompt_message_id,
        )
        app_state.pending_questions[pq.game_id] = pq
        await self.store.save_pending_question(pq)
        return pq


# ---------------------------------------------------------------------------
# /risky_start
# ---------------------------------------------------------------------------

class TestRiskyStartCommand(BotIntegrationTestCase):
    async def test_start_creates_active_game(self) -> None:
        ix = make_interaction(user_id=1, channel_id=10, guild_id=20)

        await risky_start(ix)

        self.assertEqual(1, len(app_state.active_games))
        game = next(iter(app_state.active_games.values()))
        self.assertEqual(10, game.channel_id)
        self.assertEqual(20, game.guild_id)
        self.assertEqual(1, game.opener_id)
        self.assertTrue(game.is_open)

    async def test_start_posts_message_with_embed_and_view(self) -> None:
        ix = make_interaction(user_id=1, channel_id=10, guild_id=20)

        await risky_start(ix)

        ix.response.send_message.assert_awaited_once()
        call_kwargs = ix.response.send_message.call_args.kwargs
        self.assertIsNotNone(call_kwargs.get("embed"))
        self.assertIsNotNone(call_kwargs.get("view"))

    async def test_start_persists_to_store(self) -> None:
        ix = make_interaction(user_id=1, channel_id=10, guild_id=20)

        await risky_start(ix)

        rounds = await self.store.load_active_rounds()
        self.assertEqual(1, len(rounds))

    async def test_start_stores_message_id_from_original_response(self) -> None:
        ix = make_interaction(user_id=1, channel_id=10, guild_id=20)
        # original_response() returns a message with id=8881 (set in factory)
        await risky_start(ix)

        game = next(iter(app_state.active_games.values()))
        self.assertEqual(8881, game.message_id)

    async def test_start_with_ping_role_includes_role_mention(self) -> None:
        ix = make_interaction(user_id=1, channel_id=10, guild_id=20)
        app_state.ping_roles[20] = 555

        await risky_start(ix)

        call_kwargs = ix.response.send_message.call_args.kwargs
        self.assertIn("555", call_kwargs.get("content", ""))

    async def test_start_with_valid_auto_close_players_stores_threshold(self) -> None:
        ix = make_interaction(user_id=1, channel_id=10, guild_id=20)

        await risky_start(ix, auto_close_players=5)

        game = next(iter(app_state.active_games.values()))
        self.assertEqual(5, game.auto_close_players)

    async def test_start_rejects_auto_close_players_below_two(self) -> None:
        ix = make_interaction(user_id=1, channel_id=10, guild_id=20)

        await risky_start(ix, auto_close_players=1)

        game = next(iter(app_state.active_games.values()))
        self.assertIsNone(game.auto_close_players)

    async def test_start_enforces_channel_game_limit(self) -> None:
        channel_id = 10
        for _ in range(MAX_GAMES_PER_CHANNEL):
            await self.make_open_game(channel_id=channel_id)

        ix = make_interaction(user_id=1, channel_id=channel_id, guild_id=20)
        await risky_start(ix)

        # Count should still be at the limit; no extra game added.
        channel_games = [g for g in app_state.active_games.values() if g.channel_id == channel_id]
        self.assertEqual(MAX_GAMES_PER_CHANNEL, len(channel_games))
        ix.response.send_message.assert_awaited_once()
        self.assertIn(str(MAX_GAMES_PER_CHANNEL), first_send_message_text(ix))

    async def test_start_requires_guild(self) -> None:
        ix = make_interaction(user_id=1, channel_id=10, guild_id=20)
        ix.guild = None

        await risky_start(ix)

        ix.response.send_message.assert_awaited_once()
        self.assertEqual(0, len(app_state.active_games))

    # --- permission checks ---

    def _make_perms(self, *, send_messages=True, read_message_history=True, embed_links=True):
        perms = MagicMock()
        perms.send_messages = send_messages
        perms.read_message_history = read_message_history
        perms.embed_links = embed_links
        return perms

    async def test_start_blocked_when_missing_send_messages(self) -> None:
        ix = make_interaction(user_id=1, channel_id=10, guild_id=20)
        ix.channel.permissions_for = MagicMock(return_value=self._make_perms(send_messages=False))

        await risky_start(ix)

        ix.response.send_message.assert_awaited_once()
        self.assertIn("Send Messages", first_send_message_text(ix))
        self.assertEqual(0, len(app_state.active_games))

    async def test_start_blocked_when_missing_read_message_history(self) -> None:
        ix = make_interaction(user_id=1, channel_id=10, guild_id=20)
        ix.channel.permissions_for = MagicMock(return_value=self._make_perms(read_message_history=False))

        await risky_start(ix)

        ix.response.send_message.assert_awaited_once()
        self.assertIn("Read Message History", first_send_message_text(ix))
        self.assertEqual(0, len(app_state.active_games))

    async def test_start_blocked_when_missing_embed_links(self) -> None:
        ix = make_interaction(user_id=1, channel_id=10, guild_id=20)
        ix.channel.permissions_for = MagicMock(return_value=self._make_perms(embed_links=False))

        await risky_start(ix)

        ix.response.send_message.assert_awaited_once()
        self.assertIn("Embed Links", first_send_message_text(ix))
        self.assertEqual(0, len(app_state.active_games))

    async def test_start_lists_all_missing_permissions(self) -> None:
        ix = make_interaction(user_id=1, channel_id=10, guild_id=20)
        ix.channel.permissions_for = MagicMock(
            return_value=self._make_perms(send_messages=False, read_message_history=False, embed_links=False)
        )

        await risky_start(ix)

        msg = first_send_message_text(ix)
        self.assertIn("Send Messages", msg)
        self.assertIn("Read Message History", msg)
        self.assertIn("Embed Links", msg)
        self.assertEqual(0, len(app_state.active_games))

    async def test_start_proceeds_when_all_permissions_present(self) -> None:
        ix = make_interaction(user_id=1, channel_id=10, guild_id=20)
        ix.channel.permissions_for = MagicMock(
            return_value=self._make_perms(send_messages=True, read_message_history=True, embed_links=True)
        )

        await risky_start(ix)

        self.assertEqual(1, len(app_state.active_games))

    async def test_start_schedules_timer_task_when_auto_close_minutes_given(self) -> None:
        ix = make_interaction(user_id=1, channel_id=10, guild_id=20)

        await risky_start(ix, auto_close_minutes=5)

        game = next(iter(app_state.active_games.values()))
        self.assertIn(game.game_id, app_state.auto_close_tasks)


# ---------------------------------------------------------------------------
# /risky_set_ping
# ---------------------------------------------------------------------------

class TestSetPingCommand(BotIntegrationTestCase):
    async def test_set_ping_stores_role_in_state_and_db(self) -> None:
        ix = make_interaction(user_id=1, channel_id=10, guild_id=20)
        role = MagicMock(spec=discord.Role)
        role.id = 777
        role.mention = "<@&777>"

        await risky_set_ping(ix, role)

        self.assertEqual(777, app_state.ping_roles[20])
        db_roles = await self.store.load_ping_roles()
        self.assertEqual(777, db_roles[20])
        ix.response.send_message.assert_awaited_once()

    async def test_set_ping_updates_existing_role(self) -> None:
        app_state.ping_roles[20] = 111
        await self.store.set_ping_role(20, 111)

        ix = make_interaction(user_id=1, channel_id=10, guild_id=20)
        role = MagicMock(spec=discord.Role)
        role.id = 222
        role.mention = "<@&222>"

        await risky_set_ping(ix, role)

        self.assertEqual(222, app_state.ping_roles[20])
        db_roles = await self.store.load_ping_roles()
        self.assertEqual(222, db_roles[20])


# ---------------------------------------------------------------------------
# /risky_reset_state
# ---------------------------------------------------------------------------

class TestResetStateCommand(BotIntegrationTestCase):
    async def test_reset_clears_active_game_from_state_and_db(self) -> None:
        game = await self.make_open_game(channel_id=10)

        ix = make_interaction(user_id=1, channel_id=10, guild_id=20)
        await risky_reset_state(ix)

        self.assertNotIn(game.game_id, app_state.active_games)
        self.assertEqual(0, len(await self.store.load_active_rounds()))

    async def test_reset_clears_pending_question(self) -> None:
        pq = await self.make_pending_question(channel_id=10, guild_id=20)

        ix = make_interaction(user_id=1, channel_id=10, guild_id=20)
        await risky_reset_state(ix)

        self.assertNotIn(pq.game_id, app_state.pending_questions)
        self.assertEqual(0, len(await self.store.load_pending_questions()))

    async def test_reset_cancels_auto_close_task(self) -> None:
        game = await self.make_open_game(channel_id=10)
        task = asyncio.get_event_loop().create_task(asyncio.sleep(9999))
        app_state.auto_close_tasks[game.game_id] = task

        ix = make_interaction(user_id=1, channel_id=10, guild_id=20)
        await risky_reset_state(ix)

        self.assertTrue(task.cancelled())

    async def test_reset_with_nothing_to_clear_sends_notice(self) -> None:
        ix = make_interaction(user_id=1, channel_id=10, guild_id=20)
        await risky_reset_state(ix)

        ix.response.send_message.assert_awaited_once()
        self.assertIn("No active", first_send_message_text(ix))

    async def test_reset_only_affects_matching_channel(self) -> None:
        game_same = await self.make_open_game(channel_id=10)
        game_other = await self.make_open_game(channel_id=99)

        ix = make_interaction(user_id=1, channel_id=10, guild_id=20)
        await risky_reset_state(ix)

        self.assertNotIn(game_same.game_id, app_state.active_games)
        self.assertIn(game_other.game_id, app_state.active_games)


# ---------------------------------------------------------------------------
# Roll button
# ---------------------------------------------------------------------------

class TestRollButton(BotIntegrationTestCase):
    async def _press_roll(self, game_id: str, user_id: int, roll_value: int = 42) -> MagicMock:
        ix = make_interaction(user_id=user_id, channel_id=200)
        view = RiskyRollView(game_id)
        button = MagicMock()
        with patch("riskyroller.views.random.randint", return_value=roll_value):
            await view.roll_button.callback(ix)
        return ix

    async def test_roll_records_users_roll(self) -> None:
        game = await self.make_open_game()

        await self._press_roll(game.game_id, user_id=101, roll_value=42)

        self.assertIn(101, game.rolls)
        self.assertEqual(42, game.rolls[101])

    async def test_roll_updates_message_embed(self) -> None:
        game = await self.make_open_game()

        ix = await self._press_roll(game.game_id, user_id=101)

        ix.response.edit_message.assert_awaited_once()
        self.assertIsNotNone(ix.response.edit_message.call_args.kwargs.get("embed"))

    async def test_roll_persists_to_store(self) -> None:
        game = await self.make_open_game()

        await self._press_roll(game.game_id, user_id=101)

        rounds = await self.store.load_active_rounds()
        self.assertEqual(1, len(rounds))
        self.assertIn(101, rounds[0].rolls)

    async def test_double_roll_rejected_with_error_message(self) -> None:
        game = await self.make_open_game(rolls={101: 55})

        ix = await self._press_roll(game.game_id, user_id=101)

        ix.response.send_message.assert_awaited_once()
        self.assertIn("already rolled", first_send_message_text(ix))
        # Roll value must not have changed.
        self.assertEqual(55, game.rolls[101])

    async def test_roll_on_closed_game_rejected(self) -> None:
        game = await self.make_open_game()
        game.is_open = False

        ix = await self._press_roll(game.game_id, user_id=101)

        ix.response.send_message.assert_awaited_once()
        self.assertNotIn(101, game.rolls)

    async def test_non_reroll_user_blocked_during_reroll_phase(self) -> None:
        game = await self.make_open_game(rolls={101: 80, 102: 80, 103: 30})
        game.reroll_user_ids = {101, 102}

        ix = await self._press_roll(game.game_id, user_id=103)

        ix.response.send_message.assert_awaited_once()
        self.assertIn("cannot reroll", first_send_message_text(ix))

    async def test_reroll_user_can_roll_during_reroll_phase(self) -> None:
        game = await self.make_open_game(rolls={103: 30})
        game.reroll_user_ids = {101, 102}

        ix = await self._press_roll(game.game_id, user_id=101, roll_value=77)

        self.assertIn(101, game.rolls)
        self.assertEqual(77, game.rolls[101])

    async def test_roll_triggers_auto_close_task_at_player_threshold(self) -> None:
        game = await self.make_open_game(rolls={101: 55})
        game.auto_close_players = 2  # one more roll closes the round

        with patch("riskyroller.views.auto_close_round", new=AsyncMock()):
            ix = make_interaction(user_id=102, channel_id=game.channel_id)
            view = RiskyRollView(game.game_id)
            with patch("riskyroller.views.random.randint", return_value=70):
                await view.roll_button.callback(ix)

        self.assertIn(game.game_id, app_state.auto_close_tasks)


# ---------------------------------------------------------------------------
# Close button
# ---------------------------------------------------------------------------

class TestCloseButton(BotIntegrationTestCase):
    async def _press_close(self, game_id: str, user_id: int) -> MagicMock:
        ix = make_interaction(user_id=user_id, channel_id=200)
        view = RiskyRollView(game_id)
        await view.close_button.callback(ix)
        return ix

    async def test_close_rejects_non_opener(self) -> None:
        game = await self.make_open_game(opener_id=100)

        ix = await self._press_close(game.game_id, user_id=999)

        ix.response.send_message.assert_awaited_once()
        self.assertIn("opener", first_send_message_text(ix))
        self.assertTrue(game.is_open)

    async def test_close_rejects_when_fewer_than_two_rolls(self) -> None:
        game = await self.make_open_game(opener_id=100, rolls={100: 50})

        ix = await self._press_close(game.game_id, user_id=100)

        ix.response.send_message.assert_awaited_once()
        self.assertIn("2 players", first_send_message_text(ix))
        self.assertTrue(game.is_open)

    async def test_close_rejects_when_waiting_for_rerolls(self) -> None:
        game = await self.make_open_game(opener_id=100, rolls={101: 80})
        game.reroll_user_ids = {100, 101}

        ix = await self._press_close(game.game_id, user_id=100)

        ix.response.send_message.assert_awaited_once()
        self.assertIn("waiting", first_send_message_text(ix).lower())

    async def test_close_normal_result_removes_game_from_state_and_db(self) -> None:
        game = await self.make_open_game(opener_id=100, rolls={100: 80, 101: 20})

        await self._press_close(game.game_id, user_id=100)

        self.assertNotIn(game.game_id, app_state.active_games)
        self.assertEqual(0, len(await self.store.load_active_rounds()))

    async def test_close_normal_result_creates_direct_pending_question(self) -> None:
        game = await self.make_open_game(opener_id=100, rolls={100: 80, 101: 20})

        await self._press_close(game.game_id, user_id=100)

        self.assertIn(game.game_id, app_state.pending_questions)
        pq = app_state.pending_questions[game.game_id]
        self.assertEqual(100, pq.winner_id)
        self.assertEqual({101}, pq.participant_user_ids)
        self.assertEqual("direct", pq.prompt_kind)

    async def test_close_normal_result_sends_prompt_via_followup(self) -> None:
        game = await self.make_open_game(opener_id=100, rolls={100: 80, 101: 20})
        ix = make_interaction(user_id=100, channel_id=200)
        view = RiskyRollView(game.game_id)

        await view.close_button.callback(ix)

        ix.followup.send.assert_awaited()

    async def test_close_sixtynine_result_creates_room_prompt(self) -> None:
        game = await self.make_open_game(opener_id=100, rolls={100: 69, 101: 30})

        await self._press_close(game.game_id, user_id=100)

        pq = app_state.pending_questions.get(game.game_id)
        self.assertIsNotNone(pq)
        self.assertEqual("room", pq.prompt_kind)
        self.assertEqual(100, pq.winner_id)

    async def test_close_disables_all_buttons_on_round_message(self) -> None:
        game = await self.make_open_game(opener_id=100, rolls={100: 80, 101: 20})
        ix = make_interaction(user_id=100, channel_id=200)
        view = RiskyRollView(game.game_id)

        await view.close_button.callback(ix)

        ix.response.edit_message.assert_awaited_once()
        edited_view: RiskyRollView = ix.response.edit_message.call_args.kwargs.get("view")
        self.assertIsNotNone(edited_view)
        for item in edited_view.children:
            if hasattr(item, "disabled"):
                self.assertTrue(item.disabled, f"Item {item} should be disabled after close")

    async def test_close_persists_pending_question_to_db(self) -> None:
        game = await self.make_open_game(opener_id=100, rolls={100: 80, 101: 20})

        await self._press_close(game.game_id, user_id=100)

        db_pqs = await self.store.load_pending_questions()
        self.assertEqual(1, len(db_pqs))
        self.assertEqual(100, db_pqs[0].winner_id)


# ---------------------------------------------------------------------------
# SixtyNineQuestionView — Ask Question button
# ---------------------------------------------------------------------------

class TestAskQuestionButton(BotIntegrationTestCase):
    async def test_opens_modal_for_winner(self) -> None:
        pq = await self.make_pending_question(winner_id=100)
        ix = make_interaction(user_id=100, channel_id=200)
        view = SixtyNineQuestionView(pq.game_id)

        await view.ask_question_button.callback(ix)

        ix.response.send_modal.assert_awaited_once()
        modal = ix.response.send_modal.call_args.args[0]
        self.assertIsInstance(modal, SixtyNineQuestionModal)

    async def test_rejects_non_winner_with_error(self) -> None:
        pq = await self.make_pending_question(winner_id=100)
        ix = make_interaction(user_id=999, channel_id=200)
        view = SixtyNineQuestionView(pq.game_id)

        await view.ask_question_button.callback(ix)

        ix.response.send_message.assert_awaited_once()
        ix.response.send_modal.assert_not_awaited()

    async def test_rejects_when_no_pending_state_exists(self) -> None:
        ix = make_interaction(user_id=100, channel_id=200)
        view = SixtyNineQuestionView("nonexistent-game-id")

        await view.ask_question_button.callback(ix)

        ix.response.send_message.assert_awaited_once()
        ix.response.send_modal.assert_not_awaited()


# ---------------------------------------------------------------------------
# SixtyNineQuestionModal — question submission
# ---------------------------------------------------------------------------

class TestQuestionModalSubmit(BotIntegrationTestCase):
    def _make_modal(self, game_id: str, question_text: str) -> SixtyNineQuestionModal:
        modal = SixtyNineQuestionModal(game_id)
        modal.question = MagicMock()
        modal.question.value = question_text
        return modal

    async def test_submit_sends_question_publicly(self) -> None:
        pq = await self.make_pending_question(winner_id=100, prompt_message_id=9990)
        ix = make_interaction(user_id=100, channel_id=200)
        modal = self._make_modal(pq.game_id, "What is your favourite colour?")

        await modal.on_submit(ix)

        public_calls = [c for c in ix.followup.send.call_args_list if not c.kwargs.get("ephemeral", False)]
        self.assertEqual(1, len(public_calls))
        self.assertIn("What is your favourite colour?", public_calls[0].kwargs.get("content", ""))

    async def test_submit_removes_pending_question_from_state_and_db(self) -> None:
        pq = await self.make_pending_question(winner_id=100, prompt_message_id=9990)
        ix = make_interaction(user_id=100, channel_id=200)
        modal = self._make_modal(pq.game_id, "Any question?")

        await modal.on_submit(ix)

        self.assertNotIn(pq.game_id, app_state.pending_questions)
        self.assertEqual(0, len(await self.store.load_pending_questions()))

    async def test_submit_rejects_non_winner(self) -> None:
        pq = await self.make_pending_question(winner_id=100)
        ix = make_interaction(user_id=999, channel_id=200)
        modal = self._make_modal(pq.game_id, "I should not be allowed.")

        await modal.on_submit(ix)

        ix.response.send_message.assert_awaited_once()
        self.assertIn(pq.game_id, app_state.pending_questions)  # still present

    async def test_submit_rejects_blank_question(self) -> None:
        pq = await self.make_pending_question(winner_id=100)
        ix = make_interaction(user_id=100, channel_id=200)
        modal = self._make_modal(pq.game_id, "   ")

        await modal.on_submit(ix)

        ix.response.send_message.assert_awaited_once()
        self.assertIn(pq.game_id, app_state.pending_questions)  # not consumed

    async def test_submit_rejects_when_no_pending_state(self) -> None:
        ix = make_interaction(user_id=100, channel_id=200)
        modal = self._make_modal("nonexistent-game-id", "Hello?")

        await modal.on_submit(ix)

        ix.response.send_message.assert_awaited_once()

    async def test_submit_disables_prompt_message(self) -> None:
        pq = await self.make_pending_question(winner_id=100, prompt_message_id=9990)
        ix = make_interaction(user_id=100, channel_id=200)
        modal = self._make_modal(pq.game_id, "Are you happy?")

        await modal.on_submit(ix)

        # Prompt message should be fetched and edited to disabled state.
        ix.channel.fetch_message.assert_awaited()

    async def test_submit_room_kind_mentions_all_participants(self) -> None:
        pq = await self.make_pending_question(
            winner_id=100,
            prompt_kind="room",
            participant_user_ids={100, 101, 102},
            prompt_message_id=9990,
        )
        ix = make_interaction(user_id=100, channel_id=200)
        modal = self._make_modal(pq.game_id, "Room question here.")

        await modal.on_submit(ix)

        public_calls = [c for c in ix.followup.send.call_args_list if not c.kwargs.get("ephemeral", False)]
        self.assertEqual(1, len(public_calls))
        self.assertIn("Room question here.", public_calls[0].kwargs.get("content", ""))

    async def test_submit_sends_winner_confirmation_ephemerally(self) -> None:
        pq = await self.make_pending_question(winner_id=100, prompt_message_id=9990)
        ix = make_interaction(user_id=100, channel_id=200)
        modal = self._make_modal(pq.game_id, "A valid question.")

        await modal.on_submit(ix)

        ephemeral_calls = [c for c in ix.followup.send.call_args_list if c.kwargs.get("ephemeral", False)]
        self.assertGreater(len(ephemeral_calls), 0)


# ---------------------------------------------------------------------------
# auto_close_round()
# ---------------------------------------------------------------------------

def make_client(channel_id: int = 200) -> tuple[MagicMock, MagicMock]:
    """Return (client, channel) mocks suitable for auto_close_round calls."""
    channel = MagicMock()
    channel.id = channel_id
    channel.name = f"channel-{channel_id}"
    channel.__class__ = discord.TextChannel

    round_msg = MagicMock(spec=discord.Message)
    round_msg.id = 8880
    round_msg.edit = AsyncMock()
    channel.fetch_message = AsyncMock(return_value=round_msg)

    sent_msg = MagicMock(spec=discord.Message)
    sent_msg.id = 9990
    channel.send = AsyncMock(return_value=sent_msg)

    client = MagicMock()
    client.get_channel = MagicMock(return_value=channel)
    client.fetch_channel = AsyncMock(return_value=channel)
    return client, channel


class TestAutoClose(BotIntegrationTestCase):
    async def test_returns_early_when_game_not_in_active_games(self) -> None:
        from riskyroller.views import auto_close_round
        client, _ = make_client()

        await auto_close_round(client, "nonexistent-game-id")

        # Nothing should have changed.
        self.assertEqual(0, len(app_state.active_games))
        self.assertEqual(0, len(app_state.pending_questions))

    async def test_returns_early_when_game_is_already_closed(self) -> None:
        from riskyroller.views import auto_close_round
        game = await self.make_open_game()
        game.is_open = False
        client, _ = make_client(game.channel_id)

        await auto_close_round(client, game.game_id)

        # Game should not have been removed — we didn't touch it.
        self.assertIn(game.game_id, app_state.active_games)

    async def test_not_enough_players_closes_game_and_sends_notice(self) -> None:
        from riskyroller.views import auto_close_round
        game = await self.make_open_game(rolls={101: 50})  # only 1 roll
        client, channel = make_client(game.channel_id)

        await auto_close_round(client, game.game_id)

        self.assertNotIn(game.game_id, app_state.active_games)
        self.assertEqual(0, len(await self.store.load_active_rounds()))
        self.assertNotIn(game.game_id, app_state.pending_questions)

        channel.send.assert_awaited_once()
        notice = channel.send.call_args.args[0] if channel.send.call_args.args else channel.send.call_args.kwargs.get("content", "")
        self.assertIn("not enough players", notice)

    async def test_not_enough_players_disables_round_message(self) -> None:
        from riskyroller.views import auto_close_round
        game = await self.make_open_game(rolls={101: 50})
        client, channel = make_client(game.channel_id)

        await auto_close_round(client, game.game_id)

        channel.fetch_message.assert_awaited()

    async def test_normal_result_removes_game_from_state_and_db(self) -> None:
        from riskyroller.views import auto_close_round
        game = await self.make_open_game(rolls={100: 80, 101: 20})
        client, _ = make_client(game.channel_id)

        await auto_close_round(client, game.game_id)

        self.assertNotIn(game.game_id, app_state.active_games)
        self.assertEqual(0, len(await self.store.load_active_rounds()))

    async def test_normal_result_creates_direct_pending_question(self) -> None:
        from riskyroller.views import auto_close_round
        game = await self.make_open_game(rolls={100: 80, 101: 20})
        client, _ = make_client(game.channel_id)

        await auto_close_round(client, game.game_id)

        self.assertIn(game.game_id, app_state.pending_questions)
        pq = app_state.pending_questions[game.game_id]
        self.assertEqual(100, pq.winner_id)
        self.assertEqual({101}, pq.participant_user_ids)
        self.assertEqual("direct", pq.prompt_kind)

    async def test_normal_result_persists_pending_question_to_db(self) -> None:
        from riskyroller.views import auto_close_round
        game = await self.make_open_game(rolls={100: 80, 101: 20})
        client, _ = make_client(game.channel_id)

        await auto_close_round(client, game.game_id)

        db_pqs = await self.store.load_pending_questions()
        self.assertEqual(1, len(db_pqs))
        self.assertEqual(100, db_pqs[0].winner_id)

    async def test_normal_result_disables_round_message(self) -> None:
        from riskyroller.views import auto_close_round
        game = await self.make_open_game(rolls={100: 80, 101: 20})
        client, channel = make_client(game.channel_id)

        await auto_close_round(client, game.game_id)

        channel.fetch_message.assert_awaited()
        round_msg = channel.fetch_message.return_value
        round_msg.edit.assert_awaited()
        edited_view = round_msg.edit.call_args.kwargs.get("view")
        self.assertIsNotNone(edited_view)
        for item in edited_view.children:
            if hasattr(item, "disabled"):
                self.assertTrue(item.disabled)

    async def test_normal_result_sends_question_prompt_to_channel(self) -> None:
        from riskyroller.views import auto_close_round
        game = await self.make_open_game(rolls={100: 80, 101: 20})
        client, channel = make_client(game.channel_id)

        await auto_close_round(client, game.game_id)

        # channel.send is used for both "not enough players" notice AND the
        # question prompt, so in this branch only the prompt is sent.
        channel.send.assert_awaited_once()

    async def test_sixtynine_result_creates_room_pending_question(self) -> None:
        from riskyroller.views import auto_close_round
        game = await self.make_open_game(rolls={100: 69, 101: 30})
        client, _ = make_client(game.channel_id)

        await auto_close_round(client, game.game_id)

        pq = app_state.pending_questions.get(game.game_id)
        self.assertIsNotNone(pq)
        self.assertEqual("room", pq.prompt_kind)
        self.assertEqual(100, pq.winner_id)
        self.assertIn(101, pq.participant_user_ids)

    async def test_tie_result_closes_game_and_creates_pending_question(self) -> None:
        from riskyroller.views import auto_close_round
        game = await self.make_open_game(rolls={100: 90, 101: 90, 102: 10})
        client, _ = make_client(game.channel_id)

        await auto_close_round(client, game.game_id)

        self.assertNotIn(game.game_id, app_state.active_games)
        self.assertIn(game.game_id, app_state.pending_questions)

    async def test_forbidden_on_edit_skips_prompt_and_closes_cleanly(self) -> None:
        """When fetch_message returns Forbidden, auto-close should still remove the
        game and not attempt to send a prompt to the same inaccessible channel."""
        from riskyroller.views import auto_close_round
        game = await self.make_open_game(rolls={100: 80, 101: 20})
        client, channel = make_client(game.channel_id)
        channel.fetch_message = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "missing access"))

        await auto_close_round(client, game.game_id)

        # Game cleaned up despite the permission error.
        self.assertNotIn(game.game_id, app_state.active_games)
        self.assertEqual(0, len(await self.store.load_active_rounds()))
        # No prompt attempted — would also fail.
        self.assertNotIn(game.game_id, app_state.pending_questions)
        channel.send.assert_not_awaited()

    async def test_forbidden_on_send_prompt_closes_cleanly_without_fallback(self) -> None:
        """When channel.send returns Forbidden, no fallback send is attempted
        (it would fail too) and the game is still fully cleaned up."""
        from riskyroller.views import auto_close_round
        game = await self.make_open_game(rolls={100: 80, 101: 20})
        client, channel = make_client(game.channel_id)
        channel.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "missing access"))

        await auto_close_round(client, game.game_id)

        self.assertNotIn(game.game_id, app_state.active_games)
        self.assertEqual(0, len(await self.store.load_active_rounds()))
        # send was attempted exactly once — no second fallback attempt.
        channel.send.assert_awaited_once()
        self.assertNotIn(game.game_id, app_state.pending_questions)

    async def test_channel_not_found_still_closes_game(self) -> None:
        from riskyroller.views import auto_close_round
        game = await self.make_open_game(rolls={100: 80, 101: 20})

        # Client cannot find the channel.
        client = MagicMock()
        client.get_channel = MagicMock(return_value=None)
        client.fetch_channel = AsyncMock(side_effect=discord.NotFound(MagicMock(), "not found"))

        await auto_close_round(client, game.game_id)

        # Game is still removed from memory and DB.
        self.assertNotIn(game.game_id, app_state.active_games)
        self.assertEqual(0, len(await self.store.load_active_rounds()))
        # But no pending question because there was nowhere to send it.
        self.assertNotIn(game.game_id, app_state.pending_questions)

    async def test_auto_close_task_removed_from_tasks_dict_on_run(self) -> None:
        from riskyroller.views import auto_close_round
        game = await self.make_open_game(rolls={100: 80, 101: 20})
        # Simulate the task being tracked.
        task = asyncio.get_event_loop().create_task(asyncio.sleep(9999))
        app_state.auto_close_tasks[game.game_id] = task
        client, _ = make_client(game.channel_id)

        await auto_close_round(client, game.game_id)

        self.assertNotIn(game.game_id, app_state.auto_close_tasks)

    # --- timer-based close (auto_close_minutes) ---

    async def test_timer_task_is_created_by_risky_start(self) -> None:
        ix = make_interaction(user_id=1, channel_id=10, guild_id=20)

        await risky_start(ix, auto_close_minutes=5)

        game = next(iter(app_state.active_games.values()))
        self.assertIn(game.game_id, app_state.auto_close_tasks)
        task = app_state.auto_close_tasks[game.game_id]
        self.assertFalse(task.done())

    async def test_timer_task_calls_auto_close_round_after_sleep(self) -> None:
        """When the sleep completes, auto_close_round is invoked with the right args."""
        with patch("riskyroller.commands.asyncio.sleep", new=AsyncMock()) as mock_sleep, \
             patch("riskyroller.commands.auto_close_round", new=AsyncMock()) as mock_close:

            ix = make_interaction(user_id=1, channel_id=10, guild_id=20)
            await risky_start(ix, auto_close_minutes=3)

            game = next(iter(app_state.active_games.values()))
            task = app_state.auto_close_tasks[game.game_id]
            await task  # sleep is instant; task completes synchronously

        mock_sleep.assert_awaited_once_with(3 * 60)
        mock_close.assert_awaited_once_with(ix.client, game.game_id)

    async def test_roll_button_triggers_auto_close_at_player_threshold(self) -> None:
        """Roll button fires auto_close_round when auto_close_players threshold is hit."""
        game = await self.make_open_game(rolls={101: 55})
        game.auto_close_players = 2

        with patch("riskyroller.views.auto_close_round", new=AsyncMock()) as mock_close:
            ix = make_interaction(user_id=102, channel_id=game.channel_id)
            view = RiskyRollView(game.game_id)
            with patch("riskyroller.views.random.randint", return_value=70):
                await view.roll_button.callback(ix)

            # Give the created task a chance to be awaited.
            task = app_state.auto_close_tasks.get(game.game_id)
            self.assertIsNotNone(task)
            await task

        mock_close.assert_awaited_once_with(ix.client, game.game_id)

    async def test_close_button_cancels_existing_auto_close_task(self) -> None:
        """Manually closing the round cancels any pending timer task."""
        game = await self.make_open_game(opener_id=100, rolls={100: 80, 101: 20})
        task = asyncio.get_event_loop().create_task(asyncio.sleep(9999))
        app_state.auto_close_tasks[game.game_id] = task

        ix = make_interaction(user_id=100, channel_id=200)
        view = RiskyRollView(game.game_id)
        await view.close_button.callback(ix)

        self.assertTrue(task.cancelled())
        self.assertNotIn(game.game_id, app_state.auto_close_tasks)


# ---------------------------------------------------------------------------
# Full round-trip: start → roll × N → close → submit question
# ---------------------------------------------------------------------------

class TestFullRoundTrip(BotIntegrationTestCase):
    async def test_full_direct_question_flow(self) -> None:
        """Start, two players roll, opener closes, winner submits a question."""
        channel_id, guild_id = 10, 20
        opener_id, other_id = 1, 2

        # Start a round.
        ix_start = make_interaction(user_id=opener_id, channel_id=channel_id, guild_id=guild_id)
        await risky_start(ix_start)

        self.assertEqual(1, len(app_state.active_games))
        game = next(iter(app_state.active_games.values()))

        # Both players roll.
        for uid, roll_val in [(opener_id, 90), (other_id, 20)]:
            ix_roll = make_interaction(user_id=uid, channel_id=channel_id)
            view = RiskyRollView(game.game_id)
            with patch("riskyroller.views.random.randint", return_value=roll_val):
                await view.roll_button.callback(ix_roll)

        self.assertEqual({opener_id: 90, other_id: 20}, game.rolls)

        # Opener closes the round.
        ix_close = make_interaction(user_id=opener_id, channel_id=channel_id)
        view = RiskyRollView(game.game_id)
        await view.close_button.callback(ix_close)

        self.assertNotIn(game.game_id, app_state.active_games)
        pq = app_state.pending_questions.get(game.game_id)
        self.assertIsNotNone(pq)
        self.assertEqual(opener_id, pq.winner_id)
        self.assertEqual({other_id}, pq.participant_user_ids)

        # Winner submits the question.
        pq.prompt_message_id = 9990
        ix_modal = make_interaction(user_id=opener_id, channel_id=channel_id)
        modal = SixtyNineQuestionModal(game.game_id)
        modal.question = MagicMock()
        modal.question.value = "What is your quest?"
        await modal.on_submit(ix_modal)

        self.assertNotIn(game.game_id, app_state.pending_questions)
        public_calls = [c for c in ix_modal.followup.send.call_args_list if not c.kwargs.get("ephemeral", False)]
        self.assertGreater(len(public_calls), 0)
        self.assertIn("What is your quest?", public_calls[0].kwargs.get("content", ""))

    async def test_full_sixtynine_flow(self) -> None:
        """Player rolls 69, round closes with room prompt, winner submits question."""
        channel_id, guild_id = 10, 20
        roller_id, other_id = 1, 2

        ix_start = make_interaction(user_id=roller_id, channel_id=channel_id, guild_id=guild_id)
        await risky_start(ix_start)

        game = next(iter(app_state.active_games.values()))

        for uid, roll_val in [(roller_id, 69), (other_id, 40)]:
            ix_roll = make_interaction(user_id=uid, channel_id=channel_id)
            view = RiskyRollView(game.game_id)
            with patch("riskyroller.views.random.randint", return_value=roll_val):
                await view.roll_button.callback(ix_roll)

        ix_close = make_interaction(user_id=roller_id, channel_id=channel_id)
        view = RiskyRollView(game.game_id)
        await view.close_button.callback(ix_close)

        pq = app_state.pending_questions.get(game.game_id)
        self.assertIsNotNone(pq)
        self.assertEqual("room", pq.prompt_kind)
        self.assertEqual(roller_id, pq.winner_id)
        self.assertIn(other_id, pq.participant_user_ids)

        pq.prompt_message_id = 9990
        ix_modal = make_interaction(user_id=roller_id, channel_id=channel_id)
        modal = SixtyNineQuestionModal(game.game_id)
        modal.question = MagicMock()
        modal.question.value = "Room question!"
        await modal.on_submit(ix_modal)

        self.assertNotIn(game.game_id, app_state.pending_questions)


if __name__ == "__main__":
    unittest.main()
