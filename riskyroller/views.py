import asyncio
import logging
import random

import discord

from . import state as app_state
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


async def auto_close_round(client: discord.Client, channel_id: int) -> None:
    async with app_state.get_channel_lock(channel_id):
        app_state.auto_close_tasks.pop(channel_id, None)

        state = app_state.active_games.get(channel_id)
        if not state or not state.is_open:
            return

        resolution = state.resolve()
        channel = await get_text_channel(client, channel_id)

        if resolution.result_type in (RoundResult.NOT_ENOUGH, RoundResult.WAITING_FOR_REROLLS):
            state.is_open = False
            app_state.active_games.pop(channel_id, None)
            await app_state.store.delete_round(channel_id)
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

        closed_view = RiskyRollView(channel_id)
        closed_view.disable_all_items()

        if state.message_id is not None and channel is not None:
            try:
                message = await channel.fetch_message(state.message_id)
                await message.edit(embed=build_embed(state), view=closed_view)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                log.exception("Auto-close: failed to edit round message in #%s.", channel.name)

        await app_state.store.save_round(state)
        app_state.active_games.pop(channel_id, None)
        await app_state.store.delete_round(channel_id)

        if channel is None:
            return

        if resolution.result_type in (RoundResult.SIXTYNINE, RoundResult.SIXTYNINE_TIE):
            prompt_state = PendingQuestionState(
                channel_id=channel_id,
                guild_id=state.guild_id,
                winner_id=state.highest_user,
                participant_user_ids=set(state.rolls),
                prompt_kind="room",
            )
        else:
            if state.lowest_user is None:
                log.warning("Auto-close: no lowest_user in #%s.", channel.name)
                return
            prompt_state = PendingQuestionState(
                channel_id=channel_id,
                guild_id=state.guild_id,
                winner_id=state.highest_user,
                participant_user_ids={state.lowest_user},
                lowest_tie_user_ids=set(state.lowest_tie_user_ids),
                prompt_kind="direct",
            )

        question_view = SixtyNineQuestionView(channel_id)
        prompt_message: discord.Message | None = None

        try:
            prompt_message = await channel.send(
                content=build_pending_prompt_content(prompt_state),
                allowed_mentions=discord.AllowedMentions(users=True),
                view=question_view,
            )
            prompt_state.prompt_message_id = prompt_message.id
            app_state.pending_questions[channel_id] = prompt_state
            await app_state.store.save_pending_question(prompt_state)
        except Exception:
            app_state.pending_questions.pop(channel_id, None)
            await app_state.store.delete_pending_question(channel_id)
            if prompt_message is not None:
                await disable_pending_question_message(
                    client,
                    prompt_state,
                    "Risky Rolls could not prepare the question prompt. Start a new round.",
                )
            raise


class RiskyRollView(discord.ui.View):
    def __init__(self, channel_id: int):
        super().__init__(timeout=None)
        self.channel_id = channel_id

    def disable_all_items(self) -> None:
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True

    @discord.ui.button(
        label="Roll",
        style=discord.ButtonStyle.primary,
        custom_id="riskyroller:roll",
    )
    async def roll_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with app_state.get_channel_lock(self.channel_id):
            state = app_state.active_games.get(self.channel_id)
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
                getattr(interaction.channel, "name", self.channel_id),
                interaction.user.display_name,
                roll,
            )

            await interaction.response.edit_message(embed=build_embed(state), view=self)

            if state.auto_close_players and len(state.rolls) >= state.auto_close_players:
                task = app_state.auto_close_tasks.pop(self.channel_id, None)
                if task:
                    task.cancel()
                asyncio.create_task(
                    auto_close_round(interaction.client, self.channel_id)
                )

    @discord.ui.button(
        label="Close Round",
        style=discord.ButtonStyle.danger,
        custom_id="riskyroller:close",
    )
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with app_state.get_channel_lock(self.channel_id):
            state = app_state.active_games.get(self.channel_id)
            if not state or not state.is_open:
                await interaction.response.send_message("No active game.", ephemeral=True)
                return

            if interaction.user.id != state.opener_id:
                await interaction.response.send_message(
                    "Only the round opener can close this round.",
                    ephemeral=True,
                )
                return

            task = app_state.auto_close_tasks.pop(self.channel_id, None)
            if task:
                task.cancel()

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

            if resolution.rolloff_rounds:
                await post_rolloff_embed(
                    interaction.channel,
                    resolution.rolloff_user_ids,
                    resolution.rolloff_rounds,
                    state.highest_user,
                    self.channel_id,
                )

            app_state.active_games.pop(self.channel_id, None)
            await app_state.store.delete_round(self.channel_id)

            closed_view = RiskyRollView(self.channel_id)
            closed_view.disable_all_items()

            try:
                await interaction.response.edit_message(embed=build_embed(state), view=closed_view)
            except discord.HTTPException:
                log.exception("Failed to close round in #%s.", getattr(interaction.channel, "name", self.channel_id))
                await interaction.response.send_message(
                    "Failed to close the round. Please try again.",
                    ephemeral=True,
                )
                return

            if resolution.result_type in (RoundResult.SIXTYNINE, RoundResult.SIXTYNINE_TIE):
                prompt_state = PendingQuestionState(
                    channel_id=self.channel_id,
                    guild_id=state.guild_id,
                    winner_id=state.highest_user,
                    participant_user_ids=set(state.rolls),
                    prompt_kind="room",
                )
                question_view = SixtyNineQuestionView(self.channel_id)
                prompt_message: discord.WebhookMessage | None = None

                try:
                    prompt_message = await interaction.followup.send(
                        content=build_pending_prompt_content(prompt_state),
                        allowed_mentions=discord.AllowedMentions(users=True),
                        view=question_view,
                        wait=True,
                    )
                    prompt_state.prompt_message_id = prompt_message.id
                    app_state.pending_questions[self.channel_id] = prompt_state
                    await app_state.store.save_pending_question(prompt_state)
                except Exception:
                    app_state.pending_questions.pop(self.channel_id, None)
                    await app_state.store.delete_pending_question(self.channel_id)
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
                    "Round closed in #%s without a lowest_user. This should not happen.",
                    getattr(interaction.channel, "name", self.channel_id),
                )
                return

            prompt_state = PendingQuestionState(
                channel_id=self.channel_id,
                guild_id=state.guild_id,
                winner_id=state.highest_user,
                participant_user_ids={state.lowest_user},
                lowest_tie_user_ids=set(state.lowest_tie_user_ids),
                prompt_kind="direct",
            )
            question_view = SixtyNineQuestionView(self.channel_id)
            prompt_message = None

            try:
                prompt_message = await interaction.followup.send(
                    content=build_pending_prompt_content(prompt_state),
                    allowed_mentions=discord.AllowedMentions(users=True),
                    view=question_view,
                    wait=True,
                )
                prompt_state.prompt_message_id = prompt_message.id
                app_state.pending_questions[self.channel_id] = prompt_state
                await app_state.store.save_pending_question(prompt_state)
            except Exception:
                app_state.pending_questions.pop(self.channel_id, None)
                await app_state.store.delete_pending_question(self.channel_id)
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

    def __init__(self, channel_id: int):
        super().__init__()
        self.channel_id = channel_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        async with app_state.get_channel_lock(self.channel_id):
            state = app_state.pending_questions.get(self.channel_id)
            if state is None:
                await interaction.response.send_message(
                    "There is no pending winner question for this channel.",
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
                log.exception("Failed to deliver winner question in #%s.", getattr(interaction.channel, "name", self.channel_id))
                await interaction.followup.send(
                    "I could not send the question. Please try again.",
                    ephemeral=True,
                )
                return

            app_state.pending_questions.pop(self.channel_id, None)
            await app_state.store.delete_pending_question(self.channel_id)
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
    def __init__(self, channel_id: int):
        super().__init__(timeout=None)
        self.channel_id = channel_id

    def disable_all_items(self) -> None:
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True

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
        async with app_state.get_channel_lock(self.channel_id):
            state = app_state.pending_questions.get(self.channel_id)
            if state is None:
                await interaction.response.send_message(
                    "There is no pending winner question for this channel.",
                    ephemeral=True,
                )
                return

            if interaction.user.id != state.winner_id:
                await interaction.response.send_message(
                    "Only the round winner can send that question.",
                    ephemeral=True,
                )
                return

        await interaction.response.send_modal(SixtyNineQuestionModal(self.channel_id))


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

    view = RiskyRollView(state.channel_id)
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

    view = SixtyNineQuestionView(state.channel_id)
    view.disable_all_items()

    try:
        await message.edit(content=content, view=view, allowed_mentions=discord.AllowedMentions.none())
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return
