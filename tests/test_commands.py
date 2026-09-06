import unittest
from unittest.mock import AsyncMock, Mock

from riskyroller import state as app_state
from riskyroller.commands import _start_game
from riskyroller.invite import invite_url


class StartGameGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        app_state.active_games.clear()
        self.addCleanup(app_state.active_games.clear)

    async def test_commands_only_install_is_told_to_re_add_the_bot(self) -> None:
        # Installed with the applications.commands scope alone: the guild has
        # no member for the bot, so guild.me is None.
        interaction = Mock()
        interaction.guild.me = None
        interaction.guild.id = 200
        interaction.channel.id = 100
        interaction.client.application_id = 4242
        interaction.response.is_done.return_value = False
        interaction.response.send_message = AsyncMock()

        await _start_game(interaction, auto_close_players=25, auto_close_minutes=120, ping=True, skip_min_game_time=False)

        text = interaction.response.send_message.await_args.args[0]
        self.assertIn("slash commands only", text)
        self.assertIn(invite_url(4242), text)
        self.assertEqual({}, app_state.active_games)


if __name__ == "__main__":
    unittest.main()
