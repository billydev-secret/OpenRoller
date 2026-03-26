import asyncio
import logging

import discord
from discord import app_commands

from . import state as app_state
from .formatters import build_embed
from .models import RiskyRollState
from .store import MAX_GAMES_PER_CHANNEL
from .views import RiskyRollView, SixtyNineQuestionView, auto_close_round, disable_pending_question_message, disable_round_message

log = logging.getLogger(__name__)


async def _start_game(
    interaction: discord.Interaction,
    auto_close_players: int | None,
    auto_close_minutes: int | None,
    ping: bool,
    skip_min_game_time: bool,
) -> None:
    """Shared implementation for risky_start and risky_start_no_ping."""
    if interaction.guild is None or interaction.channel is None:
        await interaction.response.send_message(
            "This command can only be used in a server channel.",
            ephemeral=True,
        )
        return

    me = interaction.guild.me
    perms = interaction.channel.permissions_for(me)
    missing = [
        name for allowed, name in [
            (perms.send_messages, "Send Messages"),
            (perms.read_message_history, "Read Message History"),
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
        if active_in_channel >= MAX_GAMES_PER_CHANNEL:
            await interaction.response.send_message(
                f"This channel already has {MAX_GAMES_PER_CHANNEL} active games. "
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
        await app_state.store.save_round(state)

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
                embed=build_embed(state),
                view=view,
                allowed_mentions=allowed_mentions,
            )
            message = await interaction.original_response()
            state.message_id = message.id
            await app_state.store.save_round(state)

            if auto_close_minutes and auto_close_minutes > 0:
                _client = interaction.client
                _game_id = state.game_id
                _minutes = auto_close_minutes

                async def _timed_close() -> None:
                    await asyncio.sleep(_minutes * 60)
                    await auto_close_round(_client, _game_id)

                task = asyncio.create_task(_timed_close())
                app_state.auto_close_tasks[state.game_id] = task
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
                            embed=build_embed(state),
                            view=failed_view,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        pass
            raise


def setup(bot: discord.Client) -> None:
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
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
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
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        if seconds < 0:
            await interaction.response.send_message(
                "Minimum game time cannot be negative.",
                ephemeral=True,
            )
            return

        if seconds == 0:
            app_state.min_game_seconds.pop(interaction.guild.id, None)
            await app_state.store.set_min_game_time(interaction.guild.id, None)
            await interaction.response.send_message(
                "Minimum game time disabled.",
                ephemeral=True,
            )
        else:
            app_state.min_game_seconds[interaction.guild.id] = seconds
            await app_state.store.set_min_game_time(interaction.guild.id, seconds)
            await interaction.response.send_message(
                f"Minimum game time set to {seconds} second(s).",
                ephemeral=True,
            )

    @bot.tree.command(
        name="risky_reset_state",
        description="Clear all active rounds and pending prompts in this channel",
    )
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    async def risky_reset_state(interaction: discord.Interaction):
        if interaction.channel is None:
            await interaction.response.send_message(
                "This command can only be used in a server channel.",
                ephemeral=True,
            )
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

            if not game_ids and not question_ids:
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

            await interaction.response.send_message(
                "Reset the Risky Rolls state for this channel.",
                ephemeral=True,
            )

    @bot.tree.error
    async def on_app_command_error(
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            if interaction.response.is_done():
                await interaction.followup.send(
                    "You do not have permission to use that command.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "You do not have permission to use that command.",
                    ephemeral=True,
                )
            return

        log.exception("Unhandled app command error", exc_info=error)
        if interaction.response.is_done():
            await interaction.followup.send(
                "The command failed. Check the bot logs for details.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "The command failed. Check the bot logs for details.",
                ephemeral=True,
            )
