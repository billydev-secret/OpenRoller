import asyncio
import logging

import discord
from discord import app_commands

from . import state as app_state
from .formatters import build_embed
from .models import RiskyRollState
from .views import RiskyRollView, SixtyNineQuestionView, auto_close_round, disable_pending_question_message, disable_round_message

log = logging.getLogger(__name__)


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
        auto_close_players: int | None = None,
        auto_close_minutes: int | None = None,
    ):
        if interaction.guild is None or interaction.channel is None:
            await interaction.response.send_message(
                "This command can only be used in a server channel.",
                ephemeral=True,
            )
            return

        async with app_state.get_channel_lock(interaction.channel.id):
            if interaction.channel.id in app_state.active_games:
                await interaction.response.send_message(
                    "A game is already active in this channel.",
                    ephemeral=True,
                )
                return

            state = RiskyRollState(
                channel_id=interaction.channel.id,
                guild_id=interaction.guild.id,
                opener_id=interaction.user.id,
                auto_close_players=auto_close_players if auto_close_players and auto_close_players >= 2 else None,
            )
            app_state.active_games[interaction.channel.id] = state
            await app_state.store.save_round(state)

            role_id = app_state.ping_roles.get(interaction.guild.id)
            content = None
            allowed_mentions = discord.AllowedMentions.none()

            if role_id:
                content = f"# <@&{role_id}> A new Risky Rolls round has begun!"
                allowed_mentions = discord.AllowedMentions(roles=True)

            view = RiskyRollView(interaction.channel.id)
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
                    async def _timed_close() -> None:
                        await asyncio.sleep(auto_close_minutes * 60)
                        await auto_close_round(interaction.client, interaction.channel.id)

                    task = asyncio.create_task(_timed_close())
                    app_state.auto_close_tasks[interaction.channel.id] = task
            except Exception:
                app_state.active_games.pop(interaction.channel.id, None)
                await app_state.store.delete_round(interaction.channel.id)
                state.is_open = False

                if interaction.response.is_done():
                    try:
                        message = await interaction.original_response()
                    except (discord.NotFound, discord.HTTPException):
                        pass
                    else:
                        failed_view = RiskyRollView(interaction.channel.id)
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
        name="risky_reset_state",
        description="Clear active round and pending prompts in this channel",
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
            task = app_state.auto_close_tasks.pop(interaction.channel.id, None)
            if task:
                task.cancel()

            state = app_state.active_games.pop(interaction.channel.id, None)
            pending_state = app_state.pending_questions.pop(interaction.channel.id, None)

            if state is None and pending_state is None:
                await app_state.store.delete_round(interaction.channel.id)
                await app_state.store.delete_pending_question(interaction.channel.id)
                await interaction.response.send_message(
                    "No active or pending Risky Rolls state was found in this channel.",
                    ephemeral=True,
                )
                return

            if state is not None:
                state.is_open = False
                await disable_round_message(state, interaction.channel)
                await app_state.store.delete_round(interaction.channel.id)

            if pending_state is not None:
                await disable_pending_question_message(
                    interaction.client,
                    pending_state,
                    "The pending 69 question prompt was cleared by an administrator.",
                )
                await app_state.store.delete_pending_question(interaction.channel.id)

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
