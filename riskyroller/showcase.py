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


def build_showcase_messages() -> list[tuple[str, str | None, list[discord.Embed]]]:
    """Return (header, content, embeds) tuples for every game-flow visual."""
    flows: list[tuple[str, str | None, list[discord.Embed]]] = []

    flows.append((
        "Flow 1 — open round, no rolls yet",
        None,
        [_styled(build_embed(_state(
            {},
            is_open=True,
            auto_close_players=25,
            auto_close_minutes=120,
        )))],
    ))

    flows.append((
        "Flow 2 — open round, rolls in progress",
        None,
        [_styled(build_embed(_state(
            {1: 87, 2: 42, 4: 73, 5: 99, 7: 31},
            is_open=True,
            auto_close_players=25,
            auto_close_minutes=120,
        )))],
    ))

    flows.append((
        "Flow 3 — closed round, normal result",
        None,
        [_styled(build_embed(_state(
            {1: 87, 2: 42, 3: 15, 4: 73, 5: 96},
            is_open=False,
            highest_user=5,
            lowest_user=3,
        )))],
    ))

    flows.append((
        "Flow 4 — winner rolled 100 (two answerers)",
        None,
        [_styled(build_embed(_state(
            {1: 100, 2: 47, 3: 15, 4: 23, 5: 78},
            is_open=False,
            highest_user=1,
            lowest_user=3,
            second_lowest_user=4,
        )))],
    ))

    flows.append((
        "Flow 5 — loser rolled 1 (two askers)",
        None,
        [_styled(build_embed(_state(
            {1: 1, 2: 47, 3: 81, 4: 23, 5: 78},
            is_open=False,
            highest_user=3,
            lowest_user=1,
            second_highest_user=5,
        )))],
    ))

    flows.append((
        "Flow 6 — someone rolled 69 (asks the room)",
        None,
        [_styled(build_embed(_state(
            {1: 47, 2: 12, 3: 69, 4: 23, 5: 88},
            is_open=False,
            highest_user=3,
        )))],
    ))

    reroll_state = _state(
        {3: 41, 4: 12, 1: 65},
        is_open=True,
        reroll_user_ids={1, 2},
    )
    flows.append((
        "Flow 7 — tied for highest, mid-reroll",
        None,
        [_styled(build_embed(reroll_state))],
    ))

    flows.append((
        "Flow 8 — tie rolloff resolution",
        None,
        [_styled(build_rolloff_embed(
            tied_user_ids=[2, 4, 5],
            rounds=[{2: 50, 4: 50, 5: 75}],
            winner_id=5,
        ))],
    ))

    flows.append((
        "Flow 9 — pending prompt, winner asks loser",
        _replace_mentions_plain(build_pending_prompt_content(_pending(
            prompt_kind=PromptKind.DIRECT,
            winner_id=5,
            participants={3},
        ))),
        [],
    ))

    flows.append((
        "Flow 10 — pending prompt, 100 rule (winner asks two losers)",
        _replace_mentions_plain(build_pending_prompt_content(_pending(
            prompt_kind=PromptKind.DIRECT,
            winner_id=1,
            participants={3, 4},
        ))),
        [],
    ))

    flows.append((
        "Flow 11 — pending prompt, two questioners (1 rule)",
        _replace_mentions_plain(build_pending_prompt_content(_pending(
            prompt_kind=PromptKind.TWO_QUESTIONERS,
            winner_id=3,
            extra_questioner_id=5,
            participants={1},
        ))),
        [],
    ))

    flows.append((
        "Flow 12 — pending prompt, 69 (asks the room)",
        _replace_mentions_plain(build_pending_prompt_content(_pending(
            prompt_kind=PromptKind.ROOM,
            winner_id=3,
            participants=set(),
        ))),
        [],
    ))

    flows.append((
        "Flow 13 — question-asked summary",
        _replace_mentions_plain(build_pending_question_summary(
            _pending(
                prompt_kind=PromptKind.DIRECT,
                winner_id=5,
                participants={3},
            ),
            "If you had to fight a Billy, which Billy and why?",
        )),
        [],
    ))

    return flows
