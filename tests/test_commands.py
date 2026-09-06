import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from riskyroller import state as app_state
from riskyroller.commands import _start_game
from riskyroller.invite import invite_url


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
            patch("riskyroller.commands.schedule_auto_close", AsyncMock()),
        ):
            interaction = Mock()
            interaction.guild.id = 200
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
            patch("riskyroller.commands.schedule_auto_close", AsyncMock()),
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


if __name__ == "__main__":
    unittest.main()
