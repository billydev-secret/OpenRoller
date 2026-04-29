from dataclasses import dataclass, field

import discord

from .formatters import (
    build_embed,
    build_pending_prompt_content,
    build_pending_question_summary,
    build_rolloff_embed,
)
from .models import PendingQuestionState, PromptKind, RiskyRollState

FAKE_USER_URL = "https://discord.com/users/0"

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
}


@dataclass
class FlowItem:
    header: str
    content: str | None = None
    embeds: list[discord.Embed] = field(default_factory=list)
    thread_name: str | None = None
    thread_messages: list[str] = field(default_factory=list)


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


def _open_embed(rolls: dict[int, int]) -> discord.Embed:
    return _styled(build_embed(_state(
        rolls,
        is_open=True,
        auto_close_players=25,
        auto_close_minutes=120,
    )))


def _closed_embed(rolls: dict[int, int], **extra) -> discord.Embed:
    return _styled(build_embed(_state(rolls, is_open=False, **extra)))


def _prompt_content(
    *,
    prompt_kind: PromptKind,
    winner_id: int,
    participants: set[int],
    extra_questioner_id: int | None = None,
) -> str:
    return _replace_mentions_plain(build_pending_prompt_content(_pending(
        prompt_kind=prompt_kind,
        winner_id=winner_id,
        participants=participants,
        extra_questioner_id=extra_questioner_id,
    )))


def _question_summary(
    *,
    prompt_kind: PromptKind,
    winner_id: int,
    participants: set[int],
    question: str,
    extra_questioner_id: int | None = None,
) -> str:
    return _replace_mentions_plain(build_pending_question_summary(
        _pending(
            prompt_kind=prompt_kind,
            winner_id=winner_id,
            participants=participants,
            extra_questioner_id=extra_questioner_id,
        ),
        question,
    ))


def build_showcase_messages() -> list[FlowItem]:
    """Sequence of fake messages that read like a series of real games end-to-end."""
    items: list[FlowItem] = []

    # ── Game 1 — straightforward round ────────────────────────────────────
    items.append(FlowItem(
        header="Game 1 · round opens",
        embeds=[_open_embed({})],
    ))
    items.append(FlowItem(
        header="Game 1 · rolls coming in",
        embeds=[_open_embed({1: 87, 2: 42, 4: 73, 5: 99, 7: 31})],
    ))
    items.append(FlowItem(
        header="Game 1 · round closes — Lil Bill wins, Billiam answers",
        embeds=[_closed_embed(
            {1: 87, 2: 42, 4: 73, 5: 99, 7: 31},
            highest_user=5, lowest_user=2,
        )],
    ))
    items.append(FlowItem(
        header="Game 1 · question prompt",
        content=_prompt_content(
            prompt_kind=PromptKind.DIRECT, winner_id=5, participants={2},
        ),
    ))
    items.append(FlowItem(
        header="Game 1 · question asked",
        content=_question_summary(
            prompt_kind=PromptKind.DIRECT, winner_id=5, participants={2},
            question="What's the worst lie you've told to get out of doing chores?",
        ),
    ))

    # ── Game 2 — 100 rule ─────────────────────────────────────────────────
    items.append(FlowItem(
        header="Game 2 · round opens",
        embeds=[_open_embed({})],
    ))
    items.append(FlowItem(
        header="Game 2 · Billy rolls a perfect 100",
        embeds=[_open_embed({1: 100, 2: 47, 3: 15, 4: 23, 5: 78})],
    ))
    items.append(FlowItem(
        header="Game 2 · 100 rule — winner asks the two lowest",
        embeds=[_closed_embed(
            {1: 100, 2: 47, 3: 15, 4: 23, 5: 78},
            highest_user=1, lowest_user=3, second_lowest_user=4,
        )],
    ))
    items.append(FlowItem(
        header="Game 2 · question prompt",
        content=_prompt_content(
            prompt_kind=PromptKind.DIRECT, winner_id=1, participants={3, 4},
        ),
    ))
    items.append(FlowItem(
        header="Game 2 · question asked",
        content=_question_summary(
            prompt_kind=PromptKind.DIRECT, winner_id=1, participants={3, 4},
            question="What's something you've Googled that you'd never want anyone to see?",
        ),
    ))

    # ── Game 3 — 1 rule (two questioners) ─────────────────────────────────
    items.append(FlowItem(
        header="Game 3 · round opens",
        embeds=[_open_embed({})],
    ))
    items.append(FlowItem(
        header="Game 3 · oof — Billy rolled a 1",
        embeds=[_open_embed({1: 1, 2: 47, 3: 81, 4: 23, 5: 78})],
    ))
    items.append(FlowItem(
        header="Game 3 · 1 rule — top two both ask Billy",
        embeds=[_closed_embed(
            {1: 1, 2: 47, 3: 81, 4: 23, 5: 78},
            highest_user=3, lowest_user=1, second_highest_user=5,
        )],
    ))
    items.append(FlowItem(
        header="Game 3 · two-questioner prompt",
        content=_prompt_content(
            prompt_kind=PromptKind.TWO_QUESTIONERS,
            winner_id=3, extra_questioner_id=5, participants={1},
        ),
    ))
    items.append(FlowItem(
        header="Game 3 · first question asked",
        content=_question_summary(
            prompt_kind=PromptKind.TWO_QUESTIONERS,
            winner_id=3, extra_questioner_id=5, participants={1},
            question="Have you ever cried watching a Pixar movie? Which one?",
        ),
    ))

    # ── Game 4 — 69 rule (with thread) ────────────────────────────────────
    sixty_nine_question = "What's the most embarrassing nickname you've ever had?"
    sixty_nine_targets = "**@Billy** **@Billiam** **@Wild Bill** **@Lil Bill**"
    items.append(FlowItem(
        header="Game 4 · round opens",
        embeds=[_open_embed({})],
    ))
    items.append(FlowItem(
        header="Game 4 · 🔥🔥🔥 NICE 🔥🔥🔥",
        embeds=[_closed_embed(
            {1: 47, 2: 12, 3: 69, 4: 23, 5: 88},
            highest_user=3,
        )],
    ))
    items.append(FlowItem(
        header="Game 4 · 69 prompt + thread",
        content=_prompt_content(
            prompt_kind=PromptKind.ROOM, winner_id=3, participants=set(),
        ),
        thread_name=sixty_nine_question,
        thread_messages=[
            f"{sixty_nine_targets}\n**@Bilbo Baggins** asks:\n{sixty_nine_question}",
            "**@Wild Bill**: my mom called me Sneezy on account of the allergies. fifteen years.",
            "**@Billford**: my older brother called me Bilfridge until i was twelve. i still flinch at appliance ads.",
            "**@Lil Bill**: i refuse to answer on the grounds that it could be used against me",
        ],
    ))

    # ── Game 5 — tie at the top, rerolls ──────────────────────────────────
    items.append(FlowItem(
        header="Game 5 · round opens",
        embeds=[_open_embed({})],
    ))
    items.append(FlowItem(
        header="Game 5 · tied for highest — Billy & Billiam must reroll",
        embeds=[_styled(build_embed(_state(
            {3: 41, 4: 12},
            is_open=True,
            reroll_user_ids={1, 2},
            auto_close_players=25,
            auto_close_minutes=120,
        )))],
    ))
    items.append(FlowItem(
        header="Game 5 · Billy rerolled, waiting on Billiam",
        embeds=[_styled(build_embed(_state(
            {3: 41, 4: 12, 1: 65},
            is_open=True,
            reroll_user_ids={1, 2},
            auto_close_players=25,
            auto_close_minutes=120,
        )))],
    ))
    items.append(FlowItem(
        header="Game 5 · round closes — Billiam takes it",
        embeds=[_closed_embed(
            {3: 41, 4: 12, 1: 65, 2: 92},
            highest_user=2, lowest_user=4,
        )],
    ))
    items.append(FlowItem(
        header="Game 5 · question prompt",
        content=_prompt_content(
            prompt_kind=PromptKind.DIRECT, winner_id=2, participants={4},
        ),
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
