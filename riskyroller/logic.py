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
