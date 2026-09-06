import asyncio
import logging
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager

from .logic import deserialize_user_ids, serialize_user_ids
from .models import PendingQuestionState, PostedQuestionState, PromptKind, RiskyRollState

log = logging.getLogger(__name__)


class StateStore:
    def __init__(self, path: str):
        self.path = path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open a connection, commit (or roll back) on exit, and always close it.

        ``sqlite3.Connection`` used directly as a context manager only manages
        the transaction; it leaves the connection open until garbage
        collection, which under WAL also keeps the -wal/-shm files pinned.
        """
        conn = sqlite3.connect(self.path, timeout=30)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA cache_size=-32000")
            conn.execute("PRAGMA mmap_size=268435456")
            conn.execute("PRAGMA foreign_keys = ON")
            with conn:
                yield conn
        finally:
            conn.close()

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
                    existing_tables -= {"round_rolls", "active_rounds", "pending_questions"}

            if "active_rounds" in existing_tables:
                ar_columns = {row["name"] for row in conn.execute("PRAGMA table_info(active_rounds)").fetchall()}
                if "skip_min_game_time" not in ar_columns:
                    conn.execute("ALTER TABLE active_rounds ADD COLUMN skip_min_game_time INTEGER NOT NULL DEFAULT 0")
                if "second_lowest_user" not in ar_columns:
                    conn.execute("ALTER TABLE active_rounds ADD COLUMN second_lowest_user INTEGER")
                if "second_highest_user" not in ar_columns:
                    conn.execute("ALTER TABLE active_rounds ADD COLUMN second_highest_user INTEGER")

            if "pending_questions" in existing_tables:
                pq_columns = {row["name"] for row in conn.execute("PRAGMA table_info(pending_questions)").fetchall()}
                if "extra_questioner_id" not in pq_columns:
                    conn.execute("ALTER TABLE pending_questions ADD COLUMN extra_questioner_id INTEGER")
                if "questioners_asked" not in pq_columns:
                    conn.execute("ALTER TABLE pending_questions ADD COLUMN questioners_asked TEXT")
                if "created_at" not in pq_columns:
                    conn.execute("ALTER TABLE pending_questions ADD COLUMN created_at REAL")
                    # Every row that exists at this exact moment was written
                    # before the column did, but that does not make it old —
                    # it may be a prompt posted seconds ago. Backfill "now"
                    # rather than leaving it NULL for the sweep to treat as
                    # ancient.
                    conn.execute(
                        "UPDATE pending_questions SET created_at = ? WHERE created_at IS NULL",
                        (time.time(),),
                    )

            if "guild_settings" in existing_tables:
                gs_columns = {row["name"] for row in conn.execute("PRAGMA table_info(guild_settings)").fetchall()}
                if "min_game_seconds" not in gs_columns:
                    conn.execute("ALTER TABLE guild_settings ADD COLUMN min_game_seconds INTEGER")
                if "max_games_per_channel" not in gs_columns:
                    conn.execute("ALTER TABLE guild_settings ADD COLUMN max_games_per_channel INTEGER")

            if "posted_questions" in existing_tables:
                pq_columns = {row["name"] for row in conn.execute("PRAGMA table_info(posted_questions)").fetchall()}
                if "created_at" not in pq_columns:
                    conn.execute("ALTER TABLE posted_questions ADD COLUMN created_at INTEGER")
                    conn.execute(
                        "UPDATE posted_questions SET created_at = ? WHERE created_at IS NULL",
                        (int(time.time()),),
                    )

            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id INTEGER PRIMARY KEY,
                    ping_role_id INTEGER,
                    min_game_seconds INTEGER,
                    max_games_per_channel INTEGER
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
                    auto_close_players INTEGER,
                    auto_close_minutes INTEGER,
                    created_at REAL,
                    skip_min_game_time INTEGER NOT NULL DEFAULT 0,
                    second_lowest_user INTEGER,
                    second_highest_user INTEGER
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
                    prompt_kind TEXT NOT NULL DEFAULT 'room',
                    extra_questioner_id INTEGER,
                    questioners_asked TEXT,
                    created_at REAL
                );

                CREATE TABLE IF NOT EXISTS posted_questions (
                    message_id INTEGER PRIMARY KEY,
                    channel_id INTEGER NOT NULL,
                    guild_id INTEGER NOT NULL,
                    asker_id INTEGER NOT NULL,
                    allowed_replier_ids TEXT NOT NULL,
                    question_text TEXT NOT NULL,
                    asker_rolled_100 INTEGER NOT NULL DEFAULT 0,
                    target_rolled_1 INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER
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

    def _set_max_games_per_channel(self, guild_id: int, cap: int | None) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO guild_settings (guild_id, max_games_per_channel)
                VALUES (?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET max_games_per_channel = excluded.max_games_per_channel
                """,
                (guild_id, cap),
            )

    async def set_max_games_per_channel(self, guild_id: int, cap: int | None) -> None:
        await asyncio.to_thread(self._set_max_games_per_channel, guild_id, cap)

    def _load_max_games_per_channel(self) -> dict[int, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT guild_id, max_games_per_channel FROM guild_settings "
                "WHERE max_games_per_channel IS NOT NULL"
            ).fetchall()
        return {int(row["guild_id"]): int(row["max_games_per_channel"]) for row in rows}

    async def load_max_games_per_channel(self) -> dict[int, int]:
        return await asyncio.to_thread(self._load_max_games_per_channel)

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
                    auto_close_players,
                    auto_close_minutes,
                    created_at,
                    skip_min_game_time,
                    second_lowest_user,
                    second_highest_user
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(game_id) DO UPDATE SET
                    channel_id = excluded.channel_id,
                    guild_id = excluded.guild_id,
                    opener_id = excluded.opener_id,
                    message_id = excluded.message_id,
                    is_open = excluded.is_open,
                    highest_user = excluded.highest_user,
                    lowest_user = excluded.lowest_user,
                    auto_close_players = excluded.auto_close_players,
                    auto_close_minutes = excluded.auto_close_minutes,
                    created_at = excluded.created_at,
                    skip_min_game_time = excluded.skip_min_game_time,
                    second_lowest_user = excluded.second_lowest_user,
                    second_highest_user = excluded.second_highest_user
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
                    state.auto_close_players,
                    state.auto_close_minutes,
                    state.created_at,
                    int(state.skip_min_game_time),
                    state.second_lowest_user,
                    state.second_highest_user,
                ),
            )

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
                    prompt_kind,
                    extra_questioner_id,
                    questioners_asked,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(game_id) DO UPDATE SET
                    channel_id = excluded.channel_id,
                    guild_id = excluded.guild_id,
                    winner_id = excluded.winner_id,
                    prompt_message_id = excluded.prompt_message_id,
                    participant_user_ids = excluded.participant_user_ids,
                    lowest_tie_user_ids = excluded.lowest_tie_user_ids,
                    prompt_kind = excluded.prompt_kind,
                    extra_questioner_id = excluded.extra_questioner_id,
                    questioners_asked = excluded.questioners_asked
                    -- created_at is NOT refreshed: a two-questioner round
                    -- re-saves this row when the first of the two asks, and
                    -- restarting the clock there would let a half-finished
                    -- prompt outlive the sweep for as long as someone kept
                    -- feeding it.
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
                    state.extra_questioner_id,
                    serialize_user_ids(state.questioners_asked),
                    state.created_at,
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
                    auto_close_players,
                    auto_close_minutes,
                    created_at,
                    skip_min_game_time,
                    second_lowest_user,
                    second_highest_user
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
                    auto_close_players=int(row["auto_close_players"]) if row["auto_close_players"] is not None else None,
                    auto_close_minutes=int(row["auto_close_minutes"]) if row["auto_close_minutes"] is not None else None,
                    created_at=float(row["created_at"]) if row["created_at"] is not None else time.time(),
                    skip_min_game_time=bool(row["skip_min_game_time"]),
                    second_lowest_user=int(row["second_lowest_user"]) if row["second_lowest_user"] is not None else None,
                    second_highest_user=int(row["second_highest_user"]) if row["second_highest_user"] is not None else None,
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
                    prompt_kind,
                    extra_questioner_id,
                    questioners_asked,
                    created_at
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
                prompt_kind=PromptKind(row["prompt_kind"] or PromptKind.ROOM.value),
                extra_questioner_id=(
                    int(row["extra_questioner_id"]) if row["extra_questioner_id"] is not None else None
                ),
                questioners_asked=deserialize_user_ids(row["questioners_asked"]),
                created_at=float(row["created_at"]) if row["created_at"] is not None else 0.0,
            )
            for row in rows
        ]

    async def load_pending_questions(self) -> list[PendingQuestionState]:
        return await asyncio.to_thread(self._load_pending_questions)

    def _save_posted_question(self, state: PostedQuestionState) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO posted_questions (
                    message_id,
                    channel_id,
                    guild_id,
                    asker_id,
                    allowed_replier_ids,
                    question_text,
                    asker_rolled_100,
                    target_rolled_1,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    channel_id = excluded.channel_id,
                    guild_id = excluded.guild_id,
                    asker_id = excluded.asker_id,
                    allowed_replier_ids = excluded.allowed_replier_ids,
                    question_text = excluded.question_text,
                    asker_rolled_100 = excluded.asker_rolled_100,
                    target_rolled_1 = excluded.target_rolled_1
                """,
                (
                    state.message_id,
                    state.channel_id,
                    state.guild_id,
                    state.asker_id,
                    serialize_user_ids(state.allowed_replier_ids),
                    state.question_text,
                    int(state.asker_rolled_100),
                    int(state.target_rolled_1),
                    int(state.created_at),
                ),
            )

    async def save_posted_question(self, state: PostedQuestionState) -> None:
        await asyncio.to_thread(self._save_posted_question, state)

    def _delete_posted_question(self, message_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM posted_questions WHERE message_id = ?", (message_id,))

    async def delete_posted_question(self, message_id: int) -> None:
        await asyncio.to_thread(self._delete_posted_question, message_id)

    def _load_posted_questions(self) -> list[PostedQuestionState]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    message_id,
                    channel_id,
                    guild_id,
                    asker_id,
                    allowed_replier_ids,
                    question_text,
                    asker_rolled_100,
                    target_rolled_1,
                    created_at
                FROM posted_questions
                """
            ).fetchall()

        return [
            PostedQuestionState(
                message_id=int(row["message_id"]),
                channel_id=int(row["channel_id"]),
                guild_id=int(row["guild_id"]),
                asker_id=int(row["asker_id"]),
                allowed_replier_ids=deserialize_user_ids(row["allowed_replier_ids"]),
                question_text=str(row["question_text"]),
                asker_rolled_100=bool(row["asker_rolled_100"]),
                target_rolled_1=bool(row["target_rolled_1"]),
                created_at=float(row["created_at"]) if row["created_at"] is not None else time.time(),
            )
            for row in rows
        ]

    async def load_posted_questions(self) -> list[PostedQuestionState]:
        return await asyncio.to_thread(self._load_posted_questions)

    def _delete_guild_data(self, guild_id: int) -> list[str]:
        with self._connect() as conn:
            game_id_rows = conn.execute(
                "SELECT game_id FROM active_rounds WHERE guild_id = ?", (guild_id,)
            ).fetchall()
            game_ids = [str(row["game_id"]) for row in game_id_rows]

            conn.execute("DELETE FROM active_rounds WHERE guild_id = ?", (guild_id,))
            conn.execute("DELETE FROM pending_questions WHERE guild_id = ?", (guild_id,))
            conn.execute("DELETE FROM posted_questions WHERE guild_id = ?", (guild_id,))
            conn.execute("DELETE FROM guild_settings WHERE guild_id = ?", (guild_id,))
        return game_ids

    async def delete_guild_data(self, guild_id: int) -> list[str]:
        return await asyncio.to_thread(self._delete_guild_data, guild_id)

    def _sweep_old_posted_questions(self, max_age_seconds: int) -> int:
        cutoff = int(time.time()) - max_age_seconds
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM posted_questions WHERE created_at IS NOT NULL AND created_at < ?",
                (cutoff,),
            )
            return cursor.rowcount or 0

    async def sweep_old_posted_questions(self, max_age_seconds: int) -> int:
        return await asyncio.to_thread(self._sweep_old_posted_questions, max_age_seconds)

    def _sweep_old_pending_questions(self, max_age_seconds: int) -> int:
        """Drop prompts whose winner never pressed Ask Question.

        ``created_at`` is always populated: new rows get it from the model
        default, and the migration backfills "now" onto any row left over
        from before the column existed (see ``_initialize``). So a NULL row
        here is never the ordinary case, and — like the posted-question
        sweep just above — is left alone rather than treated as stale.
        """
        cutoff = time.time() - max_age_seconds
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM pending_questions WHERE created_at < ?",
                (cutoff,),
            )
            return cursor.rowcount or 0

    async def sweep_old_pending_questions(self, max_age_seconds: int) -> int:
        return await asyncio.to_thread(self._sweep_old_pending_questions, max_age_seconds)
