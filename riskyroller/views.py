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
from .models import PendingQuestionState, PromptKind, RiskyRollState, RoundResult

log = logging.getLogger(__name__)


async def schedule_auto_close(client: discord.Client, game_id: str, delay: float) -> None:
    if delay > 0:
        await asyncio.sleep(delay)
    await auto_close_round(client, game_id)


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

        if resolution.rolloff_rounds and state.highest_user is not None:
            await post_rolloff_embed(
                channel,
                resolution.rolloff_user_ids,
                resolution.rolloff_rounds,
                state.highest_user,
                channel_id,
            )

        if resolution.lowest_rolloff_rounds and state.lowest_user is not None:
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
                await channel.get_partial_message(state.message_id).edit(
                    embed=build_embed(state), view=closed_view
                )
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

        await _send_question_prompts_channel(client, channel, game_id, state, resolution)


async def _register_prompt(
    game_id: str,
    prompt_state: PendingQuestionState,
    message: discord.Message | discord.WebhookMessage,
) -> None:
    prompt_state.prompt_message_id = message.id
    app_state.pending_questions[game_id] = prompt_state
    await app_state.store.save_pending_question(prompt_state)


def _build_main_prompt_state(game_id: str, state: RiskyRollState, resolution) -> PendingQuestionState | None:
    if state.highest_user is None:
        return None
    if resolution.result_type in (RoundResult.SIXTYNINE, RoundResult.SIXTYNINE_TIE):
        return PendingQuestionState(
            channel_id=state.channel_id,
            guild_id=state.guild_id,
            winner_id=state.highest_user,
            participant_user_ids=set(state.rolls),
            game_id=game_id,
            prompt_kind=PromptKind.ROOM,
        )
    if state.lowest_user is None:
        return None
    targets = {state.lowest_user}
    if state.second_lowest_user is not None:
        targets.add(state.second_lowest_user)
    return PendingQuestionState(
        channel_id=state.channel_id,
        guild_id=state.guild_id,
        winner_id=state.highest_user,
        participant_user_ids=targets,
        game_id=game_id,
        lowest_tie_user_ids=set(state.lowest_tie_user_ids),
        prompt_kind=PromptKind.DIRECT,
    )


def _build_one_rule_prompt_state(game_id: str, state: RiskyRollState) -> PendingQuestionState | None:
    if state.lowest_user is None or state.rolls.get(state.lowest_user) != 1 or state.highest_user is None:
        return None
    return PendingQuestionState(
        channel_id=state.channel_id,
        guild_id=state.guild_id,
        winner_id=state.highest_user,
        participant_user_ids={state.lowest_user},
        game_id=f"{game_id}:1",
        extra_questioner_id=state.second_highest_user,
        prompt_kind=PromptKind.TWO_QUESTIONERS,
    )


async def _send_and_register_prompt(send_fn, game_id: str, prompt_state: PendingQuestionState):
    message = await send_fn(
        content=build_pending_prompt_content(prompt_state),
        allowed_mentions=discord.AllowedMentions(users=True),
        view=SixtyNineQuestionView(game_id),
    )
    try:
        await _register_prompt(game_id, prompt_state, message)
    except Exception:
        app_state.pending_questions.pop(game_id, None)
        await app_state.store.delete_pending_question(game_id)
        raise
    return message


async def _try_send_one_rule_prompt(send_fn, game_id: str, state: RiskyRollState) -> None:
    one_rule_prompt = _build_one_rule_prompt_state(game_id, state)
    if one_rule_prompt is None:
        return
    one_game_id = f"{game_id}:1"
    try:
        await _send_and_register_prompt(send_fn, one_game_id, one_rule_prompt)
    except Exception:
        log.exception("Failed to send 1-rule prompt for game %s.", game_id)
        app_state.pending_questions.pop(one_game_id, None)
        await app_state.store.delete_pending_question(one_game_id)


async def _send_question_prompts_channel(
    client: discord.Client,
    channel: discord.TextChannel | discord.Thread,
    game_id: str,
    state: RiskyRollState,
    resolution,
) -> None:
    main_prompt = _build_main_prompt_state(game_id, state, resolution)
    if main_prompt is None:
        log.warning("Auto-close: no prompt state built for game %s.", game_id)
        return

    try:
        await _send_and_register_prompt(channel.send, game_id, main_prompt)
    except discord.Forbidden:
        log.error("Auto-close: missing access to #%s (game %s).", getattr(channel, "name", state.channel_id), game_id)
        return
    except Exception:
        log.exception("Auto-close: failed to send winner prompt for game %s.", game_id)
        await disable_pending_question_message(client, main_prompt, "Risky Rolls could not prepare the question prompt.")
        try:
            await channel.send("The round ended but the winner prompt could not be sent. Please start a new round.")
        except Exception:
            log.exception("Auto-close: also failed to send fallback message for game %s.", game_id)
        return

    if resolution.result_type in (RoundResult.SIXTYNINE, RoundResult.SIXTYNINE_TIE):
        return

    await _try_send_one_rule_prompt(channel.send, game_id, state)


async def _send_question_prompts_followup(
    interaction: discord.Interaction,
    game_id: str,
    state: RiskyRollState,
    resolution,
) -> None:
    main_prompt = _build_main_prompt_state(game_id, state, resolution)
    if main_prompt is None:
        log.warning("Close: no prompt state built for game %s.", game_id)
        return

    async def send_via_followup(**kwargs):
        return await interaction.followup.send(wait=True, **kwargs)

    try:
        await _send_and_register_prompt(send_via_followup, game_id, main_prompt)
    except Exception:
        await disable_pending_question_message(interaction.client, main_prompt, "Risky Rolls could not prepare the question prompt.")
        raise

    if resolution.result_type in (RoundResult.SIXTYNINE, RoundResult.SIXTYNINE_TIE):
        return

    await _try_send_one_rule_prompt(send_via_followup, game_id, state)


class BaseRiskyRollView(discord.ui.View):
    def __init__(self, game_id: str):
        super().__init__(timeout=None)
        self.game_id = game_id

    def disable_all_items(self) -> None:
        for item in self.children:
            if isinstance(item, (discord.ui.Button, discord.ui.Select)):
                item.disabled = True

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        log.exception("Unhandled error in %s (game %s)", type(self).__name__, self.game_id, exc_info=error)
        msg = "Something went wrong. Please try again."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


class RiskyRollView(BaseRiskyRollView):
    @discord.ui.button(
        label="Roll",
        style=discord.ButtonStyle.primary,
        custom_id="riskyroller:roll",
        emoji="🎲",
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
                app_state.auto_close_tasks[self.game_id] = asyncio.create_task(
                    schedule_auto_close(interaction.client, self.game_id, delay)
                )

    @discord.ui.button(
        label="Close Round",
        style=discord.ButtonStyle.danger,
        custom_id="riskyroller:close",
        emoji="🔒",
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

            if resolution.rolloff_rounds and state.highest_user is not None:
                await post_rolloff_embed(
                    interaction.channel,
                    resolution.rolloff_user_ids,
                    resolution.rolloff_rounds,
                    state.highest_user,
                    state.channel_id,
                )

            if resolution.lowest_rolloff_rounds and state.lowest_user is not None:
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

            await _send_question_prompts_followup(interaction, self.game_id, state, resolution)


class SixtyNineQuestionModal(discord.ui.Modal, title="Ask A Question"):
    question = discord.ui.TextInput(
        label="Your question",
        placeholder="What do you want to ask them?",
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

            asker_id = interaction.user.id

            if asker_id not in state.allowed_questioners():
                await interaction.response.send_message(
                    "Only the eligible players can send a question.",
                    ephemeral=True,
                )
                return

            if asker_id in state.questioners_asked:
                await interaction.response.send_message(
                    "You already asked your question.",
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

            await interaction.response.defer(ephemeral=True)

            if state.prompt_kind == PromptKind.ROOM:
                # Create a thread from the prompt message and ping everyone who rolled.
                channel = interaction.channel
                thread_name = question_text[:97] + "..." if len(question_text) > 97 else question_text
                try:
                    if isinstance(channel, discord.TextChannel) and state.prompt_message_id is not None:
                        partial_msg = channel.get_partial_message(state.prompt_message_id)
                        thread = await partial_msg.create_thread(
                            name=thread_name,
                            auto_archive_duration=1440,
                        )
                    elif isinstance(channel, discord.TextChannel):
                        thread = await channel.create_thread(
                            name=thread_name,
                            type=discord.ChannelType.public_thread,
                            auto_archive_duration=1440,
                        )
                    else:
                        thread = None
                except (discord.Forbidden, discord.HTTPException):
                    log.exception("Failed to create thread for 69 question in game %s.", self.game_id)
                    thread = None

                all_mentions = format_user_mentions(state.participant_user_ids)
                content = f"{all_mentions}\n<@{asker_id}> asks:\n{question_text}"

                try:
                    if thread is not None:
                        await thread.send(
                            content=content,
                            allowed_mentions=discord.AllowedMentions(users=True),
                        )
                    else:
                        await interaction.followup.send(
                            content=content,
                            allowed_mentions=discord.AllowedMentions(users=True),
                            ephemeral=False,
                        )
                except discord.HTTPException:
                    log.exception("Failed to post 69 question for game %s.", self.game_id)
                    await interaction.followup.send("I could not send the question. Please try again.", ephemeral=True)
                    return

                app_state.pending_questions.pop(self.game_id, None)
                await app_state.store.delete_pending_question(self.game_id)
                await disable_pending_question_message(
                    interaction.client,
                    state,
                    build_pending_question_summary(state, question_text, asker_id),
                )
                await interaction.followup.send("Question posted in a thread.", ephemeral=True)
                return

            if state.prompt_kind == PromptKind.TWO_QUESTIONERS:
                target_mentions = format_user_mentions(state.participant_user_ids)
                try:
                    question_msg = await interaction.followup.send(
                        content=f"{target_mentions}\n<@{asker_id}> asks:\n{question_text}",
                        allowed_mentions=discord.AllowedMentions(users=True),
                        ephemeral=False,
                        wait=True,
                    )
                    app_state.remember_question_message(question_msg.id, asker_id)
                except discord.HTTPException:
                    log.exception("Failed to deliver two-questioner question for game %s.", self.game_id)
                    await interaction.followup.send("I could not send the question. Please try again.", ephemeral=True)
                    return

                state.questioners_asked.add(asker_id)

                if state.questions_remaining > 0:
                    await app_state.store.save_pending_question(state)
                    remaining_id = next(
                        uid for uid in [state.winner_id, state.extra_questioner_id]
                        if uid is not None and uid not in state.questioners_asked
                    )
                    channel = await get_text_channel(interaction.client, state.channel_id)
                    if channel is not None and state.prompt_message_id is not None:
                        try:
                            await channel.get_partial_message(state.prompt_message_id).edit(
                                content=build_pending_prompt_content(state),
                                allowed_mentions=discord.AllowedMentions(users=True),
                            )
                        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                            pass
                    await interaction.followup.send(
                        f"Question sent! Waiting for <@{remaining_id}> to ask their question.",
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions(users=False),
                    )
                    return

                app_state.pending_questions.pop(self.game_id, None)
                await app_state.store.delete_pending_question(self.game_id)
                await disable_pending_question_message(
                    interaction.client,
                    state,
                    build_pending_question_summary(state, question_text, asker_id),
                )
                await interaction.followup.send("Question sent.", ephemeral=True)
                return

            recipient_mentions = format_user_mentions(state.participant_user_ids)
            try:
                question_msg = await interaction.followup.send(
                    content=f"{recipient_mentions}\n<@{asker_id}> asks:\n{question_text}",
                    allowed_mentions=discord.AllowedMentions(users=True),
                    ephemeral=False,
                    wait=True,
                )
                app_state.remember_question_message(question_msg.id, asker_id)
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
                build_pending_question_summary(state, question_text, asker_id),
            )
            target_count = len(state.participant_user_ids)
            await interaction.followup.send(
                "Question sent to the selected player." if target_count == 1 else "Question sent to both players.",
                ephemeral=True,
            )


class SixtyNineQuestionView(BaseRiskyRollView):
    @discord.ui.button(
        label="Ask Question",
        style=discord.ButtonStyle.success,
        custom_id="riskyroller:ask_question",
        emoji="💬",
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

            if interaction.user.id not in state.allowed_questioners():
                await interaction.response.send_message(
                    "Only the eligible players can send a question.",
                    ephemeral=True,
                )
                return

            if interaction.user.id in state.questioners_asked:
                await interaction.response.send_message(
                    "You already asked your question.",
                    ephemeral=True,
                )
                return

        await interaction.response.send_modal(SixtyNineQuestionModal(self.game_id))


async def disable_round_message(
    state: RiskyRollState,
    channel: discord.abc.Messageable | discord.abc.GuildChannel | None,
) -> None:
    if state.message_id is None or not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return

    view = RiskyRollView(state.game_id)
    view.disable_all_items()

    try:
        await channel.get_partial_message(state.message_id).edit(embed=build_embed(state), view=view)
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

    view = SixtyNineQuestionView(state.game_id)
    view.disable_all_items()

    try:
        await channel.get_partial_message(state.prompt_message_id).edit(
            content=content, view=view, allowed_mentions=discord.AllowedMentions.none()
        )
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return
