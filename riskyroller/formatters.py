import logging

import discord

from .models import PendingQuestionState, PostedQuestionState, PromptKind, RiskyRollState

log = logging.getLogger(__name__)


def format_user_mentions(user_ids: set[int]) -> str:
    return " ".join(f"<@{user_id}>" for user_id in sorted(user_ids))


def format_lowest_rolloff_note(tied_user_ids: set[int], selected_user_id: int | None) -> str:
    if selected_user_id is None or len(tied_user_ids) < 2:
        return ""
    tied_mentions = ", ".join(f"<@{user_id}>" for user_id in sorted(tied_user_ids))
    return f"{tied_mentions} → <@{selected_user_id}>"


def _roll_prefix(user_id: int, roll: int, state: RiskyRollState) -> str:
    if roll == 69:
        return "🔥"
    if not state.is_open:
        if user_id == state.highest_user:
            return "⭐" if roll == 100 else "🥇"
        if user_id == state.lowest_user:
            return "☠️" if roll == 1 else "💀"
    return "🎲"


def _questioner_mentions(state: PendingQuestionState, *, asked: bool) -> str:
    return " and ".join(
        f"<@{uid}>"
        for uid in [state.winner_id, state.extra_questioner_id]
        if uid is not None and (uid in state.questioners_asked) == asked
    )


def build_pending_prompt_content(state: PendingQuestionState) -> str:
    if state.prompt_kind == PromptKind.TWO_QUESTIONERS:
        target_mentions = format_user_mentions(state.participant_user_ids)
        lines = [
            f"☠️ Someone rolled a **1**! {_questioner_mentions(state, asked=False)} "
            f"can each fire a question at {target_mentions}."
        ]
        if state.questioners_asked:
            lines.append(f"{_questioner_mentions(state, asked=True)} already asked.")
        lines.append("Click **Ask Question** to send yours.")
        return "\n".join(lines)

    if state.prompt_kind == PromptKind.DIRECT:
        selected_user_id = next(iter(sorted(state.participant_user_ids)), None)
        lowest_rolloff_note = format_lowest_rolloff_note(state.lowest_tie_user_ids, selected_user_id)
        target_mentions = format_user_mentions(state.participant_user_ids)
        lines = [f"🥇 <@{state.winner_id}> wins the round."]
        if lowest_rolloff_note:
            lines.append(lowest_rolloff_note)
        if len(state.participant_user_ids) > 1:
            lines.append(f"They rolled **100** — click **Ask Question** to send your question to {target_mentions}.")
        else:
            lines.append(f"Click **Ask Question** to send your question to {target_mentions}.")
        return "\n".join(lines)

    return (
        f"🔥 <@{state.winner_id}> rolled **69** — they ask the room.\n"
        "Click **Ask Question** to post your question in a thread."
    )


def build_pending_question_summary(state: PendingQuestionState, question_text: str, asker_id: int | None = None) -> str:
    if state.prompt_kind == PromptKind.TWO_QUESTIONERS:
        uid = asker_id if asker_id is not None else state.winner_id
        target_mentions = format_user_mentions(state.participant_user_ids)
        return f"<@{uid}> asked {target_mentions}:\n> {question_text}"

    if state.prompt_kind == PromptKind.DIRECT:
        target_mentions = format_user_mentions(state.participant_user_ids)
        return f"<@{state.winner_id}> asked {target_mentions}:\n> {question_text}"

    return f"<@{state.winner_id}> rolled 69 and asked:\n> {question_text}"


def build_embed(state: RiskyRollState) -> discord.Embed:
    if state.is_open:
        color = discord.Color(0xDC3545)
    elif state.highest_user is not None and state.lowest_user is None:
        color = discord.Color(0xFFD700)
    else:
        color = discord.Color(0x546E7A)

    embed = discord.Embed(title="🎲 Risky Rolls", color=color)

    if state.is_open:
        embed.description = "Highest roll wins, lowest answers. Press **Roll** to join."
    else:
        embed.description = "Round over."

    if state.is_open and (state.auto_close_players or state.auto_close_minutes):
        parts = []
        if state.auto_close_players:
            parts.append(f"at {state.auto_close_players} players")
        if state.auto_close_minutes:
            parts.append(f"after {state.auto_close_minutes} minute{'s' if state.auto_close_minutes != 1 else ''}")
        embed.set_footer(text=f"Auto-closes {' or '.join(parts)}")

    if not state.rolls:
        embed.add_field(name="Rolls (0)", value="No rolls yet.", inline=False)
        return embed

    sorted_rolls = sorted(state.rolls.items(), key=lambda item: item[1], reverse=True)
    lines = [
        f"{_roll_prefix(user_id, roll, state)} **{roll}** — <@{user_id}>"
        for user_id, roll in sorted_rolls
    ]
    embed.add_field(name=f"Rolls ({len(state.rolls)})", value="\n".join(lines), inline=False)

    if not state.is_open and state.highest_user:
        high_mention = f"<@{state.highest_user}>"
        highest_rolloff_note = format_lowest_rolloff_note(state.highest_tie_user_ids, state.highest_user)
        if state.lowest_user is None:
            result = f"**Asks:** {high_mention}\n**Answers:** the room"
            if highest_rolloff_note:
                result += f"\n{highest_rolloff_note}"
        else:
            low_mention = f"<@{state.lowest_user}>"
            winner_rolled_100 = state.rolls.get(state.highest_user) == 100
            loser_rolled_1 = state.rolls.get(state.lowest_user) == 1

            if winner_rolled_100 and state.second_lowest_user is not None:
                result = f"**Asks:** {high_mention} ⭐\n**Answers:** {low_mention} and <@{state.second_lowest_user}>"
            elif loser_rolled_1 and state.second_highest_user is not None:
                result = f"**Asks:** {high_mention} and <@{state.second_highest_user}>\n**Answers:** {low_mention} ☠️"
            else:
                result = f"**Asks:** {high_mention}\n**Answers:** {low_mention}"

            # One "tied → selected" line per rolloff the round ran, so the
            # roster shows who was in each draw and not just who came out.
            for note in (
                highest_rolloff_note,
                format_lowest_rolloff_note(state.lowest_tie_user_ids, state.lowest_user),
                format_lowest_rolloff_note(state.second_lowest_tie_user_ids, state.second_lowest_user),
                format_lowest_rolloff_note(state.second_highest_tie_user_ids, state.second_highest_user),
            ):
                if note:
                    result += f"\n{note}"

            if winner_rolled_100 and loser_rolled_1:
                result += "\n*Both the 100 and 1 rules apply.*"

        embed.add_field(name="Result", value=result, inline=False)

    return embed


QUESTION_EMBED_COLOR = discord.Color(0x546E7A)
PROMPT_EMBED_COLOR = discord.Color(0x546E7A)
NOTICE_EMBED_COLOR = discord.Color(0x546E7A)


def build_question_post_embed(state: PostedQuestionState) -> discord.Embed:
    embed = discord.Embed(title="🎲 Question", color=QUESTION_EMBED_COLOR)

    asker_label = f"<@{state.asker_id}>"
    if state.asker_rolled_100:
        asker_label += " ⭐"

    target_ids = sorted(state.allowed_replier_ids)
    if state.target_rolled_1:
        target_parts = [f"<@{tid}> ☠️" for tid in target_ids]
    else:
        target_parts = [f"<@{tid}>" for tid in target_ids]
    answers_label = " and ".join(target_parts)

    embed.add_field(name="Asks", value=asker_label, inline=True)
    embed.add_field(name="Answers", value=answers_label, inline=True)
    embed.add_field(name="Question", value=f"> {state.question_text}", inline=False)
    return embed


def build_question_reply_embed(
    state: PostedQuestionState,
    replier_id: int,
    reply_text: str,
) -> discord.Embed:
    embed = build_question_post_embed(state)

    if len(state.allowed_replier_ids) > 1:
        reply_value = f"<@{replier_id}>\n> {reply_text}"
    else:
        reply_value = f"> {reply_text}"
    embed.add_field(name="Reply", value=reply_value, inline=False)

    return embed


def build_question_reply_content(
    state: PostedQuestionState,
    replier_id: int,
    reply_text: str,
) -> str:
    target_mentions = format_user_mentions(state.allowed_replier_ids)
    return f"{target_mentions}\n<@{state.asker_id}> asks:\n{state.question_text}\n\n<@{replier_id}>: {reply_text}"


def build_pending_prompt_embed(state: PendingQuestionState) -> discord.Embed:
    return discord.Embed(
        title="🎲 Risky Rolls",
        description=build_pending_prompt_content(state),
        color=PROMPT_EMBED_COLOR,
    )


def build_pending_question_summary_embed(
    state: PendingQuestionState,
    question_text: str,
    asker_id: int | None = None,
) -> discord.Embed:
    return discord.Embed(
        title="🎲 Question",
        description=build_pending_question_summary(state, question_text, asker_id),
        color=QUESTION_EMBED_COLOR,
    )


def build_notice_embed(description: str, *, title: str = "🎲 Risky Rolls") -> discord.Embed:
    return discord.Embed(title=title, description=description, color=NOTICE_EMBED_COLOR)


def build_how_to_play_content() -> str:
    return (
        "**🎲 How to Play**\n"
        "**Roll** — Each player presses **Roll** once. You roll a number from **1** to **100**.\n"
        "**Win** — Highest unique roll wins the round; lowest roll is the loser.\n"
        "**Ties for highest** — Tied players auto-reroll until one wins.\n"
        "**Question** — The winner asks the loser a question; the loser must reply.\n"
        "🔥 **Rolled 69** — The winner asks the whole room (in a thread).\n"
        "⭐ **Rolled 100** — The winner asks the bottom 2 players.\n"
        "☠️ **Rolled 1** — The top 2 players each ask the loser.\n"
        "**Close** — Only the round opener (or an admin) can close early."
    )


def build_how_to_play_embed() -> discord.Embed:
    description = (
        "**Roll** — Each player presses **Roll** once. You roll a number from **1** to **100**.\n"
        "**Win** — Highest unique roll wins the round; lowest roll is the loser.\n"
        "**Ties for highest** — Tied players auto-reroll until one wins.\n"
        "**Question** — The winner asks the loser a question; the loser must reply.\n"
        "🔥 **Rolled 69** — The winner asks the whole room (in a thread).\n"
        "⭐ **Rolled 100** — The winner asks the bottom 2 players.\n"
        "☠️ **Rolled 1** — The top 2 players each ask the loser.\n"
        "**Close** — Only the round opener (or an admin) can close early."
    )
    return discord.Embed(title="🎲 How to Play", description=description, color=NOTICE_EMBED_COLOR)


def build_reply_notification_embed(asker_id: int, jump_url: str) -> discord.Embed:
    return discord.Embed(
        title="🎲 Question",
        description=f"<@{asker_id}> — your question got a [reply]({jump_url}).",
        color=QUESTION_EMBED_COLOR,
    )


def collect_prompt_mention_ids(state: PendingQuestionState) -> list[int]:
    """User IDs that should be pinged in `content` when posting the prompt embed.

    Mirrors the mentions that appear in build_pending_prompt_content so notification
    behavior is preserved when the descriptive text moves into the embed body.
    """
    ids: list[int] = []
    if state.prompt_kind == PromptKind.TWO_QUESTIONERS:
        if state.winner_id is not None:
            ids.append(state.winner_id)
        if state.extra_questioner_id is not None:
            ids.append(state.extra_questioner_id)
        ids.extend(sorted(state.participant_user_ids))
    elif state.prompt_kind == PromptKind.DIRECT:
        if state.winner_id is not None:
            ids.append(state.winner_id)
        ids.extend(sorted(state.lowest_tie_user_ids))
        ids.extend(sorted(state.participant_user_ids))
    else:
        if state.winner_id is not None:
            ids.append(state.winner_id)
    return list(dict.fromkeys(ids))


def format_mention_list(user_ids: list[int]) -> str:
    return " ".join(f"<@{uid}>" for uid in user_ids)


def build_rolloff_embed(
    tied_user_ids: list[int],
    rounds: list[dict[int, int]],
    winner_id: int,
    title: str = "Tie Rolloff",
) -> discord.Embed:
    pick_lowest = "lowest" in title.lower()
    embed = discord.Embed(title=f"⚔️ {title}", color=discord.Color(0xFF9800))
    roll_label = "Lowest roll tied" if pick_lowest else "Highest roll tied"
    embed.description = (
        f"{roll_label} — automatic rolloff.\n"
        f"Tied: {', '.join(f'<@{user_id}>' for user_id in sorted(set(tied_user_ids)))}"
    )

    for index, round_rolls in enumerate(rounds, start=1):
        sorted_rolls = sorted(round_rolls.items(), key=lambda item: item[1], reverse=not pick_lowest)
        lines = [f"🎲 **{roll}** — <@{user_id}>" for user_id, roll in sorted_rolls]
        embed.add_field(name=f"Round {index}", value="\n".join(lines), inline=False)

    winner_label = "☠️ Selected Lowest" if pick_lowest else "🏆 Rolloff Winner"
    embed.add_field(name=winner_label, value=f"<@{winner_id}>", inline=False)
    return embed


async def get_text_channel(
    client: discord.Client,
    channel_id: int,
) -> discord.TextChannel | discord.Thread | None:
    channel = client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except discord.NotFound:
            log.warning("get_text_channel: channel %s not found.", channel_id)
            return None
        except discord.Forbidden:
            log.warning("get_text_channel: forbidden fetching channel %s.", channel_id)
            return None
        except discord.HTTPException:
            log.warning("get_text_channel: HTTP error fetching channel %s.", channel_id, exc_info=True)
            return None

    if isinstance(channel, (discord.TextChannel, discord.Thread)):
        return channel

    log.warning("get_text_channel: channel %s is type %s, not a TextChannel or Thread.", channel_id, type(channel).__name__)
    return None


async def post_rolloff_embed(
    channel: discord.abc.Messageable | discord.abc.GuildChannel | None,
    tied_user_ids: list[int],
    rolloff_rounds: list[dict[int, int]],
    winner_id: int,
    channel_id: int,
    title: str = "Tie Rolloff",
) -> None:
    try:
        if channel is not None and isinstance(channel, (discord.TextChannel, discord.Thread)):
            await channel.send(
                embed=build_rolloff_embed(tied_user_ids, rolloff_rounds, winner_id, title)
            )
    except discord.Forbidden:
        log.exception("Missing access posting rolloff embed in #%s.", getattr(channel, "name", channel_id))
    except (AttributeError, discord.HTTPException):
        log.exception("Failed to post rolloff embed in #%s.", getattr(channel, "name", channel_id))
