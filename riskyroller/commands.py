import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from . import state as app_state
from .config import DEFAULT_MAX_GAMES_PER_CHANNEL, SUPPORT_INVITE_URL
from .formatters import build_embed, format_duration, join_names, permission_help
from .invite import invite_url
from .logic import missing_start_permissions
from .models import RiskyRollState
from .views import (
    RiskyRollView,
    arm_auto_close,
    disable_pending_question_message,
    disable_round_message,
    forget_stored,
)

if TYPE_CHECKING:
    from .bot import Bot

log = logging.getLogger(__name__)

NOT_IN_SERVER_CHANNEL_TEXT = (
    "Risky Rolls only works inside a server channel, not in DMs — run this in the channel where "
    "you want the round."
)
NOT_IN_SERVER_TEXT = "This setting belongs to a server — run the command inside the server you want to change."
NOT_A_TEXT_CHANNEL_TEXT = (
    "Risky Rolls needs an ordinary text channel or a thread. This one is a voice or stage channel's "
    "built-in chat, where I can't reliably post the result after the round closes. Start the round in "
    "a text channel instead — everyone in the voice call can still play."
)
# Reached only after the round message itself posted, so permissions are not
# the problem; the log is the only lead.
SETUP_FAILED_TEXT = (
    "Risky Rolls couldn't finish setting up this round, so it was cancelled and nothing is open. "
    "Try starting it again. If it keeps happening, whoever hosts this bot can find the error in its log."
)
# Applied only when a round would otherwise have neither close path armed —
# see the note in _start_game. Generous on purpose: this is a backstop
# against a round that never ends, not a policy about how long one should run.
MAX_ROUND_LIFETIME_MINUTES = 24 * 60
# Command-layer ceilings on the two /risky_start options, generous enough that
# no real round would ever hit them — just wide enough to stop a stray extra
# digit (or a negative) from being accepted as a value at all. 0 still means
# "disable this path" on both.
MAX_AUTO_CLOSE_PLAYERS = 100
MAX_AUTO_CLOSE_MINUTES = 7 * 24 * 60


async def _send_ephemeral(interaction: discord.Interaction, message: str) -> None:
    # Refusals never need to ping anyone they name.
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    else:
        await interaction.response.send_message(message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


async def _start_game(
    interaction: discord.Interaction,
    auto_close_players: int | None,
    auto_close_minutes: int | None,
    ping: bool,
    skip_min_game_time: bool,
) -> None:
    """Shared implementation for risky_start and risky_start_no_ping."""
    if interaction.guild is None or interaction.channel is None:
        await _send_ephemeral(interaction, NOT_IN_SERVER_CHANNEL_TEXT)
        return

    if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
        # Every step after the opening message re-finds the channel by id, and
        # that lookup only accepts a text channel or a thread. In a voice or
        # stage channel's built-in chat the round would open and take rolls,
        # then auto-close would fail to find the channel and delete the round
        # with no result posted at all. Refuse up front instead.
        await _send_ephemeral(interaction, NOT_A_TEXT_CHANNEL_TEXT)
        return

    if interaction.client.get_guild(interaction.guild.id) is None:
        # discord.py synthesises interaction.guild.me on every interaction
        # (Interaction._from_data adds it from the client's own user whenever
        # it's missing), so it's never None and can't tell a commands-only
        # install (slash commands added without the bot account) from a
        # normal one. Whether the client's gateway cache has this guild at
        # all is a real signal instead: a commands-only install never gets a
        # GUILD_CREATE for it, so it's never cached — but the same "not
        # cached" shape also happens for a few seconds after a fresh
        # reconnect, for a guild the bot is genuinely in. So this refusal
        # hedges rather than asserting, and suggests a retry first: a false
        # positive here costs one retry, not a wrong claim that re-adding the
        # bot is required.
        client = interaction.client
        application_id = client.application_id or (client.user.id if client.user else None)
        link = f" If re-adding fixes it, this link keeps your settings: <{invite_url(application_id)}>" if application_id else ""
        await _send_ephemeral(
            interaction,
            "I don't see a bot account for me in this server, so I might not be able to post the round or "
            "anything after it. If I was just added to this server, or I just reconnected to Discord, wait "
            f"a few seconds and try again.{link}",
        )
        return

    missing = missing_start_permissions(interaction.app_permissions)
    if missing:
        command = interaction.command.qualified_name if interaction.command else "risky_start"
        await _send_ephemeral(
            interaction,
            f"I can't run a round here. {permission_help(missing)} Once that's done, /{command} will work "
            "in this channel.",
        )
        return

    async with app_state.get_channel_lock(interaction.channel.id):
        active_in_channel = sum(
            1 for s in app_state.active_games.values()
            if s.channel_id == interaction.channel.id
        )
        cap = app_state.max_games_per_channel.get(interaction.guild.id, DEFAULT_MAX_GAMES_PER_CHANNEL)
        if active_in_channel >= cap:
            await _send_ephemeral(
                interaction,
                f"This channel already has {active_in_channel} open round{'s' if active_in_channel != 1 else ''}; "
                f"this server allows {cap} at once. Wait for one to auto-close, have its opener press "
                "**Close Round** once two players have rolled (and any minimum time has passed), or ask an "
                "admin to clear the channel with /risky_reset_state or raise the limit with /risky_set_max_games.",
            )
            return

        close_players = auto_close_players if auto_close_players and auto_close_players >= 2 else None
        close_minutes = auto_close_minutes if auto_close_minutes and auto_close_minutes > 0 else None
        if close_players is None and close_minutes is None:
            # Both close paths off leaves nothing to ever end the round: there's
            # no message/channel-delete listener and no sweep of active_rounds,
            # and setup_hook restores it across every restart, so it would lock
            # this channel's slot forever. Falling back to a ceiling still lets
            # either close path work exactly as asked when only one is off.
            close_minutes = MAX_ROUND_LIFETIME_MINUTES

        state = RiskyRollState(
            channel_id=interaction.channel.id,
            guild_id=interaction.guild.id,
            opener_id=interaction.user.id,
            auto_close_players=close_players,
            auto_close_minutes=close_minutes,
            skip_min_game_time=skip_min_game_time,
        )
        app_state.active_games[state.game_id] = state

        content = None
        allowed_mentions = discord.AllowedMentions.none()

        if ping:
            role_id = app_state.ping_roles.get(interaction.guild.id)
            if role_id:
                content = f"# <@&{role_id}> A new Risky Rolls round has begun!"
                allowed_mentions = discord.AllowedMentions(roles=True)

        view = RiskyRollView(state.game_id)
        try:
            await interaction.response.send_message(
                content=content,
                embed=build_embed(state, interaction.guild),
                view=view,
                allowed_mentions=allowed_mentions,
            )
            message = await interaction.original_response()
            state.message_id = message.id
            await app_state.store.save_round(state)

            if close_minutes:
                arm_auto_close(interaction.client, state.game_id, close_minutes * 60)
        except Exception:
            app_state.active_games.pop(state.game_id, None)
            try:
                await app_state.store.delete_round(state.game_id)
            except Exception:
                # Don't let a store failure here replace the original error
                # (that's the one worth seeing in the log) or skip disabling
                # the half-posted message below.
                log.exception("Failed to delete round %s from the store during start cleanup.", state.game_id)
            state.is_open = False

            if interaction.response.is_done():
                try:
                    message = await interaction.original_response()
                except (discord.NotFound, discord.HTTPException):
                    pass
                else:
                    failed_view = RiskyRollView(state.game_id)
                    failed_view.disable_all_items()
                    try:
                        await message.edit(
                            content=SETUP_FAILED_TEXT,
                            embed=build_embed(state, interaction.guild),
                            view=failed_view,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        pass
            raise


async def _set_max_games_per_channel(interaction: discord.Interaction, count: int) -> None:
    """Shared implementation for /risky_set_max_games."""
    if interaction.guild is None:
        await _send_ephemeral(interaction, NOT_IN_SERVER_TEXT)
        return

    if count < 0:
        await _send_ephemeral(
            interaction,
            f"Max games per channel can't be negative. Use 0 to restore the default "
            f"({DEFAULT_MAX_GAMES_PER_CHANNEL}), or the number of rounds that may be open at once.",
        )
        return

    if count == 0:
        app_state.max_games_per_channel.pop(interaction.guild.id, None)
        await app_state.store.set_max_games_per_channel(interaction.guild.id, None)
        await _send_ephemeral(
            interaction,
            f"Max games per channel reset to the default ({DEFAULT_MAX_GAMES_PER_CHANNEL}). /risky_start "
            "refuses once a channel has that many open rounds.",
        )
    else:
        app_state.max_games_per_channel[interaction.guild.id] = count
        await app_state.store.set_max_games_per_channel(interaction.guild.id, count)
        await _send_ephemeral(
            interaction,
            f"Max games per channel set to {count}. /risky_start refuses once a channel has that many "
            "open rounds.",
        )


async def _reset_channel_state(interaction: discord.Interaction) -> None:
    """Shared implementation for /risky_reset_state.

    Defers immediately: the cleanup below can be dozens of message edits (one
    per stale round/prompt), easily enough to miss Discord's 3-second initial
    response window, and pending_questions has no cap on how many a channel
    can accumulate.
    """
    await interaction.response.defer(ephemeral=True)

    if interaction.channel is None:
        await _send_ephemeral(interaction, NOT_IN_SERVER_CHANNEL_TEXT)
        return

    async with app_state.get_channel_lock(interaction.channel.id):
        channel_id = interaction.channel.id

        game_ids = [
            gid for gid, s in app_state.active_games.items()
            if s.channel_id == channel_id
        ]
        question_ids = [
            gid for gid, s in app_state.pending_questions.items()
            if s.channel_id == channel_id
        ]
        posted_message_ids = [
            mid for mid, s in app_state.posted_questions.items()
            if s.channel_id == channel_id
        ]

        if not game_ids and not question_ids and not posted_message_ids:
            await _send_ephemeral(
                interaction,
                "Nothing to reset here: this channel has no open round, no question prompt waiting on a "
                "winner, and no question waiting on a reply.",
            )
            return

        # Each game is torn down under its own game lock — the same lock the
        # roll and close buttons and auto_close_round hold for their whole
        # critical section. Without it a roll already in flight can finish
        # afterwards and re-edit the round message with its buttons enabled,
        # or a running auto-close can post a winner prompt, for a round this
        # reset has just deleted. (bot.py's guild teardown does the same.)
        for game_id in game_ids:
            async with app_state.get_game_lock(game_id):
                task = app_state.auto_close_tasks.pop(game_id, None)
                if task:
                    task.cancel()
                state = app_state.active_games.pop(game_id, None)
                if state is not None:
                    state.is_open = False
                    await disable_round_message(state, interaction.channel)
                await forget_stored(app_state.store.delete_round, game_id, f"round {game_id}")

        for game_id in question_ids:
            async with app_state.get_game_lock(game_id):
                pending_state = app_state.pending_questions.pop(game_id, None)
            if pending_state is not None:
                await disable_pending_question_message(
                    interaction.client,
                    pending_state,
                    "This question prompt was cancelled by an administrator's reset. Start a new round to play again.",
                )
            await forget_stored(app_state.store.delete_pending_question, game_id, f"prompt {game_id}")

        for message_id in posted_message_ids:
            # Under the message lock, like the reply modal holds for its whole
            # body — otherwise a reply already in flight posts publicly, and
            # tells its author it was sent, after the admin reset it.
            async with app_state.get_message_lock(message_id):
                app_state.posted_questions.pop(message_id, None)
                await forget_stored(
                    app_state.store.delete_posted_question, message_id, f"posted question {message_id}"
                )

        def plural(n: int, noun: str) -> str:
            return f"{n} {noun}{'s' if n != 1 else ''}"

        parts = [
            text
            for count, text in (
                (len(game_ids), f"closed {plural(len(game_ids), 'round')}"),
                (len(question_ids), f"cancelled {plural(len(question_ids), 'question prompt')}"),
                (len(posted_message_ids), f"cleared {plural(len(posted_message_ids), 'unanswered question')}"),
            )
            if count
        ]
        await _send_ephemeral(
            interaction,
            f"Reset this channel: {join_names(parts)}. Start a new round with /risky_start.",
        )


def setup(bot: "Bot") -> None:
    @bot.tree.command(
        name="risky_start",
        description="Open a new Risky Rolls round in this channel",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        auto_close_players="Auto-close when this many players have rolled",
        auto_close_minutes="Auto-close after this many minutes",
    )
    async def risky_start(
        interaction: discord.Interaction,
        auto_close_players: app_commands.Range[int, 0, MAX_AUTO_CLOSE_PLAYERS] | None = 25,
        auto_close_minutes: app_commands.Range[int, 0, MAX_AUTO_CLOSE_MINUTES] | None = 120,
    ):
        await _start_game(
            interaction,
            auto_close_players=auto_close_players,
            auto_close_minutes=auto_close_minutes,
            ping=True,
            skip_min_game_time=False,
        )

    @bot.tree.command(
        name="risky_start_no_ping",
        description="Open a new Risky Rolls round without pinging and without a minimum game time",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        auto_close_players="Auto-close when this many players have rolled",
        auto_close_minutes="Auto-close after this many minutes",
    )
    async def risky_start_no_ping(
        interaction: discord.Interaction,
        auto_close_players: app_commands.Range[int, 0, MAX_AUTO_CLOSE_PLAYERS] | None = 25,
        auto_close_minutes: app_commands.Range[int, 0, MAX_AUTO_CLOSE_MINUTES] | None = 120,
    ):
        await _start_game(
            interaction,
            auto_close_players=auto_close_players,
            auto_close_minutes=auto_close_minutes,
            ping=False,
            skip_min_game_time=True,
        )

    @bot.tree.command(
        name="risky_set_ping",
        description="Set the role pinged when a new round starts",
    )
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(role="Role to mention at the start of each new round")
    async def risky_set_ping(interaction: discord.Interaction, role: discord.Role):
        if interaction.guild is None:
            await _send_ephemeral(interaction, NOT_IN_SERVER_TEXT)
            return

        app_state.ping_roles[interaction.guild.id] = role.id
        await app_state.store.set_ping_role(interaction.guild.id, role.id)

        await _send_ephemeral(
            interaction,
            f"Ping role set to {role.mention}. Every /risky_start will mention it (its members are only "
            "notified if the role is mentionable or I have Mention Everyone); /risky_start_no_ping won't.",
        )

    @bot.tree.command(
        name="risky_set_min_game_time",
        description="Set the minimum time before a round can close",
    )
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(seconds="Minimum seconds a round must be open before closing (0 to disable)")
    async def risky_set_min_game_time(interaction: discord.Interaction, seconds: int):
        if interaction.guild is None:
            await _send_ephemeral(interaction, NOT_IN_SERVER_TEXT)
            return

        if seconds < 0:
            await _send_ephemeral(
                interaction,
                "Minimum game time can't be negative. Use 0 to disable the minimum, or the number of seconds "
                "a round must stay open (for example 1800 for 30 minutes).",
            )
            return

        # 0 is stored as 0, not cleared: an absent value means "use the
        # default", and disabling the minimum must not quietly restore it.
        app_state.min_game_seconds[interaction.guild.id] = seconds
        await app_state.store.set_min_game_time(interaction.guild.id, seconds)
        if seconds == 0:
            await _send_ephemeral(
                interaction,
                "Minimum game time disabled — a round can be closed as soon as two players have rolled.",
            )
        else:
            await _send_ephemeral(
                interaction,
                f"Minimum game time set to {format_duration(seconds)}. No round opened with /risky_start "
                "closes before then — not by hand, not by the player count, and not by a minutes "
                "auto-close set shorter than this, which waits for the minimum too. Rounds opened with "
                "/risky_start_no_ping skip the minimum entirely.",
            )

    @bot.tree.command(
        name="risky_set_max_games",
        description="Set how many rounds can be open in one channel at a time",
    )
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        count=f"Open rounds allowed per channel (0 to restore the default of {DEFAULT_MAX_GAMES_PER_CHANNEL})"
    )
    async def risky_set_max_games(interaction: discord.Interaction, count: int):
        await _set_max_games_per_channel(interaction, count)

    @bot.tree.command(
        name="risky_reset_state",
        description="Clear all active rounds and pending prompts in this channel",
    )
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    async def risky_reset_state(interaction: discord.Interaction):
        await _reset_channel_state(interaction)

    @bot.tree.command(
        name="invite",
        description="Get an invite link to add Risky Rolls to your server",
    )
    async def invite(interaction: discord.Interaction):
        client = interaction.client
        application_id = client.application_id or (client.user.id if client.user else None)
        if application_id is None:
            await _send_ephemeral(interaction, "I'm still connecting to Discord — try /invite again in a few seconds.")
            return

        url = invite_url(application_id)
        await interaction.response.send_message(
            f"[Click here to add Risky Rolls to your server!]({url})",
            ephemeral=True,
        )

    @bot.tree.command(
        name="support",
        description="Get the support server link, if this bot's host has set one",
    )
    async def support(interaction: discord.Interaction):
        if SUPPORT_INVITE_URL is None:
            await _send_ephemeral(
                interaction,
                "This copy of Risky Rolls has no support server set up. Whoever hosts it can add one with "
                "the SUPPORT_INVITE_URL setting; until then, questions go to your server's admins.",
            )
            return
        await _send_ephemeral(interaction, f"[Join the Risky Rolls support server]({SUPPORT_INVITE_URL})")

    @bot.tree.error
    async def on_app_command_error(
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            await _send_ephemeral(
                interaction,
                "That command is for server administrators — it changes how Risky Rolls runs on this server. "
                "Ask an admin to run it.",
            )
            return

        log.exception("Unhandled app command error", exc_info=error)
        await _send_ephemeral(
            interaction,
            "That command hit an error on my side — try it again. If it keeps failing, whoever hosts this "
            "bot can find the details in its log.",
        )
