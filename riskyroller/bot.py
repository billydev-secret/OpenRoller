import asyncio
import logging
import time

import discord
from discord import app_commands

from . import commands
from . import state as app_state
from .config import DEBUG, DEBUG_GUILD_ID, DEFAULT_MIN_GAME_SECONDS, SYNC_COMMANDS_ON_STARTUP
from .invite import invite_url
from .logic import effective_min_game_seconds
from .views import QuestionReplyView, RiskyRollView, SixtyNineQuestionView, schedule_auto_close

log = logging.getLogger(__name__)

intents = discord.Intents.default()

QUESTION_TTL_SECONDS = 7 * 24 * 60 * 60


class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self._invite_logged = False
        self._startup_guild_sweep_done = False
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
        (
            ping_roles,
            min_game_times,
            max_games,
            active_rounds,
            pending_questions,
            posted_questions,
        ) = await asyncio.gather(
            app_state.store.load_ping_roles(),
            app_state.store.load_min_game_times(),
            app_state.store.load_max_games_per_channel(),
            app_state.store.load_active_rounds(),
            app_state.store.load_pending_questions(),
            app_state.store.load_posted_questions(),
        )
        app_state.ping_roles.update(ping_roles)
        app_state.min_game_seconds.update(min_game_times)
        app_state.max_games_per_channel.update(max_games)

        for state in active_rounds:
            if state.message_id is not None:
                app_state.active_games[state.game_id] = state
                self.add_view(RiskyRollView(state.game_id), message_id=state.message_id)

                if state.auto_close_players and len(state.rolls) >= state.auto_close_players:
                    elapsed = time.time() - state.created_at
                    min_seconds = effective_min_game_seconds(
                        app_state.min_game_seconds, state.guild_id, state.skip_min_game_time, DEFAULT_MIN_GAME_SECONDS
                    )
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

        # A question or prompt swept above for age (or from before this run
        # entirely) still has live buttons on its original Discord message,
        # but load_pending_questions/load_posted_questions never return the
        # swept row, so nothing above calls add_view for that exact
        # message_id. Without a fallback, discord.py has no view to route
        # that press to at all — the presser gets Discord's bare "This
        # interaction failed" and the bot never gets a chance to explain.
        # Registering one instance of each view without a message_id is
        # discord.py's documented catch-all: it's matched by custom_id for
        # any press whose message_id has no specific registration. Both
        # callbacks already look up their state by the interaction's own
        # message id (QuestionReplyView) or an empty game_id
        # (SixtyNineQuestionView) and answer with a real explanation
        # (PROMPT_GONE_TEXT / REPLY_CLOSED_TEXT) when it's missing, so
        # registering the fallback is the whole fix. Active rounds aren't
        # swept by age, so RiskyRollView has no equivalent gap to cover.
        self.add_view(SixtyNineQuestionView())
        self.add_view(QuestionReplyView())

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
        self._log_invite_link_once()
        await self._sweep_guilds_left_while_offline()

    def _log_invite_link_once(self) -> None:
        """Print the invite link on the first ready so a fresh install needs no other tool.

        on_ready fires again after every reconnect; the link is logged once.
        """
        if self._invite_logged:
            return
        application_id = self.application_id or (self.user.id if self.user else None)
        if application_id is None:
            return
        self._invite_logged = True
        url = invite_url(application_id)
        if self.guilds:
            log.info("Invite link (bot + slash commands): %s", url)
        else:
            log.warning("This bot is not in any server yet. Open this link to add it: %s", url)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        guild_id = guild.id
        log.info("Removed from guild %s; clearing all stored state.", guild_id)
        await self._drop_guild_state(guild_id)

    async def _drop_guild_state(self, guild_id: int) -> None:
        """Clear every in-memory and stored record of one guild.

        Shared by on_guild_remove and the startup sweep below, since both
        need to react to the same fact — the bot is no longer in this guild
        — discovered at different times.

        Each active game's pop/disable/delete happens under its own
        get_game_lock, the same lock roll_button, close_button and
        auto_close_round hold for their whole critical section. Without it,
        a roll or an auto-close already in flight for this guild could
        finish after teardown — re-editing the round message, saving a roll,
        or posting a winner prompt for a guild whose data this call is about
        to delete. Taking the lock first either waits for that in-flight
        work to finish (so delete_guild_data below cleans up whatever it
        wrote) or, if teardown gets there first, makes the in-flight
        operation find the game already gone once it acquires the lock.
        """
        app_state.ping_roles.pop(guild_id, None)
        app_state.min_game_seconds.pop(guild_id, None)
        app_state.max_games_per_channel.pop(guild_id, None)
        for key in [k for k in app_state.guild_display_names if k[0] == guild_id]:
            app_state.guild_display_names.pop(key, None)

        for game_id in [gid for gid, s in app_state.active_games.items() if s.guild_id == guild_id]:
            async with app_state.get_game_lock(game_id):
                task = app_state.auto_close_tasks.pop(game_id, None)
                if task:
                    task.cancel()
                app_state.active_games.pop(game_id, None)

        for game_id in [gid for gid, s in app_state.pending_questions.items() if s.guild_id == guild_id]:
            async with app_state.get_game_lock(game_id):
                app_state.pending_questions.pop(game_id, None)

        for message_id in [mid for mid, s in app_state.posted_questions.items() if s.guild_id == guild_id]:
            app_state.posted_questions.pop(message_id, None)

        try:
            await app_state.store.delete_guild_data(guild_id)
        except Exception:
            log.exception("Failed to delete stored data for guild %s.", guild_id)

    async def _sweep_guilds_left_while_offline(self) -> None:
        """Drop state for a guild that removed the bot while it was offline.

        on_guild_remove only fires for a removal the running bot is
        connected to see. setup_hook loads every guild's rounds, prompts and
        settings before the gateway has told us which guilds we're still
        in, so a guild that removed the bot between two runs is never
        cleaned up otherwise — its settings and rounds persist, and any
        round past its player threshold gets its auto-close timer re-armed
        on every restart, for a guild that can never see it fire. Runs once,
        on the first ready, once self.guilds is actually populated.
        """
        if self._startup_guild_sweep_done:
            return
        self._startup_guild_sweep_done = True

        current_guild_ids = {g.id for g in self.guilds}
        if not current_guild_ids:
            # Every guild would look departed. That is true of a fresh
            # install (where there is nothing to sweep anyway) and of a ready
            # that arrived without its guild list, where sweeping would
            # delete every server's data irrecoverably. Not worth the risk
            # for a cleanup this routine.
            return
        known_guild_ids = (
            set(app_state.ping_roles)
            | set(app_state.min_game_seconds)
            | set(app_state.max_games_per_channel)
            | {s.guild_id for s in app_state.active_games.values()}
            | {s.guild_id for s in app_state.pending_questions.values()}
            | {s.guild_id for s in app_state.posted_questions.values()}
        )
        for guild_id in known_guild_ids - current_guild_ids:
            log.info("Guild %s removed the bot while it was offline; clearing its stored state.", guild_id)
            await self._drop_guild_state(guild_id)


bot = Bot()
