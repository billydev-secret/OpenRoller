import logging

import discord

from .models import PendingQuestionState, PromptKind, RiskyRollState

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


def _add_reroll_field(embed: discord.Embed, state: RiskyRollState, *, show_all_in_message: bool) -> None:
    reroll_text = f"Tied: {state.reroll_mentions()}"
    pending_mentions = state.pending_reroll_mentions()
    if pending_mentions:
        reroll_text += f"\nWaiting on: {pending_mentions}"
    elif show_all_in_message:
        reroll_text += "\nAll rerolls in — close the round."
    embed.add_field(name="⚔️ Reroll", value=reroll_text, inline=False)


def build_embed(state: RiskyRollState) -> discord.Embed:
    if state.is_open:
        color = discord.Color(0xFF9800) if state.reroll_user_ids else discord.Color(0xDC3545)
    elif state.highest_user is not None and state.lowest_user is None:
        color = discord.Color(0xFFD700)
    else:
        color = discord.Color(0x546E7A)

    embed = discord.Embed(title="🎲 Risky Rolls", color=color)

    if state.is_open:
        if state.reroll_user_ids:
            embed.description = "Tie for highest — the tied players must reroll."
        else:
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
        if state.reroll_user_ids:
            _add_reroll_field(embed, state, show_all_in_message=False)
        return embed

    sorted_rolls = sorted(state.rolls.items(), key=lambda item: item[1], reverse=True)
    lines = [
        f"{_roll_prefix(user_id, roll, state)} **{roll}** — <@{user_id}>"
        for user_id, roll in sorted_rolls
    ]
    embed.add_field(name=f"Rolls ({len(state.rolls)})", value="\n".join(lines), inline=False)

    if state.reroll_user_ids:
        _add_reroll_field(embed, state, show_all_in_message=True)

    if not state.is_open and state.highest_user:
        high_mention = f"<@{state.highest_user}>"
        if state.lowest_user is None:
            result = f"**Asks:** {high_mention}\n**Answers:** the room"
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

            lowest_rolloff_note = format_lowest_rolloff_note(
                state.lowest_tie_user_ids,
                state.lowest_user,
            )
            if lowest_rolloff_note:
                result += f"\n{lowest_rolloff_note}"

            if winner_rolled_100 and loser_rolled_1:
                result += "\n*Both the 100 and 1 rules apply.*"

        embed.add_field(name="Result", value=result, inline=False)

    return embed


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
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    if isinstance(channel, (discord.TextChannel, discord.Thread)):
        return channel

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
