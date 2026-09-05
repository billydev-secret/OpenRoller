import asyncio
import os
import sqlite3
import tempfile
import time
import unittest
import uuid
from typing import Any

from riskyroller.models import PendingQuestionState, PostedQuestionState, RiskyRollState
from riskyroller.store import StateStore


def run(coro):
    return asyncio.run(coro)


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".sqlite3")
        self.store = StateStore(self.db_path)
        run(self.store.initialize())

    def tearDown(self) -> None:
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def make_state(self, **kwargs: Any) -> RiskyRollState:
        defaults: dict[str, Any] = {"channel_id": 100, "guild_id": 200, "opener_id": 300}
        defaults.update(kwargs)
        return RiskyRollState(**defaults)

    # --- round save/load ---

    def test_save_and_load_round_basic(self) -> None:
        state = self.make_state()
        run(self.store.save_round(state))

        loaded = run(self.store.load_active_rounds())

        self.assertEqual(1, len(loaded))
        self.assertEqual(100, loaded[0].channel_id)
        self.assertEqual(200, loaded[0].guild_id)
        self.assertEqual(300, loaded[0].opener_id)
        self.assertTrue(loaded[0].is_open)

    def test_save_and_load_round_preserves_auto_close_fields(self) -> None:
        created = time.time()
        state = self.make_state(auto_close_players=5, auto_close_minutes=10, created_at=created)
        run(self.store.save_round(state))

        loaded = run(self.store.load_active_rounds())

        self.assertEqual(5, loaded[0].auto_close_players)
        self.assertEqual(10, loaded[0].auto_close_minutes)
        self.assertAlmostEqual(created, loaded[0].created_at, places=2)

    def test_save_and_load_round_preserves_rolls(self) -> None:
        state = self.make_state()
        state.rolls = {11: 80, 22: 40, 33: 60}
        run(self.store.save_round(state))

        loaded = run(self.store.load_active_rounds())

        self.assertEqual({11: 80, 22: 40, 33: 60}, loaded[0].rolls)

    def test_save_and_load_round_preserves_message_id(self) -> None:
        state = self.make_state(message_id=999)
        run(self.store.save_round(state))

        loaded = run(self.store.load_active_rounds())

        self.assertEqual(999, loaded[0].message_id)

    def test_save_round_updates_existing(self) -> None:
        state = self.make_state()
        run(self.store.save_round(state))

        state.message_id = 999
        state.rolls = {11: 75}
        run(self.store.save_round(state))

        loaded = run(self.store.load_active_rounds())

        self.assertEqual(1, len(loaded))
        self.assertEqual(999, loaded[0].message_id)
        self.assertEqual({11: 75}, loaded[0].rolls)

    def test_save_round_replaces_rolls_on_update(self) -> None:
        state = self.make_state()
        state.rolls = {11: 50}
        run(self.store.save_round(state))

        state.rolls = {11: 75, 22: 30}
        run(self.store.save_round(state))

        loaded = run(self.store.load_active_rounds())

        self.assertEqual({11: 75, 22: 30}, loaded[0].rolls)

    def test_load_only_returns_open_rounds(self) -> None:
        run(self.store.save_round(self.make_state(channel_id=1)))
        closed = self.make_state(channel_id=2, is_open=False)
        run(self.store.save_round(closed))

        loaded = run(self.store.load_active_rounds())

        self.assertEqual(1, len(loaded))
        self.assertEqual(1, loaded[0].channel_id)

    def test_round_rolls_only_loaded_for_open_rounds(self) -> None:
        open_state = self.make_state(channel_id=1)
        open_state.rolls = {11: 90}
        run(self.store.save_round(open_state))

        closed_state = self.make_state(channel_id=2)
        closed_state.rolls = {22: 50}
        run(self.store.save_round(closed_state))
        closed_state.is_open = False
        run(self.store.save_round(closed_state))

        loaded = run(self.store.load_active_rounds())

        self.assertEqual(1, len(loaded))
        self.assertEqual(1, loaded[0].channel_id)
        self.assertEqual({11: 90}, loaded[0].rolls)

    def test_multiple_rounds_load_correctly(self) -> None:
        s1 = self.make_state(channel_id=1)
        s1.rolls = {10: 50}
        s2 = self.make_state(channel_id=2)
        s2.rolls = {20: 70, 30: 30}
        run(self.store.save_round(s1))
        run(self.store.save_round(s2))

        loaded = {s.channel_id: s for s in run(self.store.load_active_rounds())}

        self.assertEqual(2, len(loaded))
        self.assertEqual({10: 50}, loaded[1].rolls)
        self.assertEqual({20: 70, 30: 30}, loaded[2].rolls)

    # --- delete round ---

    def test_delete_round_removes_round(self) -> None:
        state = self.make_state()
        run(self.store.save_round(state))
        run(self.store.delete_round(state.game_id))

        self.assertEqual([], run(self.store.load_active_rounds()))

    def test_delete_round_cascades_to_rolls(self) -> None:
        state = self.make_state()
        state.rolls = {11: 80}
        run(self.store.save_round(state))
        run(self.store.delete_round(state.game_id))

        loaded = run(self.store.load_active_rounds())
        self.assertEqual([], loaded)

    def test_delete_nonexistent_round_is_safe(self) -> None:
        run(self.store.delete_round("999"))  # Should not raise

    # --- created_at defaults ---

    def test_created_at_defaults_to_now_when_null_in_db(self) -> None:
        before = time.time()
        state = self.make_state()
        run(self.store.save_round(state))

        loaded = run(self.store.load_active_rounds())

        self.assertGreaterEqual(loaded[0].created_at, before)

    # --- pending questions ---

    def test_save_and_load_pending_question(self) -> None:
        state = PendingQuestionState(
            channel_id=100,
            guild_id=200,
            winner_id=300,
            participant_user_ids={400, 500},
            game_id=str(uuid.uuid4()),
            prompt_kind="direct",
        )
        run(self.store.save_pending_question(state))

        loaded = run(self.store.load_pending_questions())

        self.assertEqual(1, len(loaded))
        self.assertEqual(100, loaded[0].channel_id)
        self.assertEqual(300, loaded[0].winner_id)
        self.assertEqual({400, 500}, loaded[0].participant_user_ids)
        self.assertEqual("direct", loaded[0].prompt_kind)

    def test_save_and_load_pending_question_room_kind(self) -> None:
        state = PendingQuestionState(
            channel_id=100,
            guild_id=200,
            winner_id=300,
            participant_user_ids={10, 20, 30},
            game_id=str(uuid.uuid4()),
            prompt_kind="room",
        )
        run(self.store.save_pending_question(state))

        loaded = run(self.store.load_pending_questions())

        self.assertEqual("room", loaded[0].prompt_kind)
        self.assertEqual({10, 20, 30}, loaded[0].participant_user_ids)

    def test_delete_pending_question(self) -> None:
        state = PendingQuestionState(
            channel_id=100,
            guild_id=200,
            winner_id=300,
            participant_user_ids={400},
            game_id=str(uuid.uuid4()),
            prompt_kind="room",
        )
        run(self.store.save_pending_question(state))
        run(self.store.delete_pending_question(state.game_id))

        self.assertEqual([], run(self.store.load_pending_questions()))

    def test_delete_nonexistent_pending_question_is_safe(self) -> None:
        run(self.store.delete_pending_question("999"))  # Should not raise

    def test_save_pending_question_updates_existing(self) -> None:
        state = PendingQuestionState(
            channel_id=100,
            guild_id=200,
            winner_id=300,
            participant_user_ids={400},
            game_id=str(uuid.uuid4()),
            prompt_kind="room",
        )
        run(self.store.save_pending_question(state))
        state.prompt_message_id = 555
        run(self.store.save_pending_question(state))

        loaded = run(self.store.load_pending_questions())

        self.assertEqual(1, len(loaded))
        self.assertEqual(555, loaded[0].prompt_message_id)

    # --- posted questions ---

    def test_save_and_load_posted_question(self) -> None:
        state = PostedQuestionState(
            message_id=12345,
            channel_id=100,
            guild_id=200,
            asker_id=300,
            allowed_replier_ids={400, 500},
            question_text="What is your favorite color?",
            asker_rolled_100=True,
            target_rolled_1=False,
        )
        run(self.store.save_posted_question(state))

        loaded = run(self.store.load_posted_questions())

        self.assertEqual(1, len(loaded))
        self.assertEqual(12345, loaded[0].message_id)
        self.assertEqual(100, loaded[0].channel_id)
        self.assertEqual(200, loaded[0].guild_id)
        self.assertEqual(300, loaded[0].asker_id)
        self.assertEqual({400, 500}, loaded[0].allowed_replier_ids)
        self.assertEqual("What is your favorite color?", loaded[0].question_text)
        self.assertTrue(loaded[0].asker_rolled_100)
        self.assertFalse(loaded[0].target_rolled_1)

    def test_save_posted_question_preserves_target_rolled_1(self) -> None:
        state = PostedQuestionState(
            message_id=999,
            channel_id=100,
            guild_id=200,
            asker_id=300,
            allowed_replier_ids={400},
            question_text="q",
            asker_rolled_100=False,
            target_rolled_1=True,
        )
        run(self.store.save_posted_question(state))

        loaded = run(self.store.load_posted_questions())

        self.assertFalse(loaded[0].asker_rolled_100)
        self.assertTrue(loaded[0].target_rolled_1)

    def test_save_posted_question_updates_existing(self) -> None:
        state = PostedQuestionState(
            message_id=12345,
            channel_id=100,
            guild_id=200,
            asker_id=300,
            allowed_replier_ids={400},
            question_text="original",
        )
        run(self.store.save_posted_question(state))

        state.question_text = "updated"
        state.allowed_replier_ids = {400, 500}
        run(self.store.save_posted_question(state))

        loaded = run(self.store.load_posted_questions())

        self.assertEqual(1, len(loaded))
        self.assertEqual("updated", loaded[0].question_text)
        self.assertEqual({400, 500}, loaded[0].allowed_replier_ids)

    def test_delete_posted_question_removes_row(self) -> None:
        state = PostedQuestionState(
            message_id=12345,
            channel_id=100,
            guild_id=200,
            asker_id=300,
            allowed_replier_ids={400},
            question_text="q",
        )
        run(self.store.save_posted_question(state))
        run(self.store.delete_posted_question(12345))

        self.assertEqual([], run(self.store.load_posted_questions()))

    def test_delete_nonexistent_posted_question_is_safe(self) -> None:
        run(self.store.delete_posted_question(999))  # Should not raise

    # --- ping roles ---

    def test_set_and_load_ping_role(self) -> None:
        run(self.store.set_ping_role(200, 999))

        self.assertEqual({200: 999}, run(self.store.load_ping_roles()))

    def test_set_ping_role_updates_existing(self) -> None:
        run(self.store.set_ping_role(200, 111))
        run(self.store.set_ping_role(200, 222))

        self.assertEqual({200: 222}, run(self.store.load_ping_roles()))

    def test_multiple_guilds_ping_roles(self) -> None:
        run(self.store.set_ping_role(1, 10))
        run(self.store.set_ping_role(2, 20))

        loaded = run(self.store.load_ping_roles())

        self.assertEqual({1: 10, 2: 20}, loaded)

    # --- min game time / max games per channel ---

    def test_min_game_time_zero_is_stored_not_cleared(self) -> None:
        run(self.store.set_min_game_time(200, 60))
        run(self.store.set_min_game_time(200, 0))

        self.assertEqual({200: 0}, run(self.store.load_min_game_times()))

    def test_set_and_load_max_games_per_channel(self) -> None:
        run(self.store.set_max_games_per_channel(200, 3))

        self.assertEqual({200: 3}, run(self.store.load_max_games_per_channel()))

    def test_set_max_games_per_channel_none_clears(self) -> None:
        run(self.store.set_max_games_per_channel(200, 3))
        run(self.store.set_max_games_per_channel(200, None))

        self.assertEqual({}, run(self.store.load_max_games_per_channel()))

    def test_guild_settings_columns_are_independent(self) -> None:
        run(self.store.set_ping_role(200, 999))
        run(self.store.set_min_game_time(200, 60))
        run(self.store.set_max_games_per_channel(200, 2))

        self.assertEqual({200: 999}, run(self.store.load_ping_roles()))
        self.assertEqual({200: 60}, run(self.store.load_min_game_times()))
        self.assertEqual({200: 2}, run(self.store.load_max_games_per_channel()))

    def test_delete_guild_data_clears_settings(self) -> None:
        run(self.store.set_ping_role(200, 999))
        run(self.store.set_max_games_per_channel(200, 2))
        run(self.store.set_ping_role(201, 1))

        run(self.store.delete_guild_data(200))

        self.assertEqual({201: 1}, run(self.store.load_ping_roles()))
        self.assertEqual({}, run(self.store.load_max_games_per_channel()))

    def test_legacy_guild_settings_table_gains_new_columns(self) -> None:
        fd, path = tempfile.mkstemp(suffix=".sqlite3")
        self.addCleanup(os.unlink, path)
        os.close(fd)
        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TABLE guild_settings (guild_id INTEGER PRIMARY KEY, ping_role_id INTEGER)"
            )
            conn.execute("INSERT INTO guild_settings (guild_id, ping_role_id) VALUES (7, 8)")
        store = StateStore(path)
        run(store.initialize())

        run(store.set_min_game_time(7, 30))
        run(store.set_max_games_per_channel(7, 4))

        self.assertEqual({7: 8}, run(store.load_ping_roles()))
        self.assertEqual({7: 30}, run(store.load_min_game_times()))
        self.assertEqual({7: 4}, run(store.load_max_games_per_channel()))

    # --- sweeps ---

    def _pending(self, game_id: str, created_at: float) -> PendingQuestionState:
        return PendingQuestionState(
            channel_id=100,
            guild_id=200,
            winner_id=300,
            participant_user_ids={400},
            game_id=game_id,
            prompt_kind="direct",
            created_at=created_at,
        )

    def test_sweep_old_pending_questions_removes_stale_and_keeps_fresh(self) -> None:
        week = 7 * 86400
        run(self.store.save_pending_question(self._pending("old", time.time() - week - 60)))
        run(self.store.save_pending_question(self._pending("new", time.time())))

        swept = run(self.store.sweep_old_pending_questions(week))

        self.assertEqual(1, swept)
        loaded = run(self.store.load_pending_questions())
        self.assertEqual(["new"], [s.game_id for s in loaded])

    def test_sweep_old_pending_questions_treats_null_created_at_as_stale(self) -> None:
        # A row written before the column existed has no timestamp; it is old
        # by definition and must not survive the sweep forever.
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO pending_questions "
                "(game_id, channel_id, guild_id, winner_id, participant_user_ids, created_at) "
                "VALUES ('legacy', 1, 2, 3, '4', NULL)"
            )

        swept = run(self.store.sweep_old_pending_questions(7 * 86400))

        self.assertEqual(1, swept)
        self.assertEqual([], run(self.store.load_pending_questions()))

    def test_pending_question_created_at_survives_resave(self) -> None:
        # The two-questioner prompt re-saves after the first question; the
        # clock must not restart or the prompt could outlive the sweep.
        original = time.time() - 3 * 86400
        state = self._pending("g", original)
        run(self.store.save_pending_question(state))

        state.created_at = time.time()
        state.prompt_message_id = 555
        run(self.store.save_pending_question(state))

        loaded = run(self.store.load_pending_questions())
        self.assertEqual(555, loaded[0].prompt_message_id)
        self.assertAlmostEqual(original, loaded[0].created_at, places=2)

    def test_sweep_old_posted_questions_removes_stale_and_keeps_fresh(self) -> None:
        week = 7 * 86400
        for message_id, created in ((1, time.time() - week - 60), (2, time.time())):
            run(self.store.save_posted_question(PostedQuestionState(
                message_id=message_id,
                channel_id=100,
                guild_id=200,
                asker_id=300,
                allowed_replier_ids={400},
                question_text="q",
                created_at=created,
            )))

        swept = run(self.store.sweep_old_posted_questions(week))

        self.assertEqual(1, swept)
        self.assertEqual([2], [s.message_id for s in run(self.store.load_posted_questions())])

    # --- schema migration ---

    def test_connections_are_closed_after_use(self) -> None:
        with self.store._connect() as conn:
            conn.execute("SELECT 1")

        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")

    def test_connection_rolls_back_and_closes_on_error(self) -> None:
        with self.assertRaises(RuntimeError):
            with self.store._connect() as conn:
                conn.execute("INSERT INTO guild_settings (guild_id, ping_role_id) VALUES (1, 2)")
                raise RuntimeError("boom")

        self.assertEqual({}, run(self.store.load_ping_roles()))
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")

    def test_database_runs_in_wal_mode(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]

        self.assertEqual("wal", mode)


    def test_initialize_is_idempotent(self) -> None:
        run(self.store.initialize())  # Second call should not raise or duplicate

    def test_legacy_reroll_column_is_tolerated(self) -> None:
        # Databases created before the reroll was removed still carry the
        # reroll_user_ids column. It is left in place rather than dropped, so
        # saving and loading a round must work around it without error.
        import sqlite3

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("ALTER TABLE active_rounds ADD COLUMN reroll_user_ids TEXT")
        run(self.store.initialize())

        state = self.make_state()
        state.rolls = {11: 80, 22: 40}
        run(self.store.save_round(state))

        loaded = run(self.store.load_active_rounds())

        self.assertEqual(1, len(loaded))
        self.assertEqual({11: 80, 22: 40}, loaded[0].rolls)


if __name__ == "__main__":
    unittest.main()
