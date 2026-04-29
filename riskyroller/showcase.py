from collections.abc import Callable
from dataclasses import dataclass, field

import discord

from .formatters import (
    build_embed,
    build_pending_prompt_embed,
    build_question_post_embed,
    build_question_reply_embed,
    build_rolloff_embed,
    collect_prompt_mention_ids,
    format_mention_list,
    format_user_mentions,
)
from .models import PendingQuestionState, PostedQuestionState, PromptKind, RiskyRollState
from .views import QuestionReplyView, RiskyRollView, SixtyNineQuestionView

FAKE_USER_URL = "https://discord.com"

FAKE_USERS: dict[int, str] = {
    1: "Billy",
    2: "Billiam",
    3: "Bilbo Baggins",
    4: "Wild Bill",
    5: "Lil Bill",
    6: "Billford",
    7: "Big Willy",
    8: "Sir Billsworth",
    9: "Billington",
    10: "Filthy Bill",
    11: "Billbo",
    12: "Billy Bob",
    13: "Buffalo Bill",
    14: "Bill Murray",
    15: "Billiards",
    16: "Billkin",
    17: "Coach Bill",
    18: "Bill Jr.",
    19: "Sir Billiam",
    20: "Doc Bill",
}


@dataclass
class ThreadMessage:
    content: str | None = None
    embed: discord.Embed | None = None


@dataclass
class FlowItem:
    header: str
    content: str | None = None
    embeds: list[discord.Embed] = field(default_factory=list)
    view_factory: Callable[[], discord.ui.View] | None = None
    thread_name: str | None = None
    thread_messages: list[str | ThreadMessage] = field(default_factory=list)


def _open_view() -> discord.ui.View:
    return RiskyRollView("showcase")


def _closed_view() -> discord.ui.View:
    view = RiskyRollView("showcase")
    view.disable_all_items()
    return view


def _ask_view() -> discord.ui.View:
    return SixtyNineQuestionView("showcase")


def _reply_view() -> discord.ui.View:
    return QuestionReplyView()


def _replace_mentions_embed(text: str) -> str:
    for uid, name in FAKE_USERS.items():
        text = text.replace(f"<@{uid}>", f"[@{name}]({FAKE_USER_URL})")
    return text


def _replace_mentions_plain(text: str) -> str:
    for uid, name in FAKE_USERS.items():
        text = text.replace(f"<@{uid}>", f"**@{name}**")
    return text


def _styled(embed: discord.Embed) -> discord.Embed:
    if embed.description:
        embed.description = _replace_mentions_embed(embed.description)
    for index, embed_field in enumerate(embed.fields):
        embed.set_field_at(
            index,
            name=embed_field.name,
            value=_replace_mentions_embed(embed_field.value or ""),
            inline=embed_field.inline,
        )
    return embed


def _state(rolls: dict[int, int], *, is_open: bool, **extra) -> RiskyRollState:
    state = RiskyRollState(channel_id=0, guild_id=0, opener_id=1, is_open=is_open)
    state.rolls = dict(rolls)
    for key, value in extra.items():
        setattr(state, key, value)
    return state


def _pending(
    *,
    prompt_kind: PromptKind,
    winner_id: int,
    participants: set[int],
    extra_questioner_id: int | None = None,
) -> PendingQuestionState:
    return PendingQuestionState(
        channel_id=0,
        guild_id=0,
        winner_id=winner_id,
        participant_user_ids=participants,
        game_id="showcase",
        prompt_kind=prompt_kind,
        extra_questioner_id=extra_questioner_id,
    )


def _posted(
    *,
    asker_id: int,
    repliers: set[int],
    question: str,
    asker_rolled_100: bool = False,
    target_rolled_1: bool = False,
) -> PostedQuestionState:
    return PostedQuestionState(
        message_id=0,
        channel_id=0,
        guild_id=0,
        asker_id=asker_id,
        allowed_replier_ids=repliers,
        question_text=question,
        asker_rolled_100=asker_rolled_100,
        target_rolled_1=target_rolled_1,
    )


def _open_embed(rolls: dict[int, int]) -> discord.Embed:
    return _styled(build_embed(_state(
        rolls,
        is_open=True,
        auto_close_players=25,
        auto_close_minutes=120,
    )))


def _closed_embed(rolls: dict[int, int], **extra) -> discord.Embed:
    return _styled(build_embed(_state(rolls, is_open=False, **extra)))


def _prompt_pings(
    *,
    prompt_kind: PromptKind,
    winner_id: int,
    participants: set[int],
    extra_questioner_id: int | None = None,
) -> str:
    state = _pending(
        prompt_kind=prompt_kind,
        winner_id=winner_id,
        participants=participants,
        extra_questioner_id=extra_questioner_id,
    )
    return _replace_mentions_plain(format_mention_list(collect_prompt_mention_ids(state)))


def _prompt_embed(
    *,
    prompt_kind: PromptKind,
    winner_id: int,
    participants: set[int],
    extra_questioner_id: int | None = None,
) -> discord.Embed:
    state = _pending(
        prompt_kind=prompt_kind,
        winner_id=winner_id,
        participants=participants,
        extra_questioner_id=extra_questioner_id,
    )
    return _styled(build_pending_prompt_embed(state))


def _question_post_pings(participants: set[int]) -> str:
    return _replace_mentions_plain(format_user_mentions(participants))


def _question_post_embed(
    *,
    asker_id: int,
    repliers: set[int],
    question: str,
    asker_rolled_100: bool = False,
    target_rolled_1: bool = False,
) -> discord.Embed:
    return _styled(build_question_post_embed(_posted(
        asker_id=asker_id,
        repliers=repliers,
        question=question,
        asker_rolled_100=asker_rolled_100,
        target_rolled_1=target_rolled_1,
    )))


def _reply_embed(
    *,
    asker_id: int,
    repliers: set[int],
    question: str,
    replier_id: int,
    reply: str,
    asker_rolled_100: bool = False,
    target_rolled_1: bool = False,
) -> discord.Embed:
    state = _posted(
        asker_id=asker_id,
        repliers=repliers,
        question=question,
        asker_rolled_100=asker_rolled_100,
        target_rolled_1=target_rolled_1,
    )
    return _styled(build_question_reply_embed(state, replier_id, reply))


def build_showcase_messages() -> list[FlowItem]:
    """Sequence of fake messages that read like a series of real games end-to-end."""
    items: list[FlowItem] = []

    # ── Game 1 — straightforward round ────────────────────────────────────
    game1_rolls = {
        1: 87, 2: 11, 3: 64, 4: 73, 5: 99, 6: 42, 7: 31, 8: 56, 9: 78, 10: 38,
        11: 65, 12: 47, 13: 22, 14: 81, 15: 53, 16: 67, 17: 29, 18: 71, 19: 44, 20: 35,
    }
    items.append(FlowItem(
        header="Game 1 · round opens",
        embeds=[_open_embed({})],
        view_factory=_open_view,
    ))
    items.append(FlowItem(
        header="Game 1 · rolls coming in",
        embeds=[_open_embed(game1_rolls)],
        view_factory=_open_view,
    ))
    items.append(FlowItem(
        header="Game 1 · round closes — Lil Bill wins, Billiam answers",
        embeds=[_closed_embed(
            game1_rolls,
            highest_user=5, lowest_user=2,
        )],
        view_factory=_closed_view,
    ))
    items.append(FlowItem(
        header="Game 1 · question prompt",
        content=_prompt_pings(
            prompt_kind=PromptKind.DIRECT, winner_id=5, participants={2},
        ),
        embeds=[_prompt_embed(
            prompt_kind=PromptKind.DIRECT, winner_id=5, participants={2},
        )],
        view_factory=_ask_view,
    ))
    game1_question = "What's the worst lie you've told to get out of doing chores?"
    items.append(FlowItem(
        header="Game 1 · question asked",
        content=_question_post_pings({2}),
        embeds=[_question_post_embed(
            asker_id=5, repliers={2}, question=game1_question,
        )],
        view_factory=_reply_view,
    ))
    items.append(FlowItem(
        header="Game 1 · Billiam replies",
        embeds=[_reply_embed(
            asker_id=5, repliers={2},
            question=game1_question,
            replier_id=2,
            reply="told my mom i had pinkeye for two weeks straight to get out of dish duty. i did not have pinkeye.",
        )],
    ))

    # ── Game 2 — 100 rule ─────────────────────────────────────────────────
    game2_rolls = {
        1: 100, 2: 47, 3: 15, 4: 23, 5: 78, 6: 56, 7: 41, 8: 67, 9: 32, 10: 88,
        11: 54, 12: 73, 13: 26, 14: 60, 15: 39, 16: 81, 17: 45, 18: 70, 19: 33, 20: 51,
    }
    items.append(FlowItem(
        header="Game 2 · round opens",
        embeds=[_open_embed({})],
        view_factory=_open_view,
    ))
    items.append(FlowItem(
        header="Game 2 · Billy rolls a perfect 100",
        embeds=[_open_embed(game2_rolls)],
        view_factory=_open_view,
    ))
    items.append(FlowItem(
        header="Game 2 · 100 rule — winner asks the two lowest",
        embeds=[_closed_embed(
            game2_rolls,
            highest_user=1, lowest_user=3, second_lowest_user=4,
        )],
        view_factory=_closed_view,
    ))
    items.append(FlowItem(
        header="Game 2 · question prompt",
        content=_prompt_pings(
            prompt_kind=PromptKind.DIRECT, winner_id=1, participants={3, 4},
        ),
        embeds=[_prompt_embed(
            prompt_kind=PromptKind.DIRECT, winner_id=1, participants={3, 4},
        )],
        view_factory=_ask_view,
    ))
    game2_question = "What's something you've Googled that you'd never want anyone to see?"
    items.append(FlowItem(
        header="Game 2 · question asked",
        content=_question_post_pings({3, 4}),
        embeds=[_question_post_embed(
            asker_id=1, repliers={3, 4}, question=game2_question,
            asker_rolled_100=True,
        )],
        view_factory=_reply_view,
    ))
    items.append(FlowItem(
        header="Game 2 · Wild Bill replies",
        embeds=[_reply_embed(
            asker_id=1, repliers={3, 4},
            question=game2_question,
            replier_id=4,
            reply='"how to apologize to a turkey" — long story. it was thanksgiving. the turkey is fine.',
            asker_rolled_100=True,
        )],
    ))

    # ── Game 3 — 1 rule (two questioners) ─────────────────────────────────
    game3_rolls = {
        1: 1, 2: 47, 3: 96, 4: 23, 5: 91, 6: 56, 7: 41, 8: 67, 9: 32, 10: 88,
        11: 54, 12: 73, 13: 26, 14: 60, 15: 39, 16: 78, 17: 45, 18: 70, 19: 33, 20: 51,
    }
    items.append(FlowItem(
        header="Game 3 · round opens",
        embeds=[_open_embed({})],
        view_factory=_open_view,
    ))
    items.append(FlowItem(
        header="Game 3 · oof — Billy rolled a 1",
        embeds=[_open_embed(game3_rolls)],
        view_factory=_open_view,
    ))
    items.append(FlowItem(
        header="Game 3 · 1 rule — top two both ask Billy",
        embeds=[_closed_embed(
            game3_rolls,
            highest_user=3, lowest_user=1, second_highest_user=5,
        )],
        view_factory=_closed_view,
    ))
    items.append(FlowItem(
        header="Game 3 · two-questioner prompt",
        content=_prompt_pings(
            prompt_kind=PromptKind.TWO_QUESTIONERS,
            winner_id=3, extra_questioner_id=5, participants={1},
        ),
        embeds=[_prompt_embed(
            prompt_kind=PromptKind.TWO_QUESTIONERS,
            winner_id=3, extra_questioner_id=5, participants={1},
        )],
        view_factory=_ask_view,
    ))
    game3_q1 = "Have you ever cried watching a Pixar movie? Which one?"
    game3_q2 = "Worst meal you've ever cooked for someone you wanted to impress?"
    items.append(FlowItem(
        header="Game 3 · first question asked (Bilbo)",
        content=_question_post_pings({1}),
        embeds=[_question_post_embed(
            asker_id=3, repliers={1}, question=game3_q1,
            target_rolled_1=True,
        )],
        view_factory=_reply_view,
    ))
    items.append(FlowItem(
        header="Game 3 · Billy replies to Bilbo",
        embeds=[_reply_embed(
            asker_id=3, repliers={1},
            question=game3_q1,
            replier_id=1,
            reply="every single one. inside out hit me like a truck. i was thirty-two.",
            target_rolled_1=True,
        )],
    ))
    items.append(FlowItem(
        header="Game 3 · second question asked (Lil Bill)",
        content=_question_post_pings({1}),
        embeds=[_question_post_embed(
            asker_id=5, repliers={1}, question=game3_q2,
            target_rolled_1=True,
        )],
        view_factory=_reply_view,
    ))
    items.append(FlowItem(
        header="Game 3 · Billy replies to Lil Bill",
        embeds=[_reply_embed(
            asker_id=5, repliers={1},
            question=game3_q2,
            replier_id=1,
            reply="raw chicken parmesan. she's now my ex. unrelated, probably.",
            target_rolled_1=True,
        )],
    ))

    # ── Game 4 — 69 rule (with thread) ────────────────────────────────────
    sixty_nine_question = "What's the most embarrassing nickname you've ever had?"
    sixty_nine_target_ids = {uid for uid in FAKE_USERS if uid != 3}
    game4_rolls = {
        1: 47, 2: 12, 3: 69, 4: 23, 5: 88, 6: 56, 7: 41, 8: 67, 9: 32, 10: 78,
        11: 54, 12: 73, 13: 26, 14: 60, 15: 39, 16: 81, 17: 45, 18: 70, 19: 33, 20: 51,
    }
    items.append(FlowItem(
        header="Game 4 · round opens",
        embeds=[_open_embed({})],
        view_factory=_open_view,
    ))
    items.append(FlowItem(
        header="Game 4 · 🔥🔥🔥 NICE 🔥🔥🔥",
        embeds=[_closed_embed(
            game4_rolls,
            highest_user=3,
        )],
        view_factory=_closed_view,
    ))
    items.append(FlowItem(
        header="Game 4 · 69 prompt + thread",
        content=_prompt_pings(
            prompt_kind=PromptKind.ROOM, winner_id=3, participants=set(),
        ),
        embeds=[_prompt_embed(
            prompt_kind=PromptKind.ROOM, winner_id=3, participants=set(),
        )],
        view_factory=_ask_view,
        thread_name=sixty_nine_question,
        thread_messages=[
            ThreadMessage(
                content=_question_post_pings(sixty_nine_target_ids),
                embed=_question_post_embed(
                    asker_id=3,
                    repliers=sixty_nine_target_ids,
                    question=sixty_nine_question,
                ),
            ),
            "**@Wild Bill**: my mom called me Sneezy on account of the allergies. fifteen years.",
            "**@Billford**: my older brother called me Bilfridge until i was twelve. i still flinch at appliance ads.",
            "**@Lil Bill**: i refuse to answer on the grounds that it could be used against me",
        ],
    ))

    # ── Game 5 — tie at the top, rerolls ──────────────────────────────────
    game5_others = {
        3: 41, 4: 18, 5: 67, 6: 56, 7: 79, 8: 30, 9: 61, 10: 78,
        11: 54, 12: 73, 13: 26, 14: 60, 15: 39, 16: 81, 17: 45, 18: 70, 19: 33, 20: 51,
    }
    items.append(FlowItem(
        header="Game 5 · round opens",
        embeds=[_open_embed({})],
        view_factory=_open_view,
    ))
    items.append(FlowItem(
        header="Game 5 · tied for highest — Billy & Billiam must reroll",
        embeds=[_styled(build_embed(_state(
            game5_others,
            is_open=True,
            reroll_user_ids={1, 2},
            auto_close_players=25,
            auto_close_minutes=120,
        )))],
        view_factory=_open_view,
    ))
    items.append(FlowItem(
        header="Game 5 · Billy rerolled, waiting on Billiam",
        embeds=[_styled(build_embed(_state(
            {**game5_others, 1: 73},
            is_open=True,
            reroll_user_ids={1, 2},
            auto_close_players=25,
            auto_close_minutes=120,
        )))],
        view_factory=_open_view,
    ))
    items.append(FlowItem(
        header="Game 5 · round closes — Billiam takes it",
        embeds=[_closed_embed(
            {**game5_others, 1: 73, 2: 88},
            highest_user=2, lowest_user=4,
        )],
        view_factory=_closed_view,
    ))
    items.append(FlowItem(
        header="Game 5 · question prompt",
        content=_prompt_pings(
            prompt_kind=PromptKind.DIRECT, winner_id=2, participants={4},
        ),
        embeds=[_prompt_embed(
            prompt_kind=PromptKind.DIRECT, winner_id=2, participants={4},
        )],
        view_factory=_ask_view,
    ))
    game5_question = "If you had to live one day on repeat for the rest of your life, which day?"
    items.append(FlowItem(
        header="Game 5 · question asked",
        content=_question_post_pings({4}),
        embeds=[_question_post_embed(
            asker_id=2, repliers={4}, question=game5_question,
        )],
        view_factory=_reply_view,
    ))
    items.append(FlowItem(
        header="Game 5 · Wild Bill replies",
        embeds=[_reply_embed(
            asker_id=2, repliers={4},
            question=game5_question,
            replier_id=4,
            reply="the day i won fifty bucks scratching off a ticket. peak Wild Bill.",
        )],
    ))

    # ── Bonus — automatic tie rolloff (rare visual) ───────────────────────
    items.append(FlowItem(
        header="Bonus · automatic tie rolloff embed",
        embeds=[_styled(build_rolloff_embed(
            tied_user_ids=[2, 4, 5],
            rounds=[{2: 50, 4: 50, 5: 75}],
            winner_id=5,
        ))],
    ))

    return items
