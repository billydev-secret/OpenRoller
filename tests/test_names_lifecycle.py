import unittest
from unittest.mock import AsyncMock, Mock, patch

from riskyroller import state as app_state
from riskyroller.formatters import build_embed
from riskyroller.models import RiskyRollState
from riskyroller.views import RiskyRollView


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
            app_state.display_names,
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


if __name__ == "__main__":
    unittest.main()
