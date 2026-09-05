import asyncio
import logging
import time

import discord
from discord import app_commands

from . import commands
from . import state as app_state
from .config import DEBUG, DEBUG_GUILD_ID, DEFAULT_MIN_GAME_SECONDS, SYNC_COMMANDS_ON_STARTUP
from .views import QuestionReplyView, RiskyRollView, SixtyNineQuestionView, schedule_auto_close

log = logging.getLogger(__name__)

intents = discord.Intents.default()

QUESTION_TTL_SECONDS = 7 * 24 * 60 * 60


class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        log.info("Bot is starting.")

    async def setup_hook(self) -> None:
        commands.setup(self)

        await app_state.store.initialize()
        swept = await app_state.store.sweep_old_posted_questions(QUESTION_TTL_SECONDS)
        if swept:
            log.info("Swept %d posted_questions older than %d days.", swept, QUESTION_TTL_SECONDS // 86400)
        swept = await app_state.store.sweep_old_pending_questions(QUESTION_TTL_SECONDS)
        if swept:
            log.info("Swept %d pending_questions older than %d days.", swept, QUESTION_TTL_SECONDS // 86400)
        ping_roles, min_game_times, active_rounds, pending_questions, posted_questions = await asyncio.gather(
            app_state.store.load_ping_roles(),
            app_state.store.load_min_game_times(),
            app_state.store.load_active_rounds(),
            app_state.store.load_pending_questions(),
            app_state.store.load_posted_questions(),
        )
        app_state.ping_roles.update(ping_roles)
        app_state.min_game_seconds.update(min_game_times)

        for state in active_rounds:
            if state.message_id is not None:
                app_state.active_games[state.game_id] = state
                self.add_view(RiskyRollView(state.game_id), message_id=state.message_id)

                if state.auto_close_players and len(state.rolls) >= state.auto_close_players:
                    elapsed = time.time() - state.created_at
                    min_seconds = 0 if state.skip_min_game_time else app_state.min_game_seconds.get(state.guild_id, DEFAULT_MIN_GAME_SECONDS)
                    remaining = max(0.0, min_seconds - elapsed)
                    app_state.auto_close_tasks[state.game_id] = asyncio.create_task(
                        schedule_auto_close(self, state.game_id, remaining)
                    )
                    log.info(
                        "Restored auto-close for game %s: player threshold already met (%d/%d), closing in %.0fs.",
                        state.game_id,
                        len(state.rolls),
                        state.auto_close_players,
                        remaining,
                    )
                elif state.auto_close_minutes:
                    elapsed = time.time() - state.created_at
                    remaining = max(0.0, state.auto_close_minutes * 60 - elapsed)
                    app_state.auto_close_tasks[state.game_id] = asyncio.create_task(
                        schedule_auto_close(self, state.game_id, remaining)
                    )
                    log.info(
                        "Restored auto-close timer for game %s (%.0fs remaining).",
                        state.game_id,
                        remaining,
                    )
            else:
                log.warning("Active round for game %s is missing a message_id.", state.game_id)
                await app_state.store.delete_round(state.game_id)

        for state in pending_questions:
            if state.prompt_message_id is not None:
                app_state.pending_questions[state.game_id] = state
                self.add_view(SixtyNineQuestionView(state.game_id), message_id=state.prompt_message_id)
            else:
                log.warning(
                    "Pending question for game %s is missing a prompt_message_id.",
                    state.game_id,
                )
                await app_state.store.delete_pending_question(state.game_id)

        for posted in posted_questions:
            app_state.posted_questions[posted.message_id] = posted
            self.add_view(QuestionReplyView(), message_id=posted.message_id)

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

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        guild_id = guild.id
        log.info("Removed from guild %s; clearing all stored state.", guild_id)

        app_state.ping_roles.pop(guild_id, None)
        app_state.min_game_seconds.pop(guild_id, None)

        for game_id in [gid for gid, s in app_state.active_games.items() if s.guild_id == guild_id]:
            task = app_state.auto_close_tasks.pop(game_id, None)
            if task:
                task.cancel()
            app_state.active_games.pop(game_id, None)

        for game_id in [gid for gid, s in app_state.pending_questions.items() if s.guild_id == guild_id]:
            app_state.pending_questions.pop(game_id, None)

        for message_id in [mid for mid, s in app_state.posted_questions.items() if s.guild_id == guild_id]:
            app_state.posted_questions.pop(message_id, None)

        try:
            await app_state.store.delete_guild_data(guild_id)
        except Exception:
            log.exception("Failed to delete stored data for guild %s.", guild_id)


bot = Bot()
