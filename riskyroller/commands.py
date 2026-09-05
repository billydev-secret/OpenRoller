import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from . import state as app_state
from .config import DEFAULT_MAX_GAMES_PER_CHANNEL
from .formatters import build_embed
from .models import RiskyRollState
from .views import RiskyRollView, disable_pending_question_message, disable_round_message, schedule_auto_close

if TYPE_CHECKING:
    from .bot import Bot

log = logging.getLogger(__name__)


async def _send_ephemeral(interaction: discord.Interaction, message: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


async def _start_game(
    interaction: discord.Interaction,
    auto_close_players: int | None,
    auto_close_minutes: int | None,
    ping: bool,
    skip_min_game_time: bool,
) -> None:
    """Shared implementation for risky_start and risky_start_no_ping."""
    if interaction.guild is None or interaction.channel is None:
        await _send_ephemeral(interaction, "This command can only be used in a server channel.")
        return

    me = interaction.guild.me
    perms = interaction.channel.permissions_for(me)
    missing = [
        name for allowed, name in [
            (perms.send_messages, "Send Messages"),
            (perms.embed_links, "Embed Links"),
        ]
        if not allowed
    ]
    if missing:
        await interaction.response.send_message(
            f"I'm missing permissions in this channel: {', '.join(missing)}. "
            "Please fix my permissions before starting a round.",
            ephemeral=True,
        )
        return

    async with app_state.get_channel_lock(interaction.channel.id):
        active_in_channel = sum(
            1 for s in app_state.active_games.values()
            if s.channel_id == interaction.channel.id
        )
        cap = app_state.max_games_per_channel.get(interaction.guild.id, DEFAULT_MAX_GAMES_PER_CHANNEL)
        if active_in_channel >= cap:
            await interaction.response.send_message(
                f"This channel already has {cap} active game{'s' if cap != 1 else ''}. "
                "Close one before starting another.",
                ephemeral=True,
            )
            return

        state = RiskyRollState(
            channel_id=interaction.channel.id,
            guild_id=interaction.guild.id,
            opener_id=interaction.user.id,
            auto_close_players=auto_close_players if auto_close_players and auto_close_players >= 2 else None,
            auto_close_minutes=auto_close_minutes if auto_close_minutes and auto_close_minutes > 0 else None,
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

            if auto_close_minutes and auto_close_minutes > 0:
                app_state.auto_close_tasks[state.game_id] = asyncio.create_task(
                    schedule_auto_close(interaction.client, state.game_id, auto_close_minutes * 60)
                )
        except Exception:
            app_state.active_games.pop(state.game_id, None)
            await app_state.store.delete_round(state.game_id)
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
                            content="Risky Rolls could not finish setup. Start a new round.",
                            embed=build_embed(state, interaction.guild),
                            view=failed_view,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        pass
            raise


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
        auto_close_players: int | None = 25,
        auto_close_minutes: int | None = 120,
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
        auto_close_players: int | None = 25,
        auto_close_minutes: int | None = 120,
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
            await _send_ephemeral(interaction, "This command can only be used in a server.")
            return

        app_state.ping_roles[interaction.guild.id] = role.id
        await app_state.store.set_ping_role(interaction.guild.id, role.id)

        await interaction.response.send_message(
            f"Ping role set to {role.mention}",
            allowed_mentions=discord.AllowedMentions(roles=True),
            ephemeral=True,
        )

    @bot.tree.command(
        name="risky_set_min_game_time",
        description="Set the minimum time before a round can be manually closed",
    )
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(seconds="Minimum seconds a round must be open before closing (0 to disable)")
    async def risky_set_min_game_time(interaction: discord.Interaction, seconds: int):
        if interaction.guild is None:
            await _send_ephemeral(interaction, "This command can only be used in a server.")
            return

        if seconds < 0:
            await _send_ephemeral(interaction, "Minimum game time cannot be negative.")
            return

        # 0 is stored as 0, not cleared: an absent value means "use the
        # default", and disabling the minimum must not quietly restore it.
        app_state.min_game_seconds[interaction.guild.id] = seconds
        await app_state.store.set_min_game_time(interaction.guild.id, seconds)
        if seconds == 0:
            await _send_ephemeral(interaction, "Minimum game time disabled.")
        else:
            await _send_ephemeral(interaction, f"Minimum game time set to {seconds} second(s).")

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
        if interaction.guild is None:
            await _send_ephemeral(interaction, "This command can only be used in a server.")
            return

        if count < 0:
            await _send_ephemeral(interaction, "Max games per channel cannot be negative.")
            return

        if count == 0:
            app_state.max_games_per_channel.pop(interaction.guild.id, None)
            await app_state.store.set_max_games_per_channel(interaction.guild.id, None)
            await _send_ephemeral(
                interaction,
                f"Max games per channel reset to the default ({DEFAULT_MAX_GAMES_PER_CHANNEL}).",
            )
        else:
            app_state.max_games_per_channel[interaction.guild.id] = count
            await app_state.store.set_max_games_per_channel(interaction.guild.id, count)
            await _send_ephemeral(interaction, f"Max games per channel set to {count}.")

    @bot.tree.command(
        name="risky_reset_state",
        description="Clear all active rounds and pending prompts in this channel",
    )
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    async def risky_reset_state(interaction: discord.Interaction):
        if interaction.channel is None:
            await _send_ephemeral(interaction, "This command can only be used in a server channel.")
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
                await interaction.response.send_message(
                    "No active or pending Risky Rolls state was found in this channel.",
                    ephemeral=True,
                )
                return

            for game_id in game_ids:
                task = app_state.auto_close_tasks.pop(game_id, None)
                if task:
                    task.cancel()
                state = app_state.active_games.pop(game_id, None)
                if state is not None:
                    state.is_open = False
                    await disable_round_message(state, interaction.channel)
                await app_state.store.delete_round(game_id)

            for game_id in question_ids:
                pending_state = app_state.pending_questions.pop(game_id, None)
                if pending_state is not None:
                    await disable_pending_question_message(
                        interaction.client,
                        pending_state,
                        "The pending question prompt was cleared by an administrator.",
                    )
                await app_state.store.delete_pending_question(game_id)

            for message_id in posted_message_ids:
                app_state.posted_questions.pop(message_id, None)
                await app_state.store.delete_posted_question(message_id)

            await interaction.response.send_message(
                "Reset the Risky Rolls state for this channel.",
                ephemeral=True,
            )

    @bot.tree.command(
        name="invite",
        description="Get an invite link to add Risky Rolls to your server",
    )
    async def invite(interaction: discord.Interaction):
        if interaction.client.user is None:
            await _send_ephemeral(interaction, "Bot is not ready yet. Try again in a moment.")
            return

        url = discord.utils.oauth_url(
            interaction.client.user.id,
            permissions=discord.Permissions(
                send_messages=True,
                embed_links=True,
                create_public_threads=True,
                send_messages_in_threads=True,
            ),
            scopes=["bot", "applications.commands"],
        )
        await interaction.response.send_message(
            f"[Click here to add Risky Rolls to your server!]({url})",
            ephemeral=True,
        )

    @bot.tree.command(
        name="support",
        description="Get a link to the Risky Rolls support Discord server",
    )
    async def support(interaction: discord.Interaction):
        await interaction.response.send_message(
            "[Join the Risky Rolls support server](https://discord.gg/7gfbYYkH)",
            ephemeral=True,
        )

    @bot.tree.error
    async def on_app_command_error(
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            await _send_ephemeral(interaction, "You do not have permission to use that command.")
            return

        log.exception("Unhandled app command error", exc_info=error)
        await _send_ephemeral(interaction, "The command failed. Check the bot logs for details.")
