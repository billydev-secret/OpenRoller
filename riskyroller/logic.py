import random


def serialize_user_ids(user_ids: set[int]) -> str | None:
    if not user_ids:
        return None
    return ",".join(str(user_id) for user_id in sorted(user_ids))


def deserialize_user_ids(raw: str | None) -> set[int]:
    if not raw:
        return set()
    return {int(part) for part in raw.split(",") if part}


def run_tie_rolloff(tied_user_ids: list[int]) -> tuple[int, list[dict[int, int]]]:
    contenders = sorted(set(tied_user_ids))
    rounds: list[dict[int, int]] = []

    while True:
        round_rolls = {user_id: random.randint(1, 100) for user_id in contenders}
        rounds.append(round_rolls)
        max_value = max(round_rolls.values())
        winners = sorted(user_id for user_id, roll in round_rolls.items() if roll == max_value)
        if len(winners) == 1:
            return winners[0], rounds
        contenders = winners
