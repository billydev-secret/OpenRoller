import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto

from .logic import run_tie_rolloff

log = logging.getLogger(__name__)


class RoundResult(Enum):
    """Enumeration of possible round resolution outcomes."""
    NOT_ENOUGH = auto()
    WAITING_FOR_REROLLS = auto()
    TIE = auto()
    SIXTYNINE = auto()
    SIXTYNINE_TIE = auto()
    OK = auto()


@dataclass
class ResolutionResult:
    """Result of round resolution with rolloff data if applicable."""
    result_type: RoundResult
    rolloff_user_ids: list[int] = field(default_factory=list)
    rolloff_rounds: list[dict[int, int]] | None = None
    lowest_rolloff_user_ids: list[int] = field(default_factory=list)
    lowest_rolloff_rounds: list[dict[int, int]] | None = None


@dataclass
class RiskyRollState:
    channel_id: int
    guild_id: int
    opener_id: int
    message_id: int | None = None
    rolls: dict[int, int] = field(default_factory=dict)
    is_open: bool = True
    highest_user: int | None = None
    lowest_user: int | None = None
    lowest_tie_user_ids: set[int] = field(default_factory=set)
    reroll_user_ids: set[int] = field(default_factory=set)
    auto_close_players: int | None = None
    auto_close_minutes: int | None = None
    created_at: float = field(default_factory=time.time)

    def add_roll(self, user_id: int, value: int) -> None:
        """
        Add a roll for a user.
        If a reroll set is active, automatically clears it when all rerolls are complete.
        """
        self.rolls[user_id] = value
        if self.reroll_user_ids:
            completed_rerolls = {
                reroll_user for reroll_user in self.reroll_user_ids if reroll_user in self.rolls
            }
            if completed_rerolls == self.reroll_user_ids:
                self.reroll_user_ids.clear()

    def can_roll(self, user_id: int) -> bool:
        """
        Check if a user is allowed to roll.
        When a reroll set is active, only users in that set can roll (and only if they haven't yet).
        Otherwise, users can roll if they haven't rolled already.
        """
        if self.reroll_user_ids:
            return user_id in self.reroll_user_ids and user_id not in self.rolls
        return user_id not in self.rolls

    def prepare_reroll(self, user_ids: list[int]) -> None:
        """
        Prepare for a reroll by clearing specified users' rolls and resetting state.
        This is used when there's a tie - the tied players must reroll.
        """
        self.reroll_user_ids = set(user_ids)
        for user_id in self.reroll_user_ids:
            self.rolls.pop(user_id, None)
        self.highest_user = None
        self.lowest_user = None
        self.lowest_tie_user_ids.clear()

    def reroll_mentions(self) -> str:
        return ", ".join(f"<@{user_id}>" for user_id in sorted(self.reroll_user_ids))

    def pending_reroll_mentions(self) -> str:
        pending_user_ids = [user_id for user_id in self.reroll_user_ids if user_id not in self.rolls]
        return ", ".join(f"<@{user_id}>" for user_id in pending_user_ids)

    def resolve(self) -> ResolutionResult:
        """
        Resolve the round outcome and update state accordingly.
        Returns ResolutionResult with the outcome type and any rolloff data.
        """
        self.lowest_tie_user_ids.clear()

        if self.reroll_user_ids:
            pending_user_ids = [user_id for user_id in self.reroll_user_ids if user_id not in self.rolls]
            if pending_user_ids:
                return ResolutionResult(result_type=RoundResult.WAITING_FOR_REROLLS)

        if len(self.rolls) < 2:
            return ResolutionResult(result_type=RoundResult.NOT_ENOUGH)

        max_value = max(self.rolls.values())
        min_value = min(self.rolls.values())

        # Check for 69 rolls (automatic win)
        sixtyniners = [user_id for user_id, roll in self.rolls.items() if roll == 69]
        if sixtyniners:
            if len(sixtyniners) > 1:
                winner_id, rolloff_rounds = run_tie_rolloff(sixtyniners)
                self.highest_user = winner_id
                self.lowest_user = None
                self.is_open = False
                log.info("Channel %s: 69 tie resolved via rolloff. Winner: %s", self.channel_id, winner_id)
                return ResolutionResult(
                    result_type=RoundResult.SIXTYNINE_TIE,
                    rolloff_user_ids=sixtyniners,
                    rolloff_rounds=rolloff_rounds,
                )
            self.highest_user = sixtyniners[0]
            self.lowest_user = None
            self.is_open = False
            log.info("Channel %s: 69 rolled by user %s", self.channel_id, sixtyniners[0])
            return ResolutionResult(result_type=RoundResult.SIXTYNINE)

        # Check for tie on highest roll
        highest_users = [user_id for user_id, roll in self.rolls.items() if roll == max_value]
        if len(highest_users) > 1:
            winner_id, rolloff_rounds = run_tie_rolloff(highest_users)

            remaining_user_ids = [user_id for user_id in self.rolls if user_id != winner_id]
            lowest_rolloff_user_ids: list[int] = []
            lowest_rolloff_rounds: list[dict[int, int]] | None = None
            if remaining_user_ids:
                min_roll = min(self.rolls[user_id] for user_id in remaining_user_ids)
                lowest_tied = [u for u in remaining_user_ids if self.rolls[u] == min_roll]
                if len(lowest_tied) > 1:
                    lowest_id, lowest_rolloff_rounds = run_tie_rolloff(lowest_tied)
                    self.lowest_tie_user_ids = set(lowest_tied)
                    lowest_rolloff_user_ids = lowest_tied
                else:
                    lowest_id = lowest_tied[0]
            else:
                lowest_id = winner_id

            self.highest_user = winner_id
            self.lowest_user = lowest_id
            self.is_open = False
            self.reroll_user_ids.clear()
            log.info("Channel %s: Highest tie resolved. Winner: %s, Lowest: %s", self.channel_id, winner_id, lowest_id)
            return ResolutionResult(
                result_type=RoundResult.TIE,
                rolloff_user_ids=highest_users,
                rolloff_rounds=rolloff_rounds,
                lowest_rolloff_user_ids=lowest_rolloff_user_ids,
                lowest_rolloff_rounds=lowest_rolloff_rounds,
            )

        # Standard outcome: single highest, possibly tied lowest
        lowest_users = [user_id for user_id, roll in self.rolls.items() if roll == min_value]
        lowest_rolloff_rounds: list[dict[int, int]] | None = None
        if len(lowest_users) > 1:
            lowest_id, lowest_rolloff_rounds = run_tie_rolloff(lowest_users)
            self.lowest_tie_user_ids = set(lowest_users)
            log.info("Channel %s: Lowest tie resolved via rolloff. Selected: %s", self.channel_id, lowest_id)
        else:
            lowest_id = lowest_users[0]

        self.highest_user = highest_users[0]
        self.lowest_user = lowest_id
        self.is_open = False
        log.info("Channel %s: Round resolved. Winner: %s, Lowest: %s", self.channel_id, highest_users[0], lowest_id)
        return ResolutionResult(
            result_type=RoundResult.OK,
            lowest_rolloff_user_ids=lowest_users if lowest_rolloff_rounds else [],
            lowest_rolloff_rounds=lowest_rolloff_rounds,
        )


@dataclass
class PendingQuestionState:
    channel_id: int
    guild_id: int
    winner_id: int
    participant_user_ids: set[int]
    lowest_tie_user_ids: set[int] = field(default_factory=set)
    prompt_message_id: int | None = None
    prompt_kind: str = "room"
