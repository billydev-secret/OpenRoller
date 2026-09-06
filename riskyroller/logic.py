"""Pure decision logic for Risky Rolls.

Everything here takes and returns plain Python values, so it is unit-testable
without Discord. The serialization helpers are the storage round-trip used by
``store.py`` for the comma-joined ``TEXT`` columns; :func:`run_tie_rolloff` is
the random-driven loop that settles ties for highest and lowest — kept here so
tests can patch ``random.randint`` and drive it deterministically.
"""

import random


def serialize_user_ids(user_ids: set[int]) -> str | None:
    """Comma-join sorted user IDs for sqlite TEXT storage.

    Returns ``None`` for empty sets so the column reads as NULL — the
    deserializer treats ``None`` and the empty string as "no users".
    """
    if not user_ids:
        return None
    return ",".join(str(uid) for uid in sorted(user_ids))


def deserialize_user_ids(raw: str | None) -> set[int]:
    """Parse the comma-joined user-ID string back into a set.

    ``None`` or empty input returns an empty set, matching what
    :func:`serialize_user_ids` writes for empty inputs.
    """
    if not raw:
        return set()
    return {int(part) for part in raw.split(",") if part}


# What a round needs from the bot in the channel it runs in. View Channel
# comes first because without it every button still works (interactions do
# not need it) while everything the bot does on its own — the auto-close
# prompt, disabling old prompts — quietly fails.
REQUIRED_CHANNEL_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("view_channel", "View Channel"),
    ("send_messages", "Send Messages"),
    ("embed_links", "Embed Links"),
)


# What the 69 room question needs on top, to open its thread. Not required
# to start a round: when they are missing the question falls back to the
# channel, and the asker is told why.
REQUIRED_THREAD_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("create_public_threads", "Create Public Threads"),
    ("send_messages_in_threads", "Send Messages in Threads"),
)


def missing_permissions(app_permissions, required=REQUIRED_CHANNEL_PERMISSIONS) -> list[str]:
    """Labels from *required* that *app_permissions* lacks, in the order given.

    Takes ``interaction.app_permissions`` — Discord's own computation of what
    the bot may do where the command was used. Checking the channel object
    instead is unreliable: for a channel the bot cannot see it is built from
    the interaction's partial payload, which carries no overwrites, so it
    reports role permissions and misses a channel-level deny.
    """
    return [label for attr, label in required if not getattr(app_permissions, attr)]


def missing_start_permissions(app_permissions) -> list[str]:
    """Names of the permissions a round needs that *app_permissions* lacks."""
    return missing_permissions(app_permissions, REQUIRED_CHANNEL_PERMISSIONS)


def effective_min_game_seconds(
    configured: dict[int, int],
    guild_id: int,
    skip_min_game_time: bool,
    default: int,
) -> int:
    """How long a round in *guild_id* must stay open before it can close.

    One lookup for every close path, so a server's configured value can never
    mean one thing to the Close button and another to the auto-close. A server
    that set 0 gets 0; a server that never set anything gets *default*, and
    that is where the paths deliberately differ: the auto-close passes
    DEFAULT_MIN_GAME_SECONDS, so a round doesn't end the instant the last
    expected player rolls, while the Close button passes 0, because a default
    nobody chose shouldn't stop the opener closing their own round.
    ``skip_min_game_time`` is the per-round opt-out and wins outright.
    """
    if skip_min_game_time:
        return 0
    return int(configured.get(guild_id, default))


def run_tie_rolloff(
    tied_user_ids: list[int], pick_lowest: bool = False
) -> tuple[int, list[dict[int, int]]]:
    """Roll 1-100 for each contender until one wins (or loses, if pick_lowest).

    Returns ``(winner_id, rounds)`` where ``rounds`` is the list of
    ``{user_id: roll}`` dicts produced in order — the formatters use
    this to render a per-round rolloff embed.
    """
    contenders = sorted(set(tied_user_ids))
    rounds: list[dict[int, int]] = []

    while True:
        round_rolls = {uid: random.randint(1, 100) for uid in contenders}
        rounds.append(round_rolls)
        target = min(round_rolls.values()) if pick_lowest else max(round_rolls.values())
        winners = sorted(uid for uid, roll in round_rolls.items() if roll == target)
        if len(winners) == 1:
            return winners[0], rounds
        contenders = winners
