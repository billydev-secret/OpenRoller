import asyncio
import logging
import time

import discord
from discord import app_commands

from . import commands
from . import state as app_state
from .config import DEBUG, DEBUG_GUILD_ID, SYNC_COMMANDS_ON_STARTUP
from .views import RiskyRollView, SixtyNineQuestionView, auto_close_round

log = logging.getLogger(__name__)

intents = discord.Intents.default()


class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        log.info("Bot is starting.")

    async def setup_hook(self) -> None:
        commands.setup(self)

        await app_state.store.initialize()
        app_state.ping_roles.update(await app_state.store.load_ping_roles())

        for state in await app_state.store.load_active_rounds():
            if state.message_id is not None:
                app_state.active_games[state.game_id] = state
                self.add_view(RiskyRollView(state.game_id), message_id=state.message_id)

                if state.auto_close_players and len(state.rolls) >= state.auto_close_players:
                    # Player threshold was already met before restart; close immediately.
                    task = asyncio.create_task(auto_close_round(self, state.game_id))
                    app_state.auto_close_tasks[state.game_id] = task
                    log.info(
                        "Restored auto-close for game %s: player threshold already met (%d/%d).",
                        state.game_id,
                        len(state.rolls),
                        state.auto_close_players,
                    )
                elif state.auto_close_minutes:
                    elapsed = time.time() - state.created_at
                    remaining = max(0.0, state.auto_close_minutes * 60 - elapsed)

                    async def _timed_close(game_id: str = state.game_id, delay: float = remaining) -> None:
                        await asyncio.sleep(delay)
                        await auto_close_round(self, game_id)

                    task = asyncio.create_task(_timed_close())
                    app_state.auto_close_tasks[state.game_id] = task
                    log.info(
                        "Restored auto-close timer for game %s (%.0fs remaining).",
                        state.game_id,
                        remaining,
                    )
            else:
                log.warning("Active round for game %s is missing a message_id.", state.game_id)
                await app_state.store.delete_round(state.game_id)

        for state in await app_state.store.load_pending_questions():
            if state.prompt_message_id is not None:
                app_state.pending_questions[state.game_id] = state
                self.add_view(SixtyNineQuestionView(state.game_id), message_id=state.prompt_message_id)
            else:
                log.warning(
                    "Pending question for game %s is missing a prompt_message_id.",
                    state.game_id,
                )
                await app_state.store.delete_pending_question(state.game_id)

        if DEBUG:
            if DEBUG_GUILD_ID is None:
                raise RuntimeError("DEBUG is enabled but GUILD_ID is missing from the environment.")
            guild = discord.Object(id=DEBUG_GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Synced commands to development guild %s.", DEBUG_GUILD_ID)
        elif SYNC_COMMANDS_ON_STARTUP:
            await self.tree.sync()
            log.info("Synced commands globally.")
        else:
            log.info("Skipping global command sync on startup.")

    async def on_ready(self) -> None:
        log.info("Bot ready in %s guild(s).", len(self.guilds))


bot = Bot()
