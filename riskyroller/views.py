import asyncio
import logging
import math
import random
import time

import discord

from . import state as app_state
from .config import DEFAULT_MIN_GAME_SECONDS
from .formatters import (
    build_embed,
    build_pending_prompt_content,
    build_pending_question_summary,
    format_user_mentions,
    get_text_channel,
    post_rolloff_embed,
)
from .models import PendingQuestionState, RiskyRollState, RoundResult

log = logging.getLogger(__name__)


async def auto_close_round(client: discord.Client, game_id: str) -> None:
    async with app_state.get_game_lock(game_id):
        app_state.auto_close_tasks.pop(game_id, None)

        state = app_state.active_games.get(game_id)
        if not state or not state.is_open:
            return

        channel_id = state.channel_id
        resolution = state.resolve()
        channel = await get_text_channel(client, channel_id)

        if resolution.result_type in (RoundResult.NOT_ENOUGH, RoundResult.WAITING_FOR_REROLLS):
            state.is_open = False
            app_state.active_games.pop(game_id, None)
            await app_state.store.delete_round(game_id)
            if channel is not None:
                await disable_round_message(state, channel)
                await channel.send("Round auto-closed: not enough players rolled.")
            return

        if resolution.rolloff_rounds:
            await post_rolloff_embed(
                channel,
                resolution.rolloff_user_ids,
                resolution.rolloff_rounds,
                state.highest_user,
                channel_id,
            )

        if resolution.lowest_rolloff_rounds:
            await post_rolloff_embed(
                channel,
                resolution.lowest_rolloff_user_ids,
                resolution.lowest_rolloff_rounds,
                state.lowest_user,
                channel_id,
                title="Lowest Roll Tiebreaker",
            )

        closed_view = RiskyRollView(game_id)
        closed_view.disable_all_items()

        channel_forbidden = False
        if state.message_id is not None and channel is not None:
            try:
                message = await channel.fetch_message(state.message_id)
                await message.edit(embed=build_embed(state), view=closed_view)
            except discord.Forbidden:
                channel_forbidden = True
                log.error(
                    "Auto-close: bot is missing access to #%s (game %s). "
                    "Check channel permissions and that the bot can access NSFW channels.",
                    getattr(channel, "name", channel_id), game_id,
                )
            except (discord.NotFound, discord.HTTPException):
                log.exception("Auto-close: failed to edit round message in #%s.", getattr(channel, "name", channel_id))

        app_state.active_games.pop(game_id, None)
        await app_state.store.delete_round(game_id)

        if channel is None:
            log.error("Auto-close: could not access channel %s; round closed with no prompt sent.", channel_id)
            return

        if channel_forbidden:
            log.error(
                "Auto-close: skipping winner prompt for game %s — bot has no access to #%s.",
                game_id, getattr(channel, "name", channel_id),
            )
            return

        if resolution.result_type in (RoundResult.SIXTYNINE, RoundResult.SIXTYNINE_TIE):
            prompt_state = PendingQuestionState(
                channel_id=channel_id,
                guild_id=state.guild_id,
                winner_id=state.highest_user,
                participant_user_ids=set(state.rolls),
                game_id=game_id,
                prompt_kind="room",
            )
        else:
            if state.lowest_user is None:
                log.warning("Auto-close: no lowest_user for game %s.", game_id)
                return
            prompt_state = PendingQuestionState(
                channel_id=channel_id,
                guild_id=state.guild_id,
                winner_id=state.highest_user,
                participant_user_ids={state.lowest_user},
                game_id=game_id,
                lowest_tie_user_ids=set(state.lowest_tie_user_ids),
                prompt_kind="direct",
            )

        question_view = SixtyNineQuestionView(game_id)
        prompt_message: discord.Message | None = None

        try:
            prompt_message = await channel.send(
                content=build_pending_prompt_content(prompt_state),
                allowed_mentions=discord.AllowedMentions(users=True),
                view=question_view,
            )
            prompt_state.prompt_message_id = prompt_message.id
            app_state.pending_questions[game_id] = prompt_state
            await app_state.store.save_pending_question(prompt_state)
        except discord.Forbidden:
            log.error(
                "Auto-close: bot is missing access to #%s (game %s). "
                "Check channel permissions and that the bot can access NSFW channels.",
                getattr(channel, "name", channel_id), game_id,
            )
        except Exception:
            log.exception("Auto-close: failed to send winner prompt for game %s.", game_id)
            app_state.pending_questions.pop(game_id, None)
            await app_state.store.delete_pending_question(game_id)
            if prompt_message is not None:
                await disable_pending_question_message(
                    client,
                    prompt_state,
                    "Risky Rolls could not prepare the question prompt. Start a new round.",
                )
            try:
                await channel.send("The round ended but the winner prompt could not be sent. Please start a new round.")
            except Exception:
                log.exception("Auto-close: also failed to send fallback message for game %s.", game_id)


class RiskyRollView(discord.ui.View):
    def __init__(self, game_id: str):
        super().__init__(timeout=None)
        self.game_id = game_id

    def disable_all_items(self) -> None:
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        log.exception("Unhandled error in RiskyRollView (game %s)", self.game_id, exc_info=error)
        msg = "Something went wrong. Please try again."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(
        label="Roll",
        style=discord.ButtonStyle.primary,
        custom_id="riskyroller:roll",
    )
    async def roll_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with app_state.get_game_lock(self.game_id):
            state = app_state.active_games.get(self.game_id)
            if not state or not state.is_open:
                await interaction.response.send_message("No open round to roll in.", ephemeral=True)
                return

            if not state.can_roll(interaction.user.id):
                if state.reroll_user_ids:
                    await interaction.response.send_message("You cannot reroll right now.", ephemeral=True)
                    return
                await interaction.response.send_message("You already rolled this round.", ephemeral=True)
                return

            roll = random.randint(1, 100)
            state.add_roll(interaction.user.id, roll)
            await app_state.store.save_round(state)

            log.info(
                "Channel #%s: %s rolled %s",
                getattr(interaction.channel, "name", state.channel_id),
                interaction.user.display_name,
                roll,
            )

            await interaction.response.edit_message(embed=build_embed(state), view=self)

            if state.auto_close_players and len(state.rolls) >= state.auto_close_players:
                task = app_state.auto_close_tasks.pop(self.game_id, None)
                if task:
                    task.cancel()
                elapsed = time.time() - state.created_at
                min_seconds = 0 if state.skip_min_game_time else app_state.min_game_seconds.get(state.guild_id, DEFAULT_MIN_GAME_SECONDS)
                delay = max(0.0, min_seconds - elapsed)
                _client = interaction.client
                _game_id = self.game_id

                async def _deferred_close(client=_client, game_id=_game_id, d=delay) -> None:
                    if d > 0:
                        await asyncio.sleep(d)
                    await auto_close_round(client, game_id)

                close_task = asyncio.create_task(_deferred_close())
                app_state.auto_close_tasks[self.game_id] = close_task

    @discord.ui.button(
        label="Close Round",
        style=discord.ButtonStyle.danger,
        custom_id="riskyroller:close",
    )
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with app_state.get_game_lock(self.game_id):
            state = app_state.active_games.get(self.game_id)
            if not state or not state.is_open:
                await interaction.response.send_message("No active game.", ephemeral=True)
                return

            is_admin = isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator
            if interaction.user.id != state.opener_id and not is_admin:
                await interaction.response.send_message(
                    "Only the round opener can close this round.",
                    ephemeral=True,
                )
                return

            min_seconds = app_state.min_game_seconds.get(state.guild_id)
            if min_seconds:
                elapsed = time.time() - state.created_at
                remaining = math.ceil(min_seconds - elapsed)
                if remaining > 0:
                    await interaction.response.send_message(
                        f"This round cannot be closed yet. Please wait {remaining} more second(s).",
                        ephemeral=True,
                    )
                    return

            resolution = state.resolve()

            if resolution.result_type == RoundResult.WAITING_FOR_REROLLS:
                await interaction.response.send_message(
                    f"Still waiting for {state.pending_reroll_mentions()} to reroll.",
                    allowed_mentions=discord.AllowedMentions(users=True),
                    ephemeral=True,
                )
                return

            if resolution.result_type == RoundResult.NOT_ENOUGH:
                await interaction.response.send_message("At least 2 players must roll.", ephemeral=True)
                return

            task = app_state.auto_close_tasks.pop(self.game_id, None)
            if task:
                task.cancel()

            if resolution.rolloff_rounds:
                await post_rolloff_embed(
                    interaction.channel,
                    resolution.rolloff_user_ids,
                    resolution.rolloff_rounds,
                    state.highest_user,
                    state.channel_id,
                )

            if resolution.lowest_rolloff_rounds:
                await post_rolloff_embed(
                    interaction.channel,
                    resolution.lowest_rolloff_user_ids,
                    resolution.lowest_rolloff_rounds,
                    state.lowest_user,
                    state.channel_id,
                    title="Lowest Roll Tiebreaker",
                )

            app_state.active_games.pop(self.game_id, None)
            await app_state.store.delete_round(self.game_id)

            closed_view = RiskyRollView(self.game_id)
            closed_view.disable_all_items()

            try:
                await interaction.response.edit_message(embed=build_embed(state), view=closed_view)
            except discord.HTTPException:
                log.exception("Failed to close round in #%s.", getattr(interaction.channel, "name", state.channel_id))
                await interaction.response.send_message(
                    "Round closed, but the message could not be updated. Start a new round.",
                    ephemeral=True,
                )
                return

            if resolution.result_type in (RoundResult.SIXTYNINE, RoundResult.SIXTYNINE_TIE):
                prompt_state = PendingQuestionState(
                    channel_id=state.channel_id,
                    guild_id=state.guild_id,
                    winner_id=state.highest_user,
                    participant_user_ids=set(state.rolls),
                    game_id=self.game_id,
                    prompt_kind="room",
                )
                question_view = SixtyNineQuestionView(self.game_id)
                prompt_message: discord.WebhookMessage | None = None

                try:
                    prompt_message = await interaction.followup.send(
                        content=build_pending_prompt_content(prompt_state),
                        allowed_mentions=discord.AllowedMentions(users=True),
                        view=question_view,
                        wait=True,
                    )
                    prompt_state.prompt_message_id = prompt_message.id
                    app_state.pending_questions[self.game_id] = prompt_state
                    await app_state.store.save_pending_question(prompt_state)
                except Exception:
                    app_state.pending_questions.pop(self.game_id, None)
                    await app_state.store.delete_pending_question(self.game_id)
                    if prompt_message is not None:
                        await disable_pending_question_message(
                            interaction.client,
                            prompt_state,
                            "Risky Rolls could not prepare the 69 question prompt. Start a new round.",
                        )
                    raise
                return

            if state.lowest_user is None:
                log.warning(
                    "Round closed for game %s without a lowest_user. This should not happen.",
                    self.game_id,
                )
                return

            prompt_state = PendingQuestionState(
                channel_id=state.channel_id,
                guild_id=state.guild_id,
                winner_id=state.highest_user,
                participant_user_ids={state.lowest_user},
                game_id=self.game_id,
                lowest_tie_user_ids=set(state.lowest_tie_user_ids),
                prompt_kind="direct",
            )
            question_view = SixtyNineQuestionView(self.game_id)
            prompt_message = None

            try:
                prompt_message = await interaction.followup.send(
                    content=build_pending_prompt_content(prompt_state),
                    allowed_mentions=discord.AllowedMentions(users=True),
                    view=question_view,
                    wait=True,
                )
                prompt_state.prompt_message_id = prompt_message.id
                app_state.pending_questions[self.game_id] = prompt_state
                await app_state.store.save_pending_question(prompt_state)
            except Exception:
                app_state.pending_questions.pop(self.game_id, None)
                await app_state.store.delete_pending_question(self.game_id)
                if prompt_message is not None:
                    await disable_pending_question_message(
                        interaction.client,
                        prompt_state,
                        "Risky Rolls could not prepare the winner question prompt. Start a new round.",
                    )
                raise


class SixtyNineQuestionModal(discord.ui.Modal, title="Ask A Question"):
    question = discord.ui.TextInput(
        label="Your question",
        placeholder="Type the question you want to send.",
        style=discord.TextStyle.paragraph,
        max_length=300,
    )

    def __init__(self, game_id: str):
        super().__init__()
        self.game_id = game_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        async with app_state.get_game_lock(self.game_id):
            state = app_state.pending_questions.get(self.game_id)
            if state is None:
                await interaction.response.send_message(
                    "There is no pending winner question for this round.",
                    ephemeral=True,
                )
                return

            if interaction.user.id != state.winner_id:
                await interaction.response.send_message(
                    "Only the round winner can send that question.",
                    ephemeral=True,
                )
                return

            question_text = self.question.value.strip()
            if not question_text:
                await interaction.response.send_message(
                    "Enter a question before sending it.",
                    ephemeral=True,
                )
                return

            if state.prompt_kind == "direct":
                recipient_mentions = format_user_mentions(state.participant_user_ids)
                prefix = f"{recipient_mentions}\n" if recipient_mentions else ""
            else:
                recipient_mentions = format_user_mentions(state.participant_user_ids - {state.winner_id})
                prefix = f"{recipient_mentions}\n" if recipient_mentions else ""

            await interaction.response.defer(ephemeral=True)

            try:
                await interaction.followup.send(
                    content=f"{prefix}<@{state.winner_id}> asks:\n{question_text}",
                    allowed_mentions=discord.AllowedMentions(users=True),
                    ephemeral=False,
                )
            except discord.HTTPException:
                log.exception("Failed to deliver winner question for game %s.", self.game_id)
                await interaction.followup.send(
                    "I could not send the question. Please try again.",
                    ephemeral=True,
                )
                return

            app_state.pending_questions.pop(self.game_id, None)
            await app_state.store.delete_pending_question(self.game_id)
            await disable_pending_question_message(
                interaction.client,
                state,
                build_pending_question_summary(state, question_text),
            )
            confirmation = (
                "Question sent to the selected player."
                if state.prompt_kind == "direct"
                else "Question sent to everyone who rolled."
            )
            await interaction.followup.send(confirmation, ephemeral=True)


class SixtyNineQuestionView(discord.ui.View):
    def __init__(self, game_id: str):
        super().__init__(timeout=None)
        self.game_id = game_id

    def disable_all_items(self) -> None:
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        log.exception("Unhandled error in SixtyNineQuestionView (game %s)", self.game_id, exc_info=error)
        msg = "Something went wrong. Please try again."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(
        label="Ask Question",
        style=discord.ButtonStyle.success,
        custom_id="riskyroller:ask_question",
    )
    async def ask_question_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        async with app_state.get_game_lock(self.game_id):
            state = app_state.pending_questions.get(self.game_id)
            if state is None:
                await interaction.response.send_message(
                    "There is no pending winner question for this round.",
                    ephemeral=True,
                )
                return

            if interaction.user.id != state.winner_id:
                await interaction.response.send_message(
                    "Only the round winner can send that question.",
                    ephemeral=True,
                )
                return

        await interaction.response.send_modal(SixtyNineQuestionModal(self.game_id))


async def disable_round_message(
    state: RiskyRollState,
    channel: discord.abc.GuildChannel | discord.Thread,
) -> None:
    if state.message_id is None or not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return

    try:
        message = await channel.fetch_message(state.message_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return

    view = RiskyRollView(state.game_id)
    view.disable_all_items()

    try:
        await message.edit(embed=build_embed(state), view=view)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return


async def disable_pending_question_message(
    client: discord.Client,
    state: PendingQuestionState,
    content: str,
) -> None:
    if state.prompt_message_id is None:
        return

    channel = await get_text_channel(client, state.channel_id)
    if channel is None:
        return

    try:
        message = await channel.fetch_message(state.prompt_message_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return

    view = SixtyNineQuestionView(state.game_id)
    view.disable_all_items()

    try:
        await message.edit(content=content, view=view, allowed_mentions=discord.AllowedMentions.none())
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return
