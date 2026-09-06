import inspect
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import discord

from riskyroller import state as app_state
from riskyroller import views
from riskyroller.models import RiskyRollState
from riskyroller.views import RiskyRollView, _build_one_rule_prompt_state, auto_close_round


def _expired_interaction() -> discord.NotFound:
    """A discord.NotFound carrying error code 10062 (Unknown interaction)."""
    response = Mock(status=404, reason="Not Found")
    return discord.NotFound(response, {"code": 10062, "message": "Unknown interaction"})


def _make_state(game_id: str, *, rolls: dict[int, int], opener_id: int) -> RiskyRollState:
    return RiskyRollState(
        channel_id=100,
        guild_id=200,
        opener_id=opener_id,
        game_id=game_id,
        message_id=999,
        rolls=dict(rolls),
        skip_min_game_time=True,  # bypass the minimum-time gate; unrelated to these findings
    )


def _make_channel() -> Mock:
    channel = Mock(spec=discord.TextChannel)
    channel.name = "general"
    channel.guild = None
    channel.get_partial_message.return_value.edit = AsyncMock()
    channel.send = AsyncMock(return_value=Mock(id=555))
    return channel


def _make_interaction(user_id: int) -> Mock:
    interaction = Mock()
    interaction.user = Mock()
    interaction.user.id = user_id
    interaction.guild = None
    interaction.channel = Mock()
    interaction.client = Mock()
    interaction.response.defer = AsyncMock()
    interaction.response.is_done.return_value = True  # deferred before the lock is ever taken
    # Both the pre-fix call (response.edit_message) and the post-fix one
    # (edit_original_response) are mocked identically, so a test exercises
    # the *behaviour* either implementation produces rather than crashing on
    # whichever attribute the code under test happens not to call.
    interaction.response.edit_message = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    interaction.followup.send = AsyncMock(return_value=Mock(id=555))
    return interaction


def _fail_the_closing_edit(interaction: Mock, error: Exception) -> None:
    interaction.response.edit_message = AsyncMock(side_effect=error)
    interaction.edit_original_response = AsyncMock(side_effect=error)


class _RoundTestCase(unittest.IsolatedAsyncioTestCase):
    """Shared fixtures for the close_button / auto_close_round tests below."""

    def setUp(self) -> None:
        app_state.active_games.clear()
        app_state.pending_questions.clear()
        app_state.auto_close_tasks.clear()
        self.addCleanup(app_state.active_games.clear)
        self.addCleanup(app_state.pending_questions.clear)
        self.addCleanup(self._cancel_pending_tasks)

        self.fake_store = Mock(
            delete_round=AsyncMock(),
            save_round=AsyncMock(),
            save_pending_question=AsyncMock(),
            delete_pending_question=AsyncMock(),
        )
        patcher = patch.object(app_state, "store", self.fake_store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _cancel_pending_tasks(self) -> None:
        for task in app_state.auto_close_tasks.values():
            task.cancel()
        app_state.auto_close_tasks.clear()


class CloseButtonTests(_RoundTestCase):
    """Close Round must not destroy the round before its own display update
    has actually succeeded — see finding A0."""

    async def test_happy_path_closes_exactly_once_and_sends_the_prompt(self) -> None:
        state = _make_state("g1", rolls={10: 80, 20: 30}, opener_id=10)
        app_state.active_games["g1"] = state
        interaction = _make_interaction(10)

        view = RiskyRollView("g1")
        await view.close_button.callback(interaction)

        interaction.response.defer.assert_awaited_once()
        interaction.edit_original_response.assert_awaited_once()
        self.assertNotIn("g1", app_state.active_games)
        self.fake_store.delete_round.assert_awaited_once_with("g1")
        interaction.followup.send.assert_awaited_once()
        self.assertTrue(interaction.followup.send.await_args.kwargs.get("wait"))
        self.assertIn("g1", app_state.pending_questions)

    async def test_expired_interaction_on_the_closing_edit_still_prompts(self) -> None:
        state = _make_state("g2", rolls={10: 80, 20: 30}, opener_id=10)
        app_state.active_games["g2"] = state
        interaction = _make_interaction(10)
        _fail_the_closing_edit(interaction, _expired_interaction())

        view = RiskyRollView("g2")
        # Captured, not just tolerated: this failure must reach the log, and
        # asserting on it also keeps a passing run from printing a traceback.
        with self.assertLogs("riskyroller.views", level="ERROR"):
            await view.close_button.callback(interaction)

        # The display update failed. What must not happen is what used to:
        # the round destroyed and the winner left with no prompt at all.
        interaction.followup.send.assert_awaited_once()
        self.assertTrue(interaction.followup.send.await_args.kwargs.get("wait"))
        self.assertIn("g2", app_state.pending_questions)
        # The round really did resolve, so it is retired either way — keeping
        # it would hold a slot against the channel's cap forever, and its
        # stored row still says is_open, so a restart would reopen a round
        # whose question has already been asked.
        self.assertNotIn("g2", app_state.active_games)
        self.fake_store.delete_round.assert_awaited_once_with("g2")

    async def test_not_enough_players_refuses_without_touching_the_round(self) -> None:
        state = _make_state("g3", rolls={10: 42}, opener_id=10)
        app_state.active_games["g3"] = state
        interaction = _make_interaction(10)

        view = RiskyRollView("g3")
        await view.close_button.callback(interaction)

        interaction.edit_original_response.assert_not_awaited()
        self.fake_store.delete_round.assert_not_awaited()
        self.assertIn("g3", app_state.active_games)
        self.assertTrue(state.is_open)
        text = interaction.followup.send.await_args.args[0]
        self.assertIn("Can't close yet", text)


class OnErrorExpiryLevelTests(unittest.IsolatedAsyncioTestCase):
    """A 10062 after a real state change is not the same as a stale refusal."""

    async def test_expired_interaction_before_any_mutation_logs_quietly(self) -> None:
        view = RiskyRollView("g4")
        interaction = Mock()
        interaction.app_permissions = Mock()
        interaction.followup.send = AsyncMock()
        interaction.response.is_done.return_value = True

        with self.assertLogs("riskyroller.views", level="DEBUG") as captured:
            await view.on_error(interaction, _expired_interaction(), Mock())

        self.assertTrue(any(record.levelname == "DEBUG" for record in captured.records))
        self.assertFalse(any(record.levelname == "ERROR" for record in captured.records))

    async def test_expired_interaction_after_a_mutation_logs_as_an_error(self) -> None:
        view = RiskyRollView("g4")
        view._resolved = True
        interaction = Mock()
        interaction.app_permissions = Mock()
        interaction.followup.send = AsyncMock()
        interaction.response.is_done.return_value = True

        with self.assertLogs("riskyroller.views", level="DEBUG") as captured:
            await view.on_error(interaction, _expired_interaction(), Mock())

        self.assertTrue(any(record.levelname == "ERROR" for record in captured.records))


class AutoCloseRoundTests(_RoundTestCase):
    """auto_close_round must never destroy a round it hasn't actually closed
    — see findings A2 and A53."""

    async def test_channel_unreachable_leaves_the_round_untouched_and_retries(self) -> None:
        state = _make_state("g5", rolls={10: 80, 20: 30}, opener_id=10)
        app_state.active_games["g5"] = state
        client = Mock()

        with patch("riskyroller.views.get_text_channel", AsyncMock(return_value=None)):
            await auto_close_round(client, "g5")

        self.assertIn("g5", app_state.active_games)
        self.assertTrue(state.is_open)
        self.fake_store.delete_round.assert_not_awaited()
        # A retry was scheduled rather than the round simply vanishing.
        self.assertIn("g5", app_state.auto_close_tasks)

    async def test_transport_error_editing_the_message_does_not_strand_the_round(self) -> None:
        state = _make_state("g6", rolls={10: 80, 20: 30}, opener_id=10)
        app_state.active_games["g6"] = state
        channel = _make_channel()
        channel.get_partial_message.return_value.edit = AsyncMock(
            side_effect=aiohttp.ServerDisconnectedError()
        )
        client = Mock()

        with (
            patch("riskyroller.views.get_text_channel", AsyncMock(return_value=channel)),
            self.assertLogs("riskyroller.views", level="ERROR"),
        ):
            await auto_close_round(client, "g6")  # must not raise

        # The edit failed, but the round still finishes closing rather than
        # being left as is_open=False forever with nothing able to touch it.
        self.assertNotIn("g6", app_state.active_games)
        self.fake_store.delete_round.assert_awaited_once_with("g6")
        channel.send.assert_awaited()  # the question prompt still went out

    async def test_happy_path_edits_the_message_and_sends_the_prompt(self) -> None:
        state = _make_state("g7", rolls={10: 80, 20: 30}, opener_id=10)
        app_state.active_games["g7"] = state
        channel = _make_channel()
        client = Mock()

        with patch("riskyroller.views.get_text_channel", AsyncMock(return_value=channel)):
            await auto_close_round(client, "g7")

        channel.get_partial_message.return_value.edit.assert_awaited_once()
        self.assertNotIn("g7", app_state.active_games)
        self.fake_store.delete_round.assert_awaited_once_with("g7")
        channel.send.assert_awaited_once()
        self.assertIn("g7", app_state.pending_questions)

    async def test_not_enough_players_closes_with_no_result_message(self) -> None:
        state = _make_state("g8", rolls={10: 42}, opener_id=10)
        app_state.active_games["g8"] = state
        channel = _make_channel()
        client = Mock()

        with patch("riskyroller.views.get_text_channel", AsyncMock(return_value=channel)):
            await auto_close_round(client, "g8")

        self.assertNotIn("g8", app_state.active_games)
        self.assertFalse(state.is_open)
        self.fake_store.delete_round.assert_awaited_once_with("g8")
        text = channel.send.await_args.args[0]
        self.assertIn("Round auto-closed with no result", text)
        self.assertIn("1 has", text)


class OneRulePromptTests(unittest.TestCase):
    """How to Play promises "the top 2 players each ask the loser" — two
    questions. The winner's own question at the loser is already the main
    prompt, so this second prompt must carry only the *other* top-2 player."""

    def _state(self, rolls: dict[int, int]) -> RiskyRollState:
        state = RiskyRollState(channel_id=1, guild_id=2, opener_id=10, rolls=rolls, game_id="g")
        state.resolve()
        return state

    def test_second_highest_is_the_only_questioner(self) -> None:
        # 90 / 50 / 1: the winner (10) already asks via the main prompt, so
        # naming them here too would ask the loser three times, twice by 10.
        prompt = _build_one_rule_prompt_state("g", self._state({10: 90, 20: 50, 30: 1}))

        self.assertIsNotNone(prompt)
        self.assertEqual({20}, prompt.allowed_questioners())
        self.assertEqual({30}, prompt.participant_user_ids)

    def test_two_player_round_gets_no_one_rule_prompt(self) -> None:
        # With no second-highest roller there is no "top 2": this prompt
        # would be a pure duplicate of the main one.
        self.assertIsNone(_build_one_rule_prompt_state("g", self._state({10: 90, 20: 1})))

    def test_no_prompt_when_the_lowest_roll_is_not_a_1(self) -> None:
        self.assertIsNone(_build_one_rule_prompt_state("g", self._state({10: 90, 20: 50, 30: 2})))


class AutoCloseTaskArmingTests(unittest.TestCase):
    """Every auto-close task must carry the done-callback that retrieves its
    exception; without it a failure surfaces only as asyncio's "Task exception
    was never retrieved". Three of the five call sites once missed it, so the
    guard is structural: there is exactly one place that creates these tasks.
    """

    def test_arm_auto_close_is_the_only_task_creator(self) -> None:
        package = Path(views.__file__).parent
        sites = [
            f"{path.name}:{number}"
            for path in sorted(package.glob("*.py"))
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if "asyncio.create_task(" in line
        ]

        self.assertEqual(1, len(sites), f"expected one task-creation site, found {sites}")
        self.assertIn("asyncio.create_task(", inspect.getsource(views.arm_auto_close))
        self.assertIn("add_done_callback", inspect.getsource(views.arm_auto_close))


class CloseButtonMinimumTests(_RoundTestCase):
    """Only a minimum the server actually chose holds Close Round. The default
    exists so an automatic close doesn't end a round the instant the last
    expected player rolls — it was never meant to stop the opener."""

    def _open_round(self, game_id: str) -> None:
        state = RiskyRollState(
            channel_id=100, guild_id=200, opener_id=10, game_id=game_id,
            message_id=999, rolls={10: 80, 20: 30},
        )
        app_state.active_games[game_id] = state

    def setUp(self) -> None:
        super().setUp()
        app_state.min_game_seconds.clear()
        self.addCleanup(app_state.min_game_seconds.clear)

    async def test_a_server_with_no_minimum_can_close_at_once(self) -> None:
        self._open_round("m1")
        interaction = _make_interaction(10)

        await RiskyRollView("m1").close_button.callback(interaction)

        interaction.edit_original_response.assert_awaited_once()
        self.assertNotIn("m1", app_state.active_games)

    async def test_a_configured_minimum_still_holds_it(self) -> None:
        app_state.min_game_seconds[200] = 1800
        self._open_round("m2")
        interaction = _make_interaction(10)

        await RiskyRollView("m2").close_button.callback(interaction)

        interaction.edit_original_response.assert_not_awaited()
        self.assertIn("m2", app_state.active_games)
        self.assertIn("can't be closed by hand", interaction.followup.send.await_args.args[0])

    async def test_a_configured_zero_closes_at_once(self) -> None:
        app_state.min_game_seconds[200] = 0
        self._open_round("m3")
        interaction = _make_interaction(10)

        await RiskyRollView("m3").close_button.callback(interaction)

        interaction.edit_original_response.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
