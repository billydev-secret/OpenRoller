import asyncio
import logging
import sqlite3
import time

from .logic import deserialize_user_ids, serialize_user_ids
from .models import PendingQuestionState, RiskyRollState

log = logging.getLogger(__name__)

MAX_GAMES_PER_CHANNEL = 10


class StateStore:
    def __init__(self, path: str):
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            # Detect old schema (channel_id as primary key, no game_id column).
            # If found, drop all tables — in-flight game state is ephemeral and
            # will be recreated on the next /risky_start.
            existing_tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            if "active_rounds" in existing_tables:
                columns = {row["name"] for row in conn.execute("PRAGMA table_info(active_rounds)").fetchall()}
                if "game_id" not in columns:
                    log.warning(
                        "Migrating database schema to multi-game support (game_id primary key). "
                        "Any in-progress rounds will be reset."
                    )
                    conn.execute("DROP TABLE IF EXISTS round_rolls")
                    conn.execute("DROP TABLE IF EXISTS active_rounds")
                    conn.execute("DROP TABLE IF EXISTS pending_questions")

            if "guild_settings" in existing_tables:
                gs_columns = {row["name"] for row in conn.execute("PRAGMA table_info(guild_settings)").fetchall()}
                if "min_game_seconds" not in gs_columns:
                    conn.execute("ALTER TABLE guild_settings ADD COLUMN min_game_seconds INTEGER")

            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id INTEGER PRIMARY KEY,
                    ping_role_id INTEGER,
                    min_game_seconds INTEGER
                );

                CREATE TABLE IF NOT EXISTS active_rounds (
                    game_id TEXT PRIMARY KEY,
                    channel_id INTEGER NOT NULL,
                    guild_id INTEGER NOT NULL,
                    opener_id INTEGER NOT NULL,
                    message_id INTEGER,
                    is_open INTEGER NOT NULL DEFAULT 1,
                    highest_user INTEGER,
                    lowest_user INTEGER,
                    reroll_user_ids TEXT,
                    auto_close_players INTEGER,
                    auto_close_minutes INTEGER,
                    created_at REAL
                );

                CREATE TABLE IF NOT EXISTS round_rolls (
                    game_id TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    roll INTEGER NOT NULL,
                    PRIMARY KEY (game_id, user_id),
                    FOREIGN KEY (game_id) REFERENCES active_rounds(game_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS pending_questions (
                    game_id TEXT PRIMARY KEY,
                    channel_id INTEGER NOT NULL,
                    guild_id INTEGER NOT NULL,
                    winner_id INTEGER NOT NULL,
                    prompt_message_id INTEGER,
                    participant_user_ids TEXT NOT NULL,
                    lowest_tie_user_ids TEXT,
                    prompt_kind TEXT NOT NULL DEFAULT 'room'
                );
                """
            )

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize)

    def _load_ping_roles(self) -> dict[int, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT guild_id, ping_role_id FROM guild_settings WHERE ping_role_id IS NOT NULL"
            ).fetchall()
        return {int(row["guild_id"]): int(row["ping_role_id"]) for row in rows}

    async def load_ping_roles(self) -> dict[int, int]:
        return await asyncio.to_thread(self._load_ping_roles)

    def _set_ping_role(self, guild_id: int, role_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO guild_settings (guild_id, ping_role_id)
                VALUES (?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET ping_role_id = excluded.ping_role_id
                """,
                (guild_id, role_id),
            )

    async def set_ping_role(self, guild_id: int, role_id: int) -> None:
        await asyncio.to_thread(self._set_ping_role, guild_id, role_id)

    def _set_min_game_time(self, guild_id: int, seconds: int | None) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO guild_settings (guild_id, min_game_seconds)
                VALUES (?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET min_game_seconds = excluded.min_game_seconds
                """,
                (guild_id, seconds),
            )

    async def set_min_game_time(self, guild_id: int, seconds: int | None) -> None:
        await asyncio.to_thread(self._set_min_game_time, guild_id, seconds)

    def _load_min_game_times(self) -> dict[int, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT guild_id, min_game_seconds FROM guild_settings WHERE min_game_seconds IS NOT NULL"
            ).fetchall()
        return {int(row["guild_id"]): int(row["min_game_seconds"]) for row in rows}

    async def load_min_game_times(self) -> dict[int, int]:
        return await asyncio.to_thread(self._load_min_game_times)

    def _save_round(self, state: RiskyRollState) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO active_rounds (
                    game_id,
                    channel_id,
                    guild_id,
                    opener_id,
                    message_id,
                    is_open,
                    highest_user,
                    lowest_user,
                    reroll_user_ids,
                    auto_close_players,
                    auto_close_minutes,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(game_id) DO UPDATE SET
                    channel_id = excluded.channel_id,
                    guild_id = excluded.guild_id,
                    opener_id = excluded.opener_id,
                    message_id = excluded.message_id,
                    is_open = excluded.is_open,
                    highest_user = excluded.highest_user,
                    lowest_user = excluded.lowest_user,
                    reroll_user_ids = excluded.reroll_user_ids,
                    auto_close_players = excluded.auto_close_players,
                    auto_close_minutes = excluded.auto_close_minutes,
                    created_at = excluded.created_at
                """,
                (
                    state.game_id,
                    state.channel_id,
                    state.guild_id,
                    state.opener_id,
                    state.message_id,
                    int(state.is_open),
                    state.highest_user,
                    state.lowest_user,
                    serialize_user_ids(state.reroll_user_ids),
                    state.auto_close_players,
                    state.auto_close_minutes,
                    state.created_at,
                ),
            )

            conn.execute("DELETE FROM round_rolls WHERE game_id = ?", (state.game_id,))
            for user_id, roll in state.rolls.items():
                conn.execute(
                    """
                    INSERT INTO round_rolls (game_id, user_id, roll)
                    VALUES (?, ?, ?)
                    ON CONFLICT(game_id, user_id) DO UPDATE SET roll = excluded.roll
                    """,
                    (state.game_id, user_id, roll),
                )

    async def save_round(self, state: RiskyRollState) -> None:
        await asyncio.to_thread(self._save_round, state)

    def _delete_round(self, game_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM active_rounds WHERE game_id = ?", (game_id,))

    async def delete_round(self, game_id: str) -> None:
        await asyncio.to_thread(self._delete_round, game_id)

    def _save_pending_question(self, state: PendingQuestionState) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pending_questions (
                    game_id,
                    channel_id,
                    guild_id,
                    winner_id,
                    prompt_message_id,
                    participant_user_ids,
                    lowest_tie_user_ids,
                    prompt_kind
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(game_id) DO UPDATE SET
                    channel_id = excluded.channel_id,
                    guild_id = excluded.guild_id,
                    winner_id = excluded.winner_id,
                    prompt_message_id = excluded.prompt_message_id,
                    participant_user_ids = excluded.participant_user_ids,
                    lowest_tie_user_ids = excluded.lowest_tie_user_ids,
                    prompt_kind = excluded.prompt_kind
                """,
                (
                    state.game_id,
                    state.channel_id,
                    state.guild_id,
                    state.winner_id,
                    state.prompt_message_id,
                    serialize_user_ids(state.participant_user_ids),
                    serialize_user_ids(state.lowest_tie_user_ids),
                    state.prompt_kind,
                ),
            )

    async def save_pending_question(self, state: PendingQuestionState) -> None:
        await asyncio.to_thread(self._save_pending_question, state)

    def _delete_pending_question(self, game_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM pending_questions WHERE game_id = ?", (game_id,))

    async def delete_pending_question(self, game_id: str) -> None:
        await asyncio.to_thread(self._delete_pending_question, game_id)

    def _load_active_rounds(self) -> list[RiskyRollState]:
        with self._connect() as conn:
            round_rows = conn.execute(
                """
                SELECT
                    game_id,
                    channel_id,
                    guild_id,
                    opener_id,
                    message_id,
                    is_open,
                    highest_user,
                    lowest_user,
                    reroll_user_ids,
                    auto_close_players,
                    auto_close_minutes,
                    created_at
                FROM active_rounds
                WHERE is_open = 1
                """
            ).fetchall()

            states = {
                str(row["game_id"]): RiskyRollState(
                    game_id=str(row["game_id"]),
                    channel_id=int(row["channel_id"]),
                    guild_id=int(row["guild_id"]),
                    opener_id=int(row["opener_id"]),
                    message_id=int(row["message_id"]) if row["message_id"] is not None else None,
                    is_open=bool(row["is_open"]),
                    highest_user=int(row["highest_user"]) if row["highest_user"] is not None else None,
                    lowest_user=int(row["lowest_user"]) if row["lowest_user"] is not None else None,
                    reroll_user_ids=deserialize_user_ids(row["reroll_user_ids"]),
                    auto_close_players=int(row["auto_close_players"]) if row["auto_close_players"] is not None else None,
                    auto_close_minutes=int(row["auto_close_minutes"]) if row["auto_close_minutes"] is not None else None,
                    created_at=float(row["created_at"]) if row["created_at"] is not None else time.time(),
                )
                for row in round_rows
            }

            roll_rows = conn.execute(
                """
                SELECT game_id, user_id, roll FROM round_rolls
                WHERE game_id IN (SELECT game_id FROM active_rounds WHERE is_open = 1)
                ORDER BY roll DESC
                """
            ).fetchall()

        for row in roll_rows:
            game_id = str(row["game_id"])
            state = states.get(game_id)
            if state is None:
                continue
            state.rolls[int(row["user_id"])] = int(row["roll"])

        return list(states.values())

    async def load_active_rounds(self) -> list[RiskyRollState]:
        return await asyncio.to_thread(self._load_active_rounds)

    def _load_pending_questions(self) -> list[PendingQuestionState]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    game_id,
                    channel_id,
                    guild_id,
                    winner_id,
                    prompt_message_id,
                    participant_user_ids,
                    lowest_tie_user_ids,
                    prompt_kind
                FROM pending_questions
                """
            ).fetchall()

        return [
            PendingQuestionState(
                game_id=str(row["game_id"]),
                channel_id=int(row["channel_id"]),
                guild_id=int(row["guild_id"]),
                winner_id=int(row["winner_id"]),
                participant_user_ids=deserialize_user_ids(row["participant_user_ids"]),
                prompt_message_id=(
                    int(row["prompt_message_id"]) if row["prompt_message_id"] is not None else None
                ),
                lowest_tie_user_ids=deserialize_user_ids(row["lowest_tie_user_ids"]),
                prompt_kind=str(row["prompt_kind"] or "room"),
            )
            for row in rows
        ]

    async def load_pending_questions(self) -> list[PendingQuestionState]:
        return await asyncio.to_thread(self._load_pending_questions)
