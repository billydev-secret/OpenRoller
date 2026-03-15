import asyncio
import os
import tempfile
import time
import unittest

from riskyroller.models import PendingQuestionState, RiskyRollState
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

    def make_state(self, **kwargs) -> RiskyRollState:
        defaults = dict(channel_id=100, guild_id=200, opener_id=300)
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

    def test_save_and_load_round_preserves_reroll_user_ids(self) -> None:
        state = self.make_state()
        state.reroll_user_ids = {11, 22}
        run(self.store.save_round(state))

        loaded = run(self.store.load_active_rounds())

        self.assertEqual({11, 22}, loaded[0].reroll_user_ids)

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
        run(self.store.save_round(self.make_state()))
        run(self.store.delete_round(100))

        self.assertEqual([], run(self.store.load_active_rounds()))

    def test_delete_round_cascades_to_rolls(self) -> None:
        state = self.make_state()
        state.rolls = {11: 80}
        run(self.store.save_round(state))
        run(self.store.delete_round(100))

        loaded = run(self.store.load_active_rounds())
        self.assertEqual([], loaded)

    def test_delete_nonexistent_round_is_safe(self) -> None:
        run(self.store.delete_round(999))  # Should not raise

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
            prompt_kind="room",
        )
        run(self.store.save_pending_question(state))
        run(self.store.delete_pending_question(100))

        self.assertEqual([], run(self.store.load_pending_questions()))

    def test_delete_nonexistent_pending_question_is_safe(self) -> None:
        run(self.store.delete_pending_question(999))  # Should not raise

    def test_save_pending_question_updates_existing(self) -> None:
        state = PendingQuestionState(
            channel_id=100,
            guild_id=200,
            winner_id=300,
            participant_user_ids={400},
            prompt_kind="room",
        )
        run(self.store.save_pending_question(state))
        state.prompt_message_id = 555
        run(self.store.save_pending_question(state))

        loaded = run(self.store.load_pending_questions())

        self.assertEqual(1, len(loaded))
        self.assertEqual(555, loaded[0].prompt_message_id)

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

    # --- schema migration ---

    def test_initialize_is_idempotent(self) -> None:
        run(self.store.initialize())  # Second call should not raise or duplicate


if __name__ == "__main__":
    unittest.main()
