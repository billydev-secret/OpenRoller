import logging

import discord

from .models import PendingQuestionState, RiskyRollState

log = logging.getLogger(__name__)


def format_user_mentions(user_ids: set[int]) -> str:
    return " ".join(f"<@{user_id}>" for user_id in sorted(user_ids))


def format_lowest_rolloff_note(tied_user_ids: set[int], selected_user_id: int | None) -> str:
    if selected_user_id is None or len(tied_user_ids) < 2:
        return ""
    tied_mentions = ", ".join(f"<@{user_id}>" for user_id in sorted(tied_user_ids))
    return f"{tied_mentions} -> <@{selected_user_id}>."


def build_pending_prompt_content(state: PendingQuestionState) -> str:
    if state.prompt_kind == "two_questioners":
        target_mentions = format_user_mentions(state.participant_user_ids)
        questioners_remaining = [
            uid for uid in [state.winner_id, state.extra_questioner_id]
            if uid is not None and uid not in state.questioners_asked
        ]
        questioner_mentions = " and ".join(f"<@{uid}>" for uid in questioners_remaining)
        lines = [f"Someone rolled a **1**! {questioner_mentions} can each ask {target_mentions} a question."]
        if state.questioners_asked:
            already = " and ".join(
                f"<@{uid}>" for uid in [state.winner_id, state.extra_questioner_id]
                if uid is not None and uid in state.questioners_asked
            )
            lines.append(f"{already} has already asked.")
        lines.append("Click **Ask Question** to send your question.")
        return "\n".join(lines)

    if state.prompt_kind == "direct":
        selected_user_id = next(iter(sorted(state.participant_user_ids)), None)
        lowest_rolloff_note = format_lowest_rolloff_note(state.lowest_tie_user_ids, selected_user_id)
        target_mentions = format_user_mentions(state.participant_user_ids)
        lines = [f"<@{state.winner_id}> won the round."]
        if lowest_rolloff_note:
            lines.append(lowest_rolloff_note)
        if len(state.participant_user_ids) > 1:
            lines.append(f"They rolled **100**! Click **Ask Question** to send your question to {target_mentions}.")
        else:
            lines.append(f"Click **Ask Question** to send your question to {target_mentions}.")
        return "\n".join(lines)

    return (
        f"<@{state.winner_id}> rolled **69** and wins.\n"
        "Click **Ask Question** to post your question to everyone who rolled."
    )


def build_pending_question_summary(state: PendingQuestionState, question_text: str, asker_id: int | None = None) -> str:
    if state.prompt_kind == "two_questioners":
        uid = asker_id if asker_id is not None else state.winner_id
        target_mentions = format_user_mentions(state.participant_user_ids)
        return f"<@{uid}> asked {target_mentions}:\n{question_text}"

    if state.prompt_kind == "direct":
        target_mentions = format_user_mentions(state.participant_user_ids)
        return f"<@{state.winner_id}> asked {target_mentions}:\n{question_text}"

    return f"<@{state.winner_id}> rolled 69 and asked:\n{question_text}"


def build_embed(state: RiskyRollState) -> discord.Embed:
    embed = discord.Embed(title="Risky Rolls", color=discord.Color.gold())
    if state.is_open:
        if state.reroll_user_ids:
            embed.description = "Tie for highest roll. Tied players must reroll."
        else:
            embed.description = "Press **Roll** to join this round."
    else:
        embed.description = "Round closed."

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
            reroll_text = f"Tied users: {state.reroll_mentions()}"
            pending_mentions = state.pending_reroll_mentions()
            if pending_mentions:
                reroll_text += f"\nWaiting on: {pending_mentions}"
            embed.add_field(name="Reroll", value=reroll_text, inline=False)
        return embed

    sorted_rolls = sorted(state.rolls.items(), key=lambda item: item[1], reverse=True)
    lines = [f"**{roll}** - <@{user_id}>" for user_id, roll in sorted_rolls]
    embed.add_field(name=f"Rolls ({len(state.rolls)})", value="\n".join(lines), inline=False)

    if state.reroll_user_ids:
        reroll_text = f"Tied users: {state.reroll_mentions()}"
        pending_mentions = state.pending_reroll_mentions()
        if pending_mentions:
            reroll_text += f"\nWaiting on: {pending_mentions}"
        else:
            reroll_text += "\nAll rerolls are in. Close the round again."
        embed.add_field(name="Reroll", value=reroll_text, inline=False)

    if not state.is_open and state.highest_user:
        high_mention = f"<@{state.highest_user}>"
        if state.lowest_user is None:
            result = f"69 rolled.\n{high_mention} wins and asks the room a question in a thread."
        else:
            low_mention = f"<@{state.lowest_user}>"
            winner_rolled_100 = state.rolls.get(state.highest_user) == 100
            loser_rolled_1 = state.rolls.get(state.lowest_user) == 1

            if winner_rolled_100 and state.second_lowest_user is not None:
                result = f"{high_mention} rolled **100** and asks\n{low_mention} and <@{state.second_lowest_user}> answer"
            elif loser_rolled_1 and state.second_highest_user is not None:
                result = f"{high_mention} and <@{state.second_highest_user}> each ask\n{low_mention} rolled **1** and answers"
            else:
                result = f"{high_mention} asks\n{low_mention} answers"

            lowest_rolloff_note = format_lowest_rolloff_note(
                state.lowest_tie_user_ids,
                state.lowest_user,
            )
            if lowest_rolloff_note:
                result += f"\n{lowest_rolloff_note}"

            if winner_rolled_100 and loser_rolled_1:
                result += "\n*(both the 100 and 1 rules apply)*"

        embed.add_field(name="Result", value=result, inline=False)

    return embed


def build_rolloff_embed(
    tied_user_ids: list[int],
    rounds: list[dict[int, int]],
    winner_id: int,
    title: str = "Tie Rolloff",
) -> discord.Embed:
    embed = discord.Embed(title=title, color=discord.Color.orange())
    roll_label = "Lowest roll tied" if "lowest" in title.lower() else "Highest roll tied"
    embed.description = (
        f"{roll_label}, so an automatic rolloff was run.\n"
        f"Initial tied players: {', '.join(f'<@{user_id}>' for user_id in sorted(set(tied_user_ids)))}"
    )

    pick_lowest = "lowest" in title.lower()
    for index, round_rolls in enumerate(rounds, start=1):
        sorted_rolls = sorted(round_rolls.items(), key=lambda item: item[1], reverse=not pick_lowest)
        lines = [f"**{roll}** - <@{user_id}>" for user_id, roll in sorted_rolls]
        embed.add_field(name=f"Rolloff Round {index}", value="\n".join(lines), inline=False)

    winner_label = "Selected Lowest" if pick_lowest else "Rolloff Winner"
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
    channel: discord.abc.GuildChannel | discord.Thread | None,
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
