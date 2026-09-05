import asyncio
import logging
import math
import random
import time

import discord

from . import state as app_state
from .config import DEFAULT_MIN_GAME_SECONDS
from .filters import contains_disallowed_content
from .logic import REQUIRED_THREAD_PERMISSIONS, effective_min_game_seconds, missing_permissions
from .formatters import (
    PERMISSION_CHECK_HINT,
    auto_close_hint,
    build_embed,
    build_how_to_play_content,
    build_pending_prompt_content,
    build_pending_question_summary,
    build_question_reply_content,
    format_duration,
    format_user_mentions,
    get_text_channel,
    join_names,
    permission_help,
)
from .models import (
    PendingQuestionState,
    PostedQuestionState,
    PromptKind,
    RiskyRollState,
    RoundResult,
)

log = logging.getLogger(__name__)

# Every refusal below says what happened and what the reader can do next. A
# message that only names the outcome ("No active game.") leaves the player
# guessing whether they did something wrong, whether the bot is broken, and
# whether to try again — so each one carries its reason and a next step.

PROMPT_GONE_TEXT = (
    "This question prompt is no longer active: the question was already asked, an admin reset "
    "the channel, or it expired (prompts stay open for 7 days). Start a new round to play again."
)
REPLY_CLOSED_TEXT = (
    "This question is no longer open for a reply: it was already answered, cleared by an admin's "
    "reset, or expired (questions stay open for 7 days)."
)
PROMPT_SETUP_FAILED_TEXT = (
    "This question prompt couldn't be set up, so it was cancelled. Start a new round with /risky_start."
)
ROUND_NO_PROMPT_TEXT = (
    "The round ended, but I couldn't post the question prompt, so it ends without a question. "
    f"Start a new round with /risky_start. {PERMISSION_CHECK_HINT}"
)


async def _ephemeral(interaction: discord.Interaction, text: str) -> None:
    # Refusals never need to ping anyone they name.
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    else:
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


def _round_over_text(game_id: str, consequence: str) -> str:
    text = f"This round has already ended, {consequence}. Start a new one with /risky_start."
    if game_id in app_state.pending_questions or f"{game_id}:1" in app_state.pending_questions:
        text += " Its question prompt is still open, waiting on the winner."
    return text


def _not_questioner_text(state: PendingQuestionState) -> str:
    who = join_names([f"<@{uid}>" for uid in sorted(state.allowed_questioners())])
    return f"Only {who} can ask the question for this round — the roll decided who asks. Roll in the next one!"


def _already_asked_text(state: PendingQuestionState) -> str:
    waiting = sorted(uid for uid in state.allowed_questioners() if uid not in state.questioners_asked)
    if waiting:
        who = join_names([f"<@{uid}>" for uid in waiting])
        return f"You've already sent your question — this prompt is now waiting on {who} to send theirs."
    return "You've already sent your question for this round."


def _not_recipient_text(state: PostedQuestionState) -> str:
    who = join_names([f"<@{uid}>" for uid in sorted(state.allowed_replier_ids)])
    return f"Only {who} can reply to this question — it was asked of them."


def _post_failure_text(what: str, text: str, button: str, interaction: discord.Interaction) -> str:
    """Why a post failed and what to do next, handing the typed text back."""
    reason = permission_help(missing_permissions(interaction.app_permissions)) or PERMISSION_CHECK_HINT
    return (
        f"I couldn't post your {what} in this channel, so it was not sent — press **{button}** to try again. "
        f"{reason}\nYour {what} was:\n> {text}"
    )


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

        if resolution.result_type == RoundResult.NOT_ENOUGH:
            state.is_open = False
            app_state.active_games.pop(game_id, None)
            await app_state.store.delete_round(game_id)
            if channel is not None:
                await disable_round_message(state, channel)
                rolled = len(state.rolls)
                await channel.send(
                    "Round auto-closed with no result: at least 2 players must roll before a round can "
                    f"resolve, and {rolled} {'has' if rolled == 1 else 'have'}. Start another with /risky_start."
                )
            return

        closed_view = RiskyRollView(game_id)
        closed_view.disable_all_items()

        channel_forbidden = False
        if state.message_id is not None and channel is not None:
            try:
                await channel.get_partial_message(state.message_id).edit(
                    embed=build_embed(state, getattr(channel, "guild", None)), view=closed_view
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


async def _register_posted_question(posted: PostedQuestionState) -> None:
    app_state.posted_questions[posted.message_id] = posted
    try:
        await app_state.store.save_posted_question(posted)
    except Exception:
        app_state.posted_questions.pop(posted.message_id, None)
        log.exception("Failed to persist posted question state for message %s.", posted.message_id)


async def _clear_posted_question(message_id: int) -> None:
    app_state.posted_questions.pop(message_id, None)
    await app_state.store.delete_posted_question(message_id)


async def _send_question_message(
    *,
    interaction: discord.Interaction,
    pending: PendingQuestionState,
    asker_id: int,
    question_text: str,
    asker_rolled_100: bool,
    target_rolled_1: bool,
) -> bool:
    target_mentions = format_user_mentions(pending.participant_user_ids)
    try:
        question_msg = await interaction.followup.send(
            content=f"{target_mentions}\n<@{asker_id}> asks:\n{question_text}",
            allowed_mentions=discord.AllowedMentions(users=True),
            ephemeral=False,
            wait=True,
            view=QuestionReplyView(),
        )
    except discord.HTTPException:
        log.exception("Failed to deliver question for game %s.", pending.game_id)
        await _ephemeral(interaction, _post_failure_text("question", question_text, "Ask Question", interaction))
        return False

    posted = PostedQuestionState(
        message_id=question_msg.id,
        channel_id=pending.channel_id,
        guild_id=pending.guild_id,
        asker_id=asker_id,
        allowed_replier_ids=set(pending.participant_user_ids),
        question_text=question_text,
        asker_rolled_100=asker_rolled_100,
        target_rolled_1=target_rolled_1,
    )
    await _register_posted_question(posted)
    return True


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
        await disable_pending_question_message(client, main_prompt, PROMPT_SETUP_FAILED_TEXT)
        try:
            await channel.send(ROUND_NO_PROMPT_TEXT)
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
        await disable_pending_question_message(interaction.client, main_prompt, PROMPT_SETUP_FAILED_TEXT)
        raise

    if resolution.result_type in (RoundResult.SIXTYNINE, RoundResult.SIXTYNINE_TIE):
        return

    await _try_send_one_rule_prompt(send_via_followup, game_id, state)


class BaseRiskyRollView(discord.ui.View):
    def __init__(self, game_id: str = ""):
        super().__init__(timeout=None)
        self.game_id = game_id

    def disable_all_items(self) -> None:
        for item in self.children:
            if isinstance(item, (discord.ui.Button, discord.ui.Select)):
                item.disabled = True

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        # The interaction token expired before we answered (Discord error
        # 10062: Unknown interaction). Nothing can be sent on it any more, and
        # it is not a fault in the game — log quietly and move on.
        if isinstance(error, discord.NotFound) and error.code == 10062:
            log.debug(
                "Interaction expired in %s (game %s)", type(self).__name__, self.game_id or "?",
            )
            return
        if self.game_id:
            log.exception("Unhandled error in %s (game %s)", type(self).__name__, self.game_id, exc_info=error)
        else:
            log.exception("Unhandled error in %s", type(self).__name__, exc_info=error)
        msg = (
            "Something went wrong on my side and that didn't go through — please try once more. "
            f"{PERMISSION_CHECK_HINT} You can also report it via /support."
        )
        try:
            await _ephemeral(interaction, msg)
        except discord.HTTPException:
            # The apology itself failed (token gone, channel gone); the
            # original error is already logged.
            pass


class RiskyRollView(BaseRiskyRollView):
    @discord.ui.button(
        label="Roll",
        style=discord.ButtonStyle.primary,
        custom_id="riskyroller:roll",
        emoji="🎲",
    )
    async def roll_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Acknowledge at once: the interaction token lasts three seconds, and
        # a burst of rolls can hold the game lock (and the database) longer
        # than that. A deferred component response edits the round message
        # later; refusals go out as ephemeral follow-ups.
        await interaction.response.defer()
        async with app_state.get_game_lock(self.game_id):
            state = app_state.active_games.get(self.game_id)
            if not state or not state.is_open:
                await _ephemeral(interaction, _round_over_text(self.game_id, "so rolls are closed"))
                return

            if not state.can_roll(interaction.user.id):
                hint = auto_close_hint(state)
                await _ephemeral(
                    interaction,
                    f"You already rolled a **{state.rolls[interaction.user.id]}** this round — it's one roll per "
                    f"player. Wait for the close to see how it lands.{' ' + hint if hint else ''}",
                )
                return

            roll = random.randint(1, 100)
            state.add_roll(interaction.user.id, roll)
            # Cache the roller's name so the roster embed can show it as text
            # instead of a <@id> mention that some viewers can't resolve.
            app_state.display_names[interaction.user.id] = interaction.user.display_name
            await app_state.store.save_round(state)

            log.info(
                "Channel #%s: %s rolled %s",
                getattr(interaction.channel, "name", state.channel_id),
                interaction.user.display_name,
                roll,
            )

            await interaction.edit_original_response(embed=build_embed(state, interaction.guild), view=self)

            if state.auto_close_players and len(state.rolls) >= state.auto_close_players:
                task = app_state.auto_close_tasks.pop(self.game_id, None)
                if task:
                    task.cancel()
                elapsed = time.time() - state.created_at
                min_seconds = effective_min_game_seconds(
                    app_state.min_game_seconds, state.guild_id, state.skip_min_game_time, DEFAULT_MIN_GAME_SECONDS
                )
                delay = max(0.0, min_seconds - elapsed)
                app_state.auto_close_tasks[self.game_id] = asyncio.create_task(
                    schedule_auto_close(interaction.client, self.game_id, delay)
                )

    @discord.ui.button(
        label="How to Play",
        style=discord.ButtonStyle.secondary,
        custom_id="riskyroller:how_to_play",
        emoji="❓",
    )
    async def how_to_play_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            content=build_how_to_play_content(),
            ephemeral=True,
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
                await _ephemeral(interaction, _round_over_text(self.game_id, "so there's nothing to close"))
                return

            is_admin = isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator
            if interaction.user.id != state.opener_id and not is_admin:
                hint = auto_close_hint(state) or "It stays open until they close it."
                await _ephemeral(
                    interaction,
                    f"Only <@{state.opener_id}>, who opened this round, or a server admin can close it. {hint}",
                )
                return

            min_seconds = effective_min_game_seconds(
                app_state.min_game_seconds, state.guild_id, state.skip_min_game_time, DEFAULT_MIN_GAME_SECONDS
            )
            if min_seconds:
                elapsed = time.time() - state.created_at
                remaining = math.ceil(min_seconds - elapsed)
                if remaining > 0:
                    hint = auto_close_hint(state)
                    await _ephemeral(
                        interaction,
                        f"This round can't be closed for another {format_duration(remaining)}: this server keeps "
                        f"rounds open at least {format_duration(min_seconds)} so everyone gets a chance to roll."
                        f"{' ' + hint if hint else ''} Admins can change the minimum with "
                        "/risky_set_min_game_time; /risky_start_no_ping opens a round without one.",
                    )
                    return

            resolution = state.resolve()

            if resolution.result_type == RoundResult.NOT_ENOUGH:
                rolled = len(state.rolls)
                hint = auto_close_hint(state)
                leave = "Wait for another roll, or leave it open"
                leave += f" — {hint[0].lower()}{hint[1:]}" if hint else "."
                await _ephemeral(
                    interaction,
                    "Can't close yet: at least 2 players must roll before a round can resolve, and "
                    f"{rolled} {'has' if rolled == 1 else 'have'} so far. {leave}",
                )
                return

            task = app_state.auto_close_tasks.pop(self.game_id, None)
            if task:
                task.cancel()

            app_state.active_games.pop(self.game_id, None)
            await app_state.store.delete_round(self.game_id)

            closed_view = RiskyRollView(self.game_id)
            closed_view.disable_all_items()

            try:
                await interaction.response.edit_message(embed=build_embed(state, interaction.guild), view=closed_view)
            except discord.HTTPException:
                log.exception("Failed to close round in #%s.", getattr(interaction.channel, "name", state.channel_id))
                reason = permission_help(missing_permissions(interaction.app_permissions)) or PERMISSION_CHECK_HINT
                await _ephemeral(
                    interaction,
                    "The round is closed, but I couldn't update its message or post the question prompt, so it "
                    f"ends without a question. Start a new round with /risky_start. {reason}",
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
                await _ephemeral(interaction, PROMPT_GONE_TEXT)
                return

            asker_id = interaction.user.id

            if asker_id not in state.allowed_questioners():
                await _ephemeral(interaction, _not_questioner_text(state))
                return

            if asker_id in state.questioners_asked:
                await _ephemeral(interaction, _already_asked_text(state))
                return

            question_text = self.question.value.strip()
            if not question_text:
                await _ephemeral(
                    interaction,
                    "Your question was empty, so nothing was sent. Press **Ask Question** again and type it in the box.",
                )
                return

            if contains_disallowed_content(question_text):
                await _ephemeral(
                    interaction,
                    "That question tripped the word filter and wasn't sent. Reword it and press **Ask Question** again.",
                )
                return

            await interaction.response.defer(ephemeral=True)

            if state.prompt_kind == PromptKind.ROOM:
                # Create a thread from the prompt message and ping everyone who rolled.
                channel = interaction.channel
                thread_name = question_text[:97] + "..." if len(question_text) > 97 else question_text
                thread = None
                thread_failed = False
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
                except (discord.Forbidden, discord.HTTPException):
                    log.exception("Failed to create thread for 69 question in game %s.", self.game_id)
                    thread_failed = True

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
                    await _ephemeral(
                        interaction, _post_failure_text("question", question_text, "Ask Question", interaction)
                    )
                    return

                app_state.pending_questions.pop(self.game_id, None)
                await app_state.store.delete_pending_question(self.game_id)
                await disable_pending_question_message(
                    interaction.client,
                    state,
                    build_pending_question_summary(state, question_text, asker_id),
                )
                if thread is not None:
                    done = "Question posted in a thread."
                elif thread_failed:
                    # The question went out, just not where it should have; say
                    # why so the next round can go right.
                    missing = missing_permissions(interaction.app_permissions, REQUIRED_THREAD_PERMISSIONS)
                    done = "I couldn't open a thread here, so the question is posted in the channel instead. " + (
                        permission_help(missing)
                        if missing
                        else "An admin should check I can create public threads and send messages in them."
                    )
                else:
                    done = "Question posted."
                await _ephemeral(interaction, done)
                return

            if state.prompt_kind == PromptKind.TWO_QUESTIONERS:
                if not await _send_question_message(
                    interaction=interaction,
                    pending=state,
                    asker_id=asker_id,
                    question_text=question_text,
                    asker_rolled_100=False,
                    target_rolled_1=True,
                ):
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

            if not await _send_question_message(
                interaction=interaction,
                pending=state,
                asker_id=asker_id,
                question_text=question_text,
                asker_rolled_100=len(state.participant_user_ids) > 1,
                target_rolled_1=False,
            ):
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
                await _ephemeral(interaction, PROMPT_GONE_TEXT)
                return

            if interaction.user.id not in state.allowed_questioners():
                await _ephemeral(interaction, _not_questioner_text(state))
                return

            if interaction.user.id in state.questioners_asked:
                await _ephemeral(interaction, _already_asked_text(state))
                return

        await interaction.response.send_modal(SixtyNineQuestionModal(self.game_id))


class QuestionReplyModal(discord.ui.Modal, title="Reply"):
    reply = discord.ui.TextInput(
        label="Your reply",
        style=discord.TextStyle.paragraph,
        max_length=300,
    )

    def __init__(self, message_id: int):
        super().__init__()
        self.message_id = message_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        async with app_state.get_message_lock(self.message_id):
            state = app_state.posted_questions.get(self.message_id)
            if state is None:
                await _ephemeral(interaction, REPLY_CLOSED_TEXT)
                return
            if interaction.user.id not in state.allowed_replier_ids:
                await _ephemeral(interaction, _not_recipient_text(state))
                return

            reply_text = self.reply.value.strip()
            if not reply_text:
                await _ephemeral(
                    interaction, "Your reply was empty, so nothing was posted. Press **Reply** again and type it in the box."
                )
                return

            # The reply is posted publicly too, so it gets the same guard as
            # the question.
            if contains_disallowed_content(reply_text):
                await _ephemeral(
                    interaction, "That reply tripped the word filter and wasn't posted. Reword it and press **Reply** again."
                )
                return

            reply_content = build_question_reply_content(state, interaction.user.id, reply_text)

            # Answer the modal by updating the message it was opened from. That
            # goes through the interaction callback, which needs no channel
            # permission at all — unlike resolving the channel and editing the
            # message by id, which fails wherever the bot lacks View Channel
            # (members-only channels are the common case) even though every
            # button press there still works.
            try:
                await interaction.response.edit_message(
                    content=reply_content,
                    embed=None,
                    view=None,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.NotFound:
                await _clear_posted_question(self.message_id)
                await _ephemeral(
                    interaction,
                    "The question message has been deleted, so there's nothing to reply to — this question is closed.",
                )
                return
            except discord.HTTPException:
                log.exception("Failed to edit question message %s.", self.message_id)
                # Hand the text back rather than losing it.
                await _ephemeral(
                    interaction,
                    "Discord rejected the update, so your reply was not posted — press **Reply** to try again. "
                    f"If it keeps failing, report it via /support. Your reply was:\n> {reply_text}",
                )
                return

            await _clear_posted_question(self.message_id)
            await interaction.followup.send("Reply sent.", ephemeral=True)


class QuestionReplyView(BaseRiskyRollView):
    @discord.ui.button(
        label="Reply",
        style=discord.ButtonStyle.primary,
        custom_id="riskyroller:question_reply",
        emoji="✏️",
    )
    async def reply_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.message is None:
            return
        state = app_state.posted_questions.get(interaction.message.id)
        if state is None:
            await _ephemeral(interaction, REPLY_CLOSED_TEXT)
            return
        if interaction.user.id not in state.allowed_replier_ids:
            await _ephemeral(interaction, _not_recipient_text(state))
            return
        await interaction.response.send_modal(
            QuestionReplyModal(message_id=interaction.message.id)
        )


async def disable_round_message(
    state: RiskyRollState,
    channel: discord.abc.Messageable | discord.abc.GuildChannel | None,
) -> None:
    if state.message_id is None or not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return

    view = RiskyRollView(state.game_id)
    view.disable_all_items()

    try:
        await channel.get_partial_message(state.message_id).edit(embed=build_embed(state, channel.guild), view=view)
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
